"""Pinned source-derived wire fixtures; these are not real hardware recordings."""
from __future__ import annotations

import asyncio
import contextlib
import copy
import json
import time
from pathlib import Path

import httpx
import pytest

from microduck_harness.executor import ExecutorSettings, create_executor_app
from microduck_harness.robot import RobotError, RobotdClient, inspect_firmware, normalize_state


@pytest.fixture(params=["0.16.3", "0.17.8"])
def wire(request):
    return json.loads((Path(__file__).parent / "fixtures" / f"robotd_{request.param}.json").read_text())


def fresh_state(wire):
    state = copy.deepcopy(wire["state"])
    state["t_ns"] = time.monotonic_ns()
    return state


@contextlib.asynccontextmanager
async def native_daemon(tmp_path, wire):
    """NDJSON server that keeps hello and subscribe/move on the same connection."""
    path = str(tmp_path / "robotd.sock")
    seen, tasks = [], set()

    async def handler(reader, writer):
        task = asyncio.current_task()
        tasks.add(task)
        connection = []
        seen.append(connection)
        try:
            while line := await reader.readline():
                request = json.loads(line)
                method = request["method"]
                connection.append(method)
                assert method != "robot.state"  # This is a notification, never a request.
                if method == "hello":
                    result = copy.deepcopy(wire["hello"])
                elif method == "robot.health":
                    result = wire["health"]
                elif method == "robot.subscribe":
                    result = wire["subscribe"]
                else:
                    result = {"accepted": True}
                writer.write(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}).encode() + b"\n")
                await writer.drain()
                if method == "robot.subscribe":
                    while True:
                        event = {"jsonrpc": "2.0", "method": "robot.state", "params": fresh_state(wire)}
                        writer.write(json.dumps(event).encode() + b"\n")
                        await writer.drain()
                        await asyncio.sleep(0.02)
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            tasks.discard(task)

    server = await asyncio.start_unix_server(handler, path)
    try:
        yield path, seen
    finally:
        server.close()
        await server.wait_closed()
        active = list(tasks)
        for task in active:
            task.cancel()
        await asyncio.gather(*active, return_exceptions=True)


def test_pinned_profiles_and_complete_measurements(wire):
    expected = wire["hello"]["daemon_version"]
    for selected in ("auto", expected):
        status = inspect_firmware(wire["hello"], selected)
        assert status["compatible"] and status["source_revision_matches_tag"]
        assert status["daemon_version"] == expected
        assert status["api_version"] == (41 if expected == "0.16.3" else 44)
    state = normalize_state(fresh_state(wire), wire["health"])
    assert state["healthy"] and state["enabled"]
    assert len(state["raw"]["joints"]) == 15
    assert state["frames"]["camera"] == wire["state"]["frames"]["camera"]
    assert state["frames"]["tof"] == wire["state"]["frames"]["tof"]


@pytest.mark.parametrize("edit", [
    {"daemon_version": None}, {"daemon_version": "0.17.9"},
    {"daemon_version": "0.17.8-rc.1"}, {"api_version": 45},
    {"api_version": True}, {"revision": 123},
])
def test_unknown_and_malformed_hello_never_select_a_safe_profile(wire, edit):
    assert inspect_firmware({**wire["hello"], **edit})["compatible"] is False


def test_declared_version_mismatch_is_not_overridden_by_api_guess(wire):
    other = "0.17.8" if wire["hello"]["daemon_version"] == "0.16.3" else "0.16.3"
    report = inspect_firmware(wire["hello"], other)
    assert not report["compatible"]
    assert "configured_daemon_version_mismatch" in report["reasons"]


def test_local_build_revision_and_semver_metadata_are_visible(wire):
    version = wire["hello"]["daemon_version"]
    local = {**wire["hello"], "daemon_version": version + "+imu-id200.local", "revision": "a" * 40}
    report = inspect_firmware(local, version)
    assert report["compatible"]
    assert report["daemon_version"] == local["daemon_version"]
    assert report["profile_version"] == version
    assert report["source_revision_matches_tag"] is False
    assert len(report["warnings"]) == 2
    assert inspect_firmware({**wire["hello"], "revision": None})["compatible"]


@pytest.mark.parametrize("path,value", [
    (("imu",), None), (("control_loop",), None), (("bus",), None),
    (("imu", "ready"), False), (("imu", "consecutive_stale_blocks"), None),
    (("imu", "consecutive_stale_blocks"), True), (("imu", "consecutive_stale_blocks"), -1),
    (("imu", "missing"), "unknown"), (("control_loop", "last_tick_age_ms"), True),
    (("control_loop", "last_tick_age_ms"), -1), (("bus", "consecutive_errors"), False),
    (("bus", "missing"), None),
])
def test_required_health_fields_have_no_permissive_legacy_fallback(wire, path, value):
    health = copy.deepcopy(wire["health"])
    target = health
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = value
    assert not normalize_state(fresh_state(wire), health)["healthy"]


@pytest.mark.parametrize("flag", ["fallen", "limp", "picked_up"])
def test_safety_flags_are_required_for_both_releases(wire, flag):
    state = fresh_state(wire)
    state["safety"].pop(flag)
    assert not normalize_state(state, wire["health"])["healthy"]


@pytest.mark.asyncio
async def test_real_hello_subscription_and_movement_connection(wire, tmp_path):
    async with native_daemon(tmp_path, wire) as (path, seen):
        robot = RobotdClient(path, robotd_version=wire["hello"]["daemon_version"])
        try:
            state = await robot.state()
            assert state["healthy"] and state["firmware"]["compatible"]
            await robot.call("robot.move", {"vx": 0.04, "vy": 0, "vyaw": 0})
            await robot.call("robot.stop")
            assert ["hello", "robot.subscribe"] in seen
            assert ["hello", "robot.move"] in seen
            assert ["robot.stop"] in seen
        finally:
            await robot.aclose()


@pytest.mark.asyncio
async def test_mismatch_visible_in_state_and_blocks_move_but_not_stop(wire, tmp_path):
    expected = "0.17.8" if wire["hello"]["daemon_version"] == "0.16.3" else "0.16.3"
    async with native_daemon(tmp_path, wire) as (path, seen):
        robot = RobotdClient(path, robotd_version=expected)
        try:
            state = await robot.state()
            assert not state["healthy"] and not state["firmware"]["compatible"]
            assert "firmware_configured_daemon_version_mismatch" in state["health_reasons"]
            with pytest.raises(RobotError, match="firmware incompatible"):
                await robot.call("robot.move", {"vx": 0.04})
            await robot.call("robot.stop")
            assert not any("robot.move" in connection for connection in seen)
            assert ["robot.stop"] in seen
        finally:
            await robot.aclose()


@pytest.mark.asyncio
async def test_changed_build_between_state_and_command_requires_new_subscription(wire, tmp_path):
    async with native_daemon(tmp_path, wire) as (path, seen):
        robot = RobotdClient(path)
        try:
            assert (await robot.state())["healthy"]
            wire["hello"]["revision"] = "b" * 40  # Another locally patched binary.
            with pytest.raises(RobotError, match="identity changed"):
                await robot.call("robot.move", {"vx": 0.04})
            assert not any("robot.move" in connection for connection in seen)
            assert not (await robot.state())["healthy"]
            assert (await robot.call("robot.stop"))["accepted"]
        finally:
            await robot.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("sensor", ["VL53L8CX", "fake", "sim", None])
async def test_native_media_and_tof_frames_keep_same_wire_contract(wire, tmp_path, sensor):
    wire["tof_stream"]["sensor"] = sensor
    paths = {name: str(tmp_path / f"{name}.sock") for name in ("media", "tof")}
    tasks = set()

    async def handler(reader, writer):
        task = asyncio.current_task()
        tasks.add(task)
        try:
            request = json.loads(await reader.readline())
            if request["method"] == "media.frame":
                result = {**wire["media_frame"], "captured_at_unix_us": int(time.time() * 1e6)}
                tail = bytes(wire["media_bytes"])
            else:
                assert request["method"] == "tof.stream"
                result, tail = wire["tof_stream"], b""
            writer.write(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}).encode() + b"\n" + tail)
            await writer.drain()
            if request["method"] == "tof.stream":
                while True:
                    frame = {**wire["tof_frame"], "t_ns": time.monotonic_ns()}
                    event = {"jsonrpc": "2.0", "method": "tof.frame", "params": frame}
                    writer.write(json.dumps(event).encode() + b"\n")
                    await writer.drain()
                    await asyncio.sleep(0.02)
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            tasks.discard(task)

    servers = [await asyncio.start_unix_server(handler, path) for path in paths.values()]
    robot = RobotdClient(media_socket=paths["media"], tof_socket=paths["tof"])
    try:
        header, data = await robot.media_frame()
        assert header["rotate"] == 180 and data == bytes(wire["media_bytes"])
        if sensor == "VL53L8CX":
            sample = await robot.tof_frame()
            assert sample["frame"]["distance_mm"] == wire["tof_frame"]["distance_mm"]
            assert sample["frame"]["status"] == wire["tof_frame"]["status"]
            assert 0 <= sample["age_s"] < 0.2
        else:
            with pytest.raises(RobotError, match="synthetic|no measured"):
                await robot.tof_frame()
    finally:
        await robot.aclose()
        for server in servers:
            server.close()
            await server.wait_closed()
        active = list(tasks)
        for task in active:
            task.cancel()
        await asyncio.gather(*active, return_exceptions=True)


def test_executor_version_is_local_configuration_and_bind_requires_token(monkeypatch):
    monkeypatch.setenv("EXECUTOR_ROBOTD_VERSION", "0.16.3")
    monkeypatch.setenv("EXECUTOR_HOST", "0.0.0.0")
    monkeypatch.setenv("EXECUTOR_TOKEN", "local-test-token-with-over-24-characters")
    cfg = ExecutorSettings.from_env()
    cfg.validate()
    assert cfg.robotd_version == "0.16.3" and cfg.bind_host == "0.0.0.0"
    assert create_executor_app(cfg).state.executor.robot.robotd_version == "0.16.3"
    with pytest.raises(ValueError, match="ROBOTD_VERSION"):
        ExecutorSettings(robotd_version="0.17.9").validate()
    with pytest.raises(ValueError, match="non-loopback"):
        ExecutorSettings(bind_host="0.0.0.0", token="short").validate()


@pytest.mark.asyncio
async def test_health_reports_unchecked_identity_without_claiming_robot_health():
    app = create_executor_app({"robotd_version": "0.17.8"})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://executor") as client:
        health = (await client.get("/health")).json()
    assert health["robotd_version_configured"] == "0.17.8"
    assert health["firmware"]["identity_source"] == "not_yet_checked"
    assert not health["firmware"]["compatible"]
