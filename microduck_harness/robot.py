"""Strict adapters for native Microduck daemon sockets; never opens the servo bus.

Audited against pollen-robotics/microduck daemon-v0.16.3 and daemon-v0.17.8.
``robot.state`` is a notification produced by ``robot.subscribe``, not an RPC
request method. A real ``hello`` exchange checks the running binary before
subscription and on each connection that could send a movement command.
"""
from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import math
import re
import time
from typing import Any


class RobotError(RuntimeError):
    """Unavailable daemon, invalid measurement, or rejected intent."""


# These are release-specific wire versions, not a minimum-version guess. The
# native structs used below are identical in these two audited source tags.
SUPPORTED_FIRMWARE = {"0.16.3": 41, "0.17.8": 44}
FIRMWARE_REVISIONS = {
    "0.16.3": "0b2b82f56ac1e32d69018837ac1cd7d4c687f5a0",
    "0.17.8": "942e58e3aa6dbb28e7037203fff11866122c7567",
}


def inspect_firmware(hello: dict, expected_version: str = "auto") -> dict:
    """Interpret self-reported native identity; never infer a missing version.

    ``revision`` can legitimately be null or custom for a local build. Record
    that limit visibly; required wire fields are still validated at runtime.
    This is a compatibility check, not cryptographic binary attestation.
    """
    if expected_version not in {"auto", *SUPPORTED_FIRMWARE}:
        raise ValueError("robotd_version must be auto, 0.16.3, or 0.17.8")
    version, api, revision = (hello.get(k) for k in ("daemon_version", "api_version", "revision"))
    version_match = (re.fullmatch(r"(0\.(?:16\.3|17\.8))(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?", version)
                     if isinstance(version, str) else None)
    profile_version = version_match.group(1) if version_match else None
    reasons: list[str] = []
    warnings: list[str] = []
    if profile_version is None:
        reasons.append("unsupported_or_missing_daemon_version")
    if isinstance(api, bool) or not isinstance(api, int):
        reasons.append("invalid_api_version")
    elif profile_version is not None and api != SUPPORTED_FIRMWARE[profile_version]:
        reasons.append("daemon_version_api_mismatch")
    if expected_version != "auto" and profile_version != expected_version:
        reasons.append("configured_daemon_version_mismatch")
    if profile_version is not None and version != profile_version:
        warnings.append("custom_version_build_metadata; source_build_not_hardware_validated")
    revision_matches = None
    if revision is None:
        warnings.append("source_revision_unknown; version_and_api_only")
    elif not isinstance(revision, str):
        reasons.append("invalid_source_revision")
    elif profile_version is not None:
        # CI may embed a full or an unambiguous abbreviated Git SHA.
        revision_matches = (7 <= len(revision) <= 40
                            and FIRMWARE_REVISIONS[profile_version].startswith(revision.lower()))
        if not revision_matches:
            warnings.append("custom_source_revision; required_runtime_fields_still_enforced")
    return {
        "expected_version": expected_version,
        "daemon_version": version,
        "profile_version": profile_version,
        "api_version": api,
        "revision": revision,
        "source_revision_matches_tag": revision_matches,
        "compatible": not reasons,
        "reasons": reasons,
        "warnings": warnings,
        "supported_versions": list(SUPPORTED_FIRMWARE),
        "identity_source": "native_hello",
    }


def _number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RobotError(f"{name} is not numeric")
    value = float(value)
    if not math.isfinite(value):
        raise RobotError(f"{name} is not finite")
    return value


def _vector(value: Any, n: int, name: str) -> list[float]:
    if not isinstance(value, list) or len(value) != n:
        raise RobotError(f"{name} must contain {n} values")
    return [_number(v, name) for v in value]


def normalize_state(raw: dict, health: dict, *, monotonic_ns: int | None = None,
                    wall_time: float | None = None) -> dict:
    """Normalize measured state without treating absent telemetry as healthy.

    This runs on the same Linux board as robotd. Its t_ns uses CLOCK_MONOTONIC,
    so age can be checked without clock synchronization with the workstation.
    """
    now_ns = time.monotonic_ns() if monotonic_ns is None else monotonic_ns
    now_wall = time.time() if wall_time is None else wall_time
    t_ns = raw.get("t_ns")
    if isinstance(t_ns, bool) or not isinstance(t_ns, int) or t_ns <= 0:
        raise RobotError("robot.state has no valid CLOCK_MONOTONIC t_ns")
    age = (now_ns - t_ns) / 1e9
    if age < -0.05:
        raise RobotError("robot.state timestamp is in the future / wrong clock domain")
    age = max(0.0, age)
    joints = _vector(raw.get("joints"), 15, "joints (full 15-servo robot required)")
    imu = raw.get("imu")
    safety = raw.get("safety")
    if not isinstance(imu, dict) or not isinstance(safety, dict):
        raise RobotError("robot.state lacks imu or safety measurements")
    quat = _vector(imu.get("quat"), 4, "imu.quat")
    norm = math.sqrt(sum(v * v for v in quat))
    if not 0.90 <= norm <= 1.10:
        raise RobotError("imu.quat is not a unit quaternion")
    w, x, y, z = [v / norm for v in quat]
    roll = math.atan2(2 * (w * x + y * z), 1 - 2 * (x * x + y * y))
    pitch = math.asin(max(-1.0, min(1.0, 2 * (w * y - z * x))))
    gravity = _vector(safety.get("gravity"), 3, "safety.gravity")
    gravity_norm = math.sqrt(sum(v * v for v in gravity))
    policy = raw.get("policy")
    if not isinstance(policy, str):
        raise RobotError("robot.state has no policy label")
    reasons: list[str] = []
    if age > 0.15:
        reasons.append("stale_robot_state")
    if health.get("healthy") is not True:
        reasons.append("robot_health_not_healthy")
    loop = health.get("control_loop")
    if (not isinstance(loop, dict) or isinstance(loop.get("last_tick_age_ms"), bool)
        or not isinstance(loop.get("last_tick_age_ms"), int) or loop["last_tick_age_ms"] < 0):
        reasons.append("missing_loop_health")
    elif loop["last_tick_age_ms"] > 150:
        reasons.append("stale_control_loop")
    imu_health = health.get("imu")
    if not isinstance(imu_health, dict) or imu_health.get("ready") is not True:
        reasons.append("imu_not_ready")
    elif (("missing" in imu_health and imu_health["missing"] is not False)
          or isinstance(imu_health.get("consecutive_stale_blocks"), bool)
          or not isinstance(imu_health.get("consecutive_stale_blocks"), int)
          or not 0 <= imu_health["consecutive_stale_blocks"] < 25):
        reasons.append("imu_missing_or_frozen")
    if not 0.8 <= gravity_norm <= 1.2:
        reasons.append("invalid_gravity")
    for flag in ("fallen", "limp", "picked_up"):
        if safety.get(flag) is not False:
            reasons.append(f"safety_{flag}_or_unknown")
    bus = health.get("bus")
    if (not isinstance(bus, dict) or type(bus.get("consecutive_errors")) is not int
        or bus["consecutive_errors"] != 0 or ("missing" in bus and bus["missing"] != [])):
        reasons.append("bus_errors_or_unknown")
    if not isinstance(raw.get("loop"), dict) or _number(raw["loop"].get("hz"), "loop.hz") < 35:
        reasons.append("control_rate_low")
    return {
        "observed_at": now_wall - age,
        "state_age_s": age,
        "enabled": policy in {"walk", "stand"},
        "enabled_semantics": "policy_is_actively_driving_this_tick",
        "healthy": not reasons,
        "health_reasons": reasons,
        "roll": roll,
        "pitch": pitch,
        "policy": policy,
        "head": joints[5:9],
        "measured_head": joints[5:9],
        "frames": raw.get("frames"),
        "clearance_m": None,
        "raw": raw,
        "health": health,
    }


class RobotdClient:
    def __init__(self, socket_path: str = "/run/robotd.sock", *, timeout_s: float = 0.3,
                 media_socket: str = "/run/mediad/media.sock",
                 tof_socket: str = "/run/tofd/tof.sock", robotd_version: str = "auto") -> None:
        self.socket_path = socket_path
        self.media_socket = media_socket
        self.tof_socket = tof_socket
        self.timeout_s = timeout_s
        if robotd_version not in {"auto", *SUPPORTED_FIRMWARE}:
            raise ValueError("robotd_version must be auto, 0.16.3, or 0.17.8")
        self.robotd_version = robotd_version
        self._firmware = inspect_firmware({}, robotd_version)
        self._firmware["identity_source"] = "not_yet_checked"
        self._stream_identity: tuple | None = None
        self._ids = itertools.count(1)
        self._subscriber: asyncio.Task | None = None
        self._tof_subscriber: asyncio.Task | None = None
        self._latest_tof: dict | None = None
        self._tof_ready = asyncio.Event()
        self._tof_error = "ToF subscription has not started"
        self._latest: dict | None = None
        self._health: dict | None = None
        self._health_at = 0.0
        self._health_lock = asyncio.Lock()
        self._ready = asyncio.Event()
        self._error = "state subscription has not started"
        self._closed = False

    @staticmethod
    async def _close(writer: asyncio.StreamWriter | None) -> None:
        if writer:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def _send(self, writer: asyncio.StreamWriter, method: str, params: dict) -> int:
        request_id = next(self._ids)
        data = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        writer.write(json.dumps(data, allow_nan=False, separators=(",", ":")).encode() + b"\n")
        await writer.drain()
        return request_id

    @staticmethod
    async def _read(reader: asyncio.StreamReader) -> dict:
        line = await reader.readline()
        if not line:
            raise RobotError("daemon closed the socket")
        try:
            answer = json.loads(line)
        except (ValueError, UnicodeError) as exc:
            raise RobotError("daemon returned malformed JSON") from exc
        if not isinstance(answer, dict) or answer.get("jsonrpc") != "2.0":
            raise RobotError("invalid JSON-RPC envelope")
        return answer

    @staticmethod
    def _result(answer: dict, request_id: int) -> dict:
        if answer.get("id") != request_id:
            raise RobotError("JSON-RPC response id mismatch")
        if "error" in answer:
            raise RobotError(f"daemon RPC error: {answer['error']}")
        result = answer.get("result")
        if not isinstance(result, dict):
            raise RobotError("daemon result is not an object")
        if result.get("accepted") is False:
            raise RobotError(f"daemon refused intent: {result.get('reason', 'unspecified')}")
        return result

    @property
    def firmware_status(self) -> dict:
        return {**self._firmware}

    @staticmethod
    def _identity(firmware: dict) -> tuple:
        return tuple(firmware.get(k) for k in ("daemon_version", "api_version", "revision"))

    async def _handshake(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> dict:
        # Both audited daemons return their actual identity even when the
        # caller's api_version differs; they gate individual methods instead.
        offered_api = SUPPORTED_FIRMWARE.get(self.robotd_version, min(SUPPORTED_FIRMWARE.values()))
        try:
            request_id = await self._send(writer, "hello", {"api_version": offered_api})
            result = self._result(await self._read(reader), request_id)
            self._firmware = inspect_firmware(result, self.robotd_version)
        except Exception:
            self._firmware = inspect_firmware({}, self.robotd_version)
            self._firmware["reasons"].append("native_hello_unavailable")
            raise
        return self._firmware

    async def call(self, method: str, params: dict | None = None) -> dict:
        writer = None
        try:
            async with asyncio.timeout(self.timeout_s):
                reader, writer = await asyncio.open_unix_connection(self.socket_path, limit=1024 * 1024)
                if method == "robot.move":
                    firmware = await self._handshake(reader, writer)
                    if not firmware["compatible"]:
                        raise RobotError("robotd firmware incompatible: " + ",".join(firmware["reasons"]))
                    if (self._stream_identity is None or not self._ready.is_set()
                        or self._identity(firmware) != self._stream_identity):
                        raise RobotError("robotd identity changed or has no verified state subscription")
                    if self._latest is None or self._health is None:
                        raise RobotError("robotd has no measured state and health for movement")
                    checked = normalize_state(self._latest, self._health)
                    if not checked["healthy"] or not checked["enabled"]:
                        raise RobotError("robotd state became unsafe or stale during native handshake")
                request_id = await self._send(writer, method, params or {})
                return self._result(await self._read(reader), request_id)
        except (OSError, TimeoutError, ValueError) as exc:
            raise RobotError(f"{method}: daemon unavailable or timed out") from exc
        finally:
            await self._close(writer)

    async def _stream(self) -> None:
        while not self._closed:
            writer = None
            try:
                async with asyncio.timeout(self.timeout_s):
                    reader, writer = await asyncio.open_unix_connection(self.socket_path, limit=1024 * 1024)
                    firmware = await self._handshake(reader, writer)
                    self._stream_identity = self._identity(firmware)
                    request_id = await self._send(writer, "robot.subscribe", {"hz": 20})
                    subscribed = self._result(await self._read(reader), request_id)
                    if subscribed.get("accepted") is not True:
                        raise RobotError("robot.subscribe was not accepted")
                while not self._closed:
                    async with asyncio.timeout(self.timeout_s):
                        event = await self._read(reader)
                    if event.get("method") == "robot.state" and isinstance(event.get("params"), dict):
                        self._latest = event["params"]
                        self._ready.set()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._latest = None
                self._stream_identity = None
                self._health = None
                self._ready.clear()
                self._error = str(exc)
                await asyncio.sleep(0.2)
            finally:
                await self._close(writer)

    async def state(self) -> dict:
        if self._closed:
            raise RobotError("robot client is closed")
        if self._subscriber is None:
            self._subscriber = asyncio.create_task(self._stream())
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=self.timeout_s + 0.1)
        except TimeoutError as exc:
            raise RobotError(f"no fresh robot.state: {self._error}") from exc
        async with self._health_lock:
            if self._health is None or time.monotonic() - self._health_at >= 0.4:
                self._health = await self.call("robot.health")
                self._health_at = time.monotonic()
        if self._latest is None:
            raise RobotError("robot state stream disconnected")
        state = normalize_state(self._latest, self._health)
        firmware = self.firmware_status
        state["firmware"] = firmware
        if not firmware["compatible"]:
            state["healthy"] = False
            state["health_reasons"].extend("firmware_" + r for r in firmware["reasons"])
        elif self._identity(firmware) != self._stream_identity:
            state["healthy"] = False
            state["health_reasons"].append("firmware_state_subscription_identity_mismatch")
        return state

    async def media_frame(self) -> tuple[dict, bytes]:
        """Native UYVY bytes, untouched rotation, and capture age measured on board."""
        writer = None
        try:
            async with asyncio.timeout(1.0):
                reader, writer = await asyncio.open_unix_connection(self.media_socket, limit=1024 * 1024)
                request_id = await self._send(writer, "media.frame", {})
                header = self._result(await self._read(reader), request_id)
                width, height, size = (header.get(k) for k in ("width", "height", "bytes"))
                if (not all(isinstance(v, int) and not isinstance(v, bool) and v > 0 for v in (width, height, size))
                    or width % 2 or size != width * height * 2 or size > 16 * 1024 * 1024
                    or header.get("format") != "UYVY" or header.get("rotate") not in (0, 90, 180, 270)):
                    raise RobotError("invalid mediad UYVY frame geometry")
                captured = _number(header.get("captured_at_unix_us"), "captured_at_unix_us") / 1e6
                data = await reader.readexactly(size)
                age = time.time() - captured
                if age < -0.05:
                    raise RobotError("mediad capture timestamp is in the future")
                header["age_s"] = max(0.0, age)
                return header, data
        except (OSError, TimeoutError, asyncio.IncompleteReadError) as exc:
            raise RobotError("mediad frame unavailable") from exc
        finally:
            await self._close(writer)

    async def _stream_tof(self) -> None:
        while not self._closed:
            writer = None
            try:
                async with asyncio.timeout(self.timeout_s):
                    reader, writer = await asyncio.open_unix_connection(self.tof_socket, limit=1024 * 1024)
                    request_id = await self._send(writer, "tof.stream", {})
                    result = self._result(await self._read(reader), request_id)
                    sensor = result.get("sensor")
                    if (result.get("accepted") is not True or not isinstance(sensor, str)
                        or not sensor.strip()):
                        raise RobotError("no measured ToF sensor is available")
                    if sensor.strip().lower() in {"fake", "sim"}:
                        raise RobotError("synthetic ToF sensor is forbidden for the live executor")
                while not self._closed:
                    async with asyncio.timeout(self.timeout_s):
                        event = await self._read(reader)
                    if event.get("method") == "tof.frame" and isinstance(event.get("params"), dict):
                        self._latest_tof = event["params"]
                        self._tof_ready.set()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._latest_tof = None
                self._tof_ready.clear()
                self._tof_error = str(exc)
                await asyncio.sleep(0.2)
            finally:
                await self._close(writer)

    async def tof_frame(self) -> dict:
        if self._closed:
            raise RobotError("robot client is closed")
        if self._tof_subscriber is None:
            self._tof_subscriber = asyncio.create_task(self._stream_tof())
        try:
            await asyncio.wait_for(self._tof_ready.wait(), timeout=self.timeout_s + 0.1)
        except TimeoutError as exc:
            raise RobotError(f"ToF frame unavailable: {self._tof_error}") from exc
        frame = self._latest_tof
        if frame is None:
            raise RobotError("ToF stream disconnected")
        t_ns = frame.get("t_ns")
        if isinstance(t_ns, bool) or not isinstance(t_ns, int) or t_ns <= 0:
            raise RobotError("ToF frame lacks a monotonic timestamp")
        age = (time.monotonic_ns() - t_ns) / 1e9
        if age < -0.05 or age > 0.5:
            raise RobotError("ToF frame is stale or has an invalid clock")
        return {"frame": frame, "age_s": max(0.0, age)}

    async def aclose(self) -> None:
        self._closed = True
        for subscriber in (self._subscriber, self._tof_subscriber):
            if not subscriber:
                continue
            subscriber.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await subscriber
