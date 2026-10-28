from __future__ import annotations

import asyncio
import contextlib
import copy
import json
import time

import httpx
import pytest
from fastapi import HTTPException

from microduck_harness.executor import ActionRequest, ExecutorService, ExecutorSettings, create_executor_app
from microduck_harness.robot import RobotError, RobotdClient, normalize_state


def native_state():
    return {
        "t": 12.0, "t_ns": time.monotonic_ns(),
        "move": {"requested": [0, 0, 0], "applied": [0, 0, 0]},
        "head": [0, 0, 0, 0], "policy": "stand", "loop": {"hz": 50, "missed": 0},
        "safety": {"fallen": False, "limp": False, "picked_up": False, "gravity": [0, 0, -1], "gain": 300},
        "joints": [0.0] * 15, "targets": [0.0] * 15,
        "imu": {"quat": [1, 0, 0, 0], "gyro": [0, 0, 0]},
        "frames": {"camera": {"pos": [0.1, 0, 0.2], "quat": [1, 0, 0, 0]},
                   "tof": {"pos": [0.1, 0, 0.2], "quat": [1, 0, 0, 0]}},
    }


def native_health():
    return {"healthy": True, "bus": {"consecutive_errors": 0},
            "imu": {"ready": True, "consecutive_stale_blocks": 0},
            "control_loop": {"last_tick_age_ms": 5}}


class FakeRobot:
    def __init__(self):
        self.commands = []
        self.fallen = False
        self.stop_failure = False
        self.move_delay = 0
        self.stop_delay = 0
        self.tof_range_mm = 1000
        self.tof_status = 5

    async def state(self):
        raw = native_state()
        raw["safety"]["fallen"] = self.fallen
        return normalize_state(raw, native_health())

    async def call(self, method, params=None):
        self.commands.append((method, params, time.monotonic()))
        if method == "robot.move":
            await asyncio.sleep(self.move_delay)
        if method == "robot.stop":
            await asyncio.sleep(self.stop_delay)
            if self.stop_failure:
                raise RobotError("stop socket unavailable")
        return {"accepted": True}

    async def aclose(self):
        pass

    async def tof_frame(self):
        return {"age_s": 0.01, "frame": {"t_ns": time.monotonic_ns(), "rows": 8, "cols": 8,
                "distance_mm": [self.tof_range_mm] * 64, "status": [self.tof_status] * 64}}


async def request_for(service, request_id="test-1", **overrides):
    state = await service.snapshot()
    return ActionRequest(request_id=request_id, epoch=state["epoch"], state_token=state["state_token"],
                         vx=0.04, vy=0.0, vyaw=0.0, duration_s=overrides.pop("duration_s", 0.10),
                         observation_age_s=0.01, **overrides)


def test_state_is_native_measured_not_unknown_defaults():
    state = normalize_state(native_state(), native_health())
    assert state["enabled"] and state["healthy"]
    assert state["roll"] == 0 and state["pitch"] == 0
    assert state["head"] == [0.0] * 4
    incomplete = native_health()
    incomplete.pop("imu")
    assert normalize_state(native_state(), incomplete)["healthy"] is False
    raw = native_state()
    raw["policy"] = "held"
    assert normalize_state(raw, native_health())["enabled"] is False


def test_missing_and_stale_telemetry_blocks_motion():
    raw = native_state()
    raw["joints"] = [0] * 10
    with pytest.raises(RobotError, match="15"):
        normalize_state(raw, native_health())
    raw = native_state()
    raw["t_ns"] -= 1_000_000_000
    assert normalize_state(raw, native_health())["healthy"] is False
    raw.pop("t_ns")
    with pytest.raises(RobotError, match="t_ns"):
        normalize_state(raw, native_health())


@pytest.mark.asyncio
async def test_real_unix_jsonrpc_subscribe_and_health(tmp_path):
    socket_path = str(tmp_path / "robotd.sock")
    seen = []
    handlers = set()

    async def handler(reader, writer):
        handlers.add(asyncio.current_task())
        try:
            req = json.loads(await reader.readline())
            seen.append(req)
            if req["method"] == "hello":
                result = {"daemon_version": "0.17.8", "api_version": 44, "revision": None}
                writer.write(json.dumps({"jsonrpc": "2.0", "id": req["id"], "result": result}).encode() + b"\n")
                await writer.drain()
                req = json.loads(await reader.readline())
                seen.append(req)
            assert req["method"] != "robot.state"
            result = native_health() if req["method"] == "robot.health" else {"accepted": True}
            writer.write(json.dumps({"jsonrpc": "2.0", "id": req["id"], "result": result}).encode() + b"\n")
            await writer.drain()
            if req["method"] == "robot.subscribe":
                while True:
                    writer.write(json.dumps({"jsonrpc": "2.0", "method": "robot.state", "params": native_state()}).encode() + b"\n")
                    await writer.drain()
                    await asyncio.sleep(0.03)
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            handlers.discard(asyncio.current_task())

    server = await asyncio.start_unix_server(handler, socket_path)
    robot = RobotdClient(socket_path)
    try:
        state = await robot.state()
        assert state["healthy"]
        assert state["firmware"]["daemon_version"] == "0.17.8"
        assert (await robot.call("robot.move", {"vx": 0.05, "vy": 0, "vyaw": 0}))["accepted"]
        assert any(r["method"] == "robot.subscribe" for r in seen)
        assert any(r["method"] == "robot.health" for r in seen)
        move = next(r for r in seen if r["method"] == "robot.move")
        assert move["params"]["vx"] == 0.05
    finally:
        await robot.aclose()
        server.close()
        await server.wait_closed()
        for task in list(handlers):
            task.cancel()
        await asyncio.gather(*handlers, return_exceptions=True)


@pytest.mark.asyncio
async def test_bounded_pulse_ends_with_stop_and_honest_receipt():
    robot = FakeRobot()
    service = ExecutorService(ExecutorSettings(allow_motion=True), robot)
    action = await request_for(service)
    result = await service.execute(action)
    assert result["status"] == "completed"
    assert result["verified_success"] is False
    assert result["stop"]["verified_physical_stop"] is False
    assert robot.commands[-1][0] == "robot.stop"
    count = len(robot.commands)
    assert await service.execute(action) == result
    assert len(robot.commands) == count  # Retry only returns the original receipt.
    with pytest.raises(HTTPException) as caught:
        await service.execute(action.model_copy(update={"vx": 0.03}))
    assert caught.value.status_code == 409


@pytest.mark.asyncio
async def test_http_waiter_cancellation_does_not_cancel_watchdog():
    robot = FakeRobot()
    service = ExecutorService(ExecutorSettings(allow_motion=True), robot)
    waiter = asyncio.create_task(service.execute(await request_for(service, duration_s=0.16)))
    await asyncio.sleep(0.03)
    pulse = service.active
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert pulse is not None and not pulse.done()
    result = await pulse
    assert result["status"] == "completed"
    assert robot.commands[-1][0] == "robot.stop"


@pytest.mark.asyncio
async def test_stop_fences_delayed_action_and_cancels_current_pulse():
    robot = FakeRobot()
    service = ExecutorService(ExecutorSettings(allow_motion=True), robot)
    old = await request_for(service, "late")
    running = asyncio.create_task(service.execute(await request_for(service, "active", duration_s=0.5)))
    await asyncio.sleep(0.025)
    stopped = await service.stop()
    result = await running
    assert result["status"] == "cancelled"
    assert stopped["epoch"] != old.epoch
    assert robot.commands[-1][0] == "robot.stop"
    with pytest.raises(HTTPException, match="stale control epoch"):
        await service.execute(old)


@pytest.mark.asyncio
async def test_stop_arriving_during_final_stop_is_not_lost():
    robot = FakeRobot()
    robot.stop_delay = 0.05
    service = ExecutorService(ExecutorSettings(allow_motion=True), robot)
    waiter = asyncio.create_task(service.execute(await request_for(service, duration_s=0.04)))
    await asyncio.sleep(0.06)
    await asyncio.gather(service.stop(), service.stop())
    result = await waiter
    assert result["status"] == "cancelled"
    assert service.active is None and not service.stopping
    assert robot.commands[-1][0] == "robot.stop"


@pytest.mark.asyncio
async def test_fall_mid_pulse_stops_without_auto_recovery():
    robot = FakeRobot()
    service = ExecutorService(ExecutorSettings(allow_motion=True), robot)
    waiter = asyncio.create_task(service.execute(await request_for(service, duration_s=0.3)))
    await asyncio.sleep(0.025)
    robot.fallen = True
    result = await waiter
    assert result["status"] == "failed"
    assert "healthy" in result["error"]
    assert [c[0] for c in robot.commands] == ["robot.move", "robot.stop"]


@pytest.mark.asyncio
async def test_no_overlapping_action_and_expired_state_ticket():
    robot = FakeRobot()
    service = ExecutorService(ExecutorSettings(allow_motion=True), robot)
    expired = await request_for(service, "expired")
    service.tickets[expired.state_token] = (service.epoch, time.monotonic() - 2)
    with pytest.raises(HTTPException, match="expired"):
        await service.execute(expired)
    first = asyncio.create_task(service.execute(await request_for(service, "first", duration_s=0.1)))
    await asyncio.sleep(0.02)
    with pytest.raises(HTTPException, match="another action"):
        await service.execute(await request_for(service, "second"))
    await first


@pytest.mark.asyncio
async def test_hanging_move_is_bounded_and_stop_failure_latches():
    robot = FakeRobot()
    robot.move_delay = 1.0
    service = ExecutorService(ExecutorSettings(allow_motion=True), robot)
    before = time.monotonic()
    result = await service.execute(await request_for(service, duration_s=0.06))
    assert time.monotonic() - before < 0.3
    assert result["status"] == "failed" and robot.commands[-1][0] == "robot.stop"
    robot.move_delay = 0
    robot.stop_failure = True
    result = await service.execute(await request_for(service, "stop-fails"))
    assert result["status"] == "failed" and service.fault
    with pytest.raises(HTTPException, match="latched"):
        await service.execute(await request_for(service, "after-failure"))


@pytest.mark.asyncio
async def test_executor_auth_readonly_and_nonfinite_validation():
    robot = FakeRobot()
    token = "x" * 32
    app = create_executor_app({"token": token}, robot=robot)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://executor") as client:
        assert (await client.get("/state")).status_code == 401
        headers = {"Authorization": "Bearer " + token}
        state = (await client.get("/state", headers=headers)).json()
        body = {"request_id": "readonly", "epoch": state["epoch"], "state_token": state["state_token"],
                "vx": 0.02, "duration_s": 0.1, "observation_age_s": 0.01}
        assert (await client.post("/action", headers=headers, json=body)).status_code == 403
        body["duration_s"] = 2.0
        assert (await client.post("/action", headers=headers, json=body)).status_code == 422
    assert robot.commands == []
    with pytest.raises(ValueError, match="non-loopback"):
        create_executor_app({"bind_host": "0.0.0.0"})


@pytest.mark.asyncio
async def test_onboard_tof_stops_new_obstacle_and_unknown_is_not_clear():
    robot = FakeRobot()
    service = ExecutorService(ExecutorSettings(allow_motion=True), robot)
    running = asyncio.create_task(service.execute(await request_for(service, duration_s=0.3)))
    await asyncio.sleep(0.025)
    robot.tof_range_mm = 200
    result = await running
    assert result["status"] == "failed" and "ToF obstacle" in result["error"]
    assert robot.commands[-1][0] == "robot.stop"
    robot.tof_range_mm = 4000
    robot.tof_status = 255
    before = len(robot.commands)
    result = await service.execute(await request_for(service, "unknown-tof"))
    assert result["status"] == "failed" and "unknown" in result["error"]
    assert [r[0] for r in robot.commands[before:]] == ["robot.stop"]


@pytest.mark.asyncio
async def test_reverse_lateral_and_mixed_twists_are_rejected():
    service = ExecutorService(ExecutorSettings(allow_motion=True), FakeRobot())
    for i, changes in enumerate(({"vx": -0.02}, {"vy": 0.02}, {"vx": 0.02, "vyaw": 0.1})):
        action = (await request_for(service, f"shape-{i}")).model_copy(update=changes)
        with pytest.raises(HTTPException, match="forward-only or yaw-only"):
            await service.execute(action)


@pytest.mark.asyncio
async def test_perception_expiry_bounds_pulse_while_gateway_is_disconnected():
    robot = FakeRobot()
    service = ExecutorService(ExecutorSettings(allow_motion=True), robot)
    action = (await request_for(service, duration_s=0.4)).model_copy(update={"observation_age_s": 0.92})
    before = time.monotonic()
    result = await service.execute(action)
    assert time.monotonic() - before < 0.2
    assert result["status"] == "failed" and "perception expired" in result["error"]
    assert robot.commands[-1][0] == "robot.stop"


@pytest.mark.asyncio
async def test_media_frame_reads_binary_tail_and_retains_rotation(tmp_path):
    path = str(tmp_path / "media.sock")
    pixels = bytes([128, 20, 128, 40])
    tasks = set()

    async def handler(reader, writer):
        tasks.add(asyncio.current_task())
        request = json.loads(await reader.readline())
        header = {"width": 2, "height": 1, "format": "UYVY", "bytes": 4,
                  "captured_at_unix_us": int(time.time() * 1e6), "rotate": 180}
        writer.write(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": header}).encode() + b"\n" + pixels)
        await writer.drain()
        writer.close()
        await writer.wait_closed()
        tasks.discard(asyncio.current_task())

    server = await asyncio.start_unix_server(handler, path)
    try:
        header, got = await RobotdClient(media_socket=path).media_frame()
        assert got == pixels and header["rotate"] == 180
        assert 0 <= header["age_s"] < 0.1
    finally:
        server.close()
        await server.wait_closed()
        await asyncio.gather(*tasks, return_exceptions=True)
