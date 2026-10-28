"""Board-local bounded locomotion executor. Does not enable torque or run skills."""
from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import hmac
import ipaddress
import math
import os
import secrets
import time
from contextlib import asynccontextmanager
from typing import Any
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from .robot import RobotError, RobotdClient, SUPPORTED_FIRMWARE
from .plugins import NativePluginInspector, PluginPaths, unavailable as unavailable_plugins
from . import __version__


@dataclasses.dataclass(frozen=True)
class ExecutorSettings:
    socket_path: str = "/run/robotd.sock"
    media_socket: str = "/run/mediad/media.sock"
    tof_socket: str = "/run/tofd/tof.sock"
    robotd_version: str = "auto"
    token: str = ""
    bind_host: str = "127.0.0.1"
    allow_motion: bool = False
    require_tof: bool = True
    require_native_plugins: bool = False
    native_config_path: str = "/etc/robot/robotd.toml"
    calibration_runtime_path: str = "/run/robotd/calibration-effective.json"
    camera_runtime_path: str = "/run/mediad/camera-effective.json"
    max_linear_m_s: float = 0.08
    max_yaw_rad_s: float = 0.5
    max_duration_s: float = 1.0
    max_observation_age_s: float = 1.0
    max_tilt_rad: float = 0.45
    refresh_s: float = 0.05

    @classmethod
    def from_env(cls) -> "ExecutorSettings":
        native_required = os.getenv("EXECUTOR_REQUIRE_NATIVE_PLUGINS", "0")
        if native_required not in {"0", "1"}:
            raise ValueError("EXECUTOR_REQUIRE_NATIVE_PLUGINS must be 0 or 1")
        return cls(
            socket_path=os.getenv("ROBOTD_SOCKET", "/run/robotd.sock"),
            media_socket=os.getenv("MEDIAD_SOCKET", "/run/mediad/media.sock"),
            tof_socket=os.getenv("TOFD_SOCKET", "/run/tofd/tof.sock"),
            robotd_version=os.getenv("EXECUTOR_ROBOTD_VERSION", "auto"),
            token=os.getenv("EXECUTOR_TOKEN", ""),
            bind_host=os.getenv("EXECUTOR_HOST", "127.0.0.1"),
            allow_motion=os.getenv("EXECUTOR_ALLOW_MOTION", "0") == "1",
            require_native_plugins=native_required == "1",
            native_config_path=os.getenv("NATIVE_CONFIG_PATH", "/etc/robot/robotd.toml"),
            calibration_runtime_path=os.getenv("CALIBRATION_RUNTIME_PATH", "/run/robotd/calibration-effective.json"),
            camera_runtime_path=os.getenv("CAMERA_RUNTIME_PATH", "/run/mediad/camera-effective.json"),
        )

    def validate(self) -> None:
        if self.robotd_version not in {"auto", *SUPPORTED_FIRMWARE}:
            raise ValueError("EXECUTOR_ROBOTD_VERSION must be auto, 0.16.3, or 0.17.8")
        if type(self.require_native_plugins) is not bool:
            raise ValueError("require_native_plugins must be boolean")
        for path in (self.native_config_path, self.calibration_runtime_path, self.camera_runtime_path):
            if not isinstance(path, str) or not Path(path).is_absolute() or any(ord(c) < 32 for c in path):
                raise ValueError("native plugin paths must be absolute local paths")
        try:
            loopback = ipaddress.ip_address(self.bind_host).is_loopback
        except ValueError:
            loopback = self.bind_host == "localhost"
        if not loopback and len(self.token) < 24:
            raise ValueError("a non-loopback executor requires EXECUTOR_TOKEN with >=24 characters")
        limits = (
            (self.max_linear_m_s, 0.08, "linear speed"),
            (self.max_yaw_rad_s, 0.5, "yaw speed"),
            (self.max_duration_s, 1.0, "pulse duration"),
            (self.max_observation_age_s, 1.0, "observation age"),
            (self.max_tilt_rad, 0.6, "tilt"),
            (self.refresh_s, 0.1, "refresh interval"),
        )
        for value, maximum, name in limits:
            if not math.isfinite(value) or not 0 < value <= maximum:
                raise ValueError(f"invalid {name} limit")


class ActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    request_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    epoch: int = Field(ge=0, strict=True)
    state_token: str = Field(min_length=8, max_length=128)
    vx: float = Field(default=0.0, strict=True)
    vy: float = Field(default=0.0, strict=True)
    vyaw: float = Field(default=0.0, strict=True)
    duration_s: float = Field(gt=0, le=1, strict=True)
    observation_age_s: float = Field(ge=0, le=1, strict=True)


class ExecutorService:
    """One action owner, short state tickets, epoch fencing, independent deadlines."""

    def __init__(self, settings: ExecutorSettings, robot: RobotdClient) -> None:
        self.settings = settings
        self.robot = robot
        self.epoch = secrets.randbits(48)
        self.active: asyncio.Task | None = None
        self.active_id: str | None = None
        self.lock = asyncio.Lock()
        self.stop_lock = asyncio.Lock()
        self.stopping = False
        self.fault: str | None = None
        self.tickets: dict[str, tuple[int, float]] = {}
        self.ticket_plugin_generations: dict[str, str] = {}
        self.requests: dict[str, tuple[dict, asyncio.Task]] = {}
        self.plugins = NativePluginInspector(PluginPaths(
            settings.native_config_path, settings.calibration_runtime_path, settings.camera_runtime_path,
        ), required=settings.require_native_plugins, robotd_version=settings.robotd_version)

    async def plugin_status(self, firmware: dict | None = None) -> dict:
        identity = firmware if firmware is not None else getattr(self.robot, "firmware_status", None)
        try:
            return await asyncio.wait_for(asyncio.to_thread(self.plugins.inspect, identity), timeout=.25)
        except TimeoutError:
            return unavailable_plugins(self.settings.require_native_plugins, "native plugin verification exceeded its local time budget")

    async def _state_with_plugins(self, expected_generation: str | None = None) -> dict:
        state = await self.robot.state()
        started = time.monotonic()
        plugins = await self.plugin_status(state.get("firmware"))
        state = {**state, "plugins": plugins}
        age = state.get("state_age_s")
        if type(age) in (int, float) and math.isfinite(age):
            state["state_age_s"] = age + time.monotonic() - started
        reasons = list(state.get("health_reasons", []))
        if self.settings.require_native_plugins:
            if not plugins["ready"]:
                state["healthy"] = False
                reasons.append("native_plugins_not_active")
            elif expected_generation is not None and plugins["generation"] != expected_generation:
                state["healthy"] = False
                reasons.append("native_plugin_generation_changed")
        if state.get("state_age_s", float("inf")) > .15:
            state["healthy"] = False
            reasons.append("robot_state_aged_during_plugin_verification")
        state["health_reasons"] = reasons
        return state

    def _check_state(self, state: dict) -> None:
        if state.get("healthy") is not True:
            raise RobotError("robot not healthy: " + ",".join(state.get("health_reasons", [])))
        if state.get("enabled") is not True or state.get("policy") not in {"walk", "stand"}:
            raise RobotError("robot policy is not actively walking/standing; no auto-enable is performed")
        for key in ("roll", "pitch"):
            value = state.get(key)
            if not isinstance(value, (int, float)) or not math.isfinite(value) or abs(value) > self.settings.max_tilt_rad:
                raise RobotError(f"robot {key} exceeds the configured locomotion envelope")
        age = state.get("state_age_s")
        if not isinstance(age, (int, float)) or not math.isfinite(age) or not 0 <= age <= 0.15:
            raise RobotError("robot state is missing or stale")

    def _check_tof(self, state: dict, sample: dict, action: ActionRequest, remaining_s: float) -> None:
        """Conservative local near-field guard, not a 360-degree collision model."""
        age = sample.get("age_s")
        if isinstance(age, bool) or not isinstance(age, (int, float)) or not math.isfinite(age) or not 0 <= age <= 0.2:
            raise RobotError("ToF is missing or stale")
        frame = sample.get("frame", {})
        raw = state.get("raw", {})
        if (not isinstance(frame.get("t_ns"), int) or not isinstance(raw.get("t_ns"), int)
            or abs(frame["t_ns"] - raw["t_ns"]) > 200_000_000):
            raise RobotError("ToF and robot pose timestamps do not match")
        distances, statuses = frame.get("distance_mm"), frame.get("status")
        if (frame.get("rows") != 8 or frame.get("cols") != 8
            or not isinstance(distances, list) or len(distances) != 64
            or not isinstance(statuses, list) or len(statuses) != 64):
            raise RobotError("ToF must provide a measured 8x8 grid")
        frames = raw.get("frames")
        quat = frames.get("tof", {}).get("quat") if isinstance(frames, dict) else None
        if (not isinstance(quat, list) or len(quat) != 4
            or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in quat)):
            raise RobotError("ToF sensor pose is unknown")
        norm = math.sqrt(sum(v * v for v in quat))
        if not 0.9 <= norm <= 1.1:
            raise RobotError("ToF sensor pose quaternion is invalid")
        _, _, y, z = [v / norm for v in quat]
        # Native ToF +x is its optical axis; require it within 20 degrees of trunk +x.
        if 1 - 2 * (y * y + z * z) < math.cos(0.35):
            raise RobotError("ToF sensor is not looking forwards")
        turning = abs(action.vyaw) > 0
        indices = range(64) if turning else [r * 8 + c for r in range(2, 6) for c in range(2, 6)]
        ranges = []
        for index in indices:
            distance, status = distances[index], statuses[index]
            if (isinstance(distance, bool) or not isinstance(distance, (int, float))
                or not math.isfinite(distance) or distance <= 0 or status not in (5, 9)):
                raise RobotError("ToF clearance is unknown; no-target is not open space")
            ranges.append(distance / 1000)
        required = 0.40 if turning else 0.30 + action.vx * max(0.0, remaining_s)
        if min(ranges) <= required:
            raise RobotError("onboard ToF obstacle inside the motion clearance envelope")

    async def snapshot(self) -> dict:
        state = await self._state_with_plugins()
        now = time.monotonic()
        async with self.lock:
            self.tickets = {k: v for k, v in self.tickets.items() if now - v[1] < self.settings.max_observation_age_s}
            self.ticket_plugin_generations = {k: v for k, v in self.ticket_plugin_generations.items() if k in self.tickets}
            if len(self.tickets) >= 256:
                self.tickets.pop(next(iter(self.tickets)))
            token = None
            if not self.settings.require_native_plugins or state["plugins"]["ready"]:
                token = secrets.token_urlsafe(24)
                self.tickets[token] = (self.epoch, now)
                if self.settings.require_native_plugins:
                    self.ticket_plugin_generations[token] = state["plugins"]["generation"]
            else:
                self.tickets.clear()
                self.ticket_plugin_generations.clear()
            return {
                **state,
                "epoch": self.epoch,
                "state_token": token,
                "state_token_ttl_s": self.settings.max_observation_age_s if token else 0,
                "allow_motion": self.settings.allow_motion,
                "active_request_id": self.active_id,
                "fault": self.fault,
            }

    async def execute(self, action: ActionRequest) -> dict:
        payload = action.model_dump()
        async with self.lock:
            previous = self.requests.get(action.request_id)
            if previous:
                if previous[0] != payload:
                    raise HTTPException(409, "request_id was already used with different parameters")
                task = previous[1]
            else:
                if not self.settings.allow_motion:
                    raise HTTPException(403, "executor is read-only; configure EXECUTOR_ALLOW_MOTION=1 locally")
                if self.fault:
                    raise HTTPException(503, f"executor latched a stop failure: {self.fault}")
                if self.stopping or self.active is not None:
                    raise HTTPException(409, "another action or stop is in progress")
                if action.epoch != self.epoch:
                    raise HTTPException(409, "stale control epoch")
                ticket = self.tickets.get(action.state_token)
                if ticket is None or ticket[0] != self.epoch:
                    raise HTTPException(409, "state_token is unknown, consumed, or fenced")
                plugin_generation = self.ticket_plugin_generations.get(action.state_token)
                if self.settings.require_native_plugins:
                    plugins = await self.plugin_status()
                    if not plugins["ready"]:
                        self.tickets.pop(action.state_token, None)
                        self.ticket_plugin_generations.pop(action.state_token, None)
                        raise HTTPException(409, "native plugins are not verified active")
                    if not plugin_generation or plugin_generation != plugins["generation"]:
                        self.tickets.pop(action.state_token, None)
                        self.ticket_plugin_generations.pop(action.state_token, None)
                        raise HTTPException(409, "native plugins changed after the state ticket was issued")
                ticket_age = time.monotonic() - ticket[1]
                if ticket_age > self.settings.max_observation_age_s:
                    raise HTTPException(409, "state_token expired on the board")
                if action.observation_age_s + ticket_age > self.settings.max_observation_age_s:
                    raise HTTPException(409, "perception is too old for a new action")
                if (math.hypot(action.vx, action.vy) > self.settings.max_linear_m_s
                    or abs(action.vyaw) > self.settings.max_yaw_rad_s
                    or action.duration_s > self.settings.max_duration_s):
                    raise HTTPException(422, "action exceeds the local motion envelope")
                if action.vx < 0 or action.vy != 0 or (action.vx > 0 and action.vyaw != 0):
                    raise HTTPException(422, "only forward-only or yaw-only pulses are supported")
                if len(self.requests) >= 10000:
                    raise HTTPException(503, "request journal is full; restart executor after stopping")
                self.tickets.pop(action.state_token)
                self.ticket_plugin_generations.pop(action.state_token, None)
                perception_budget = self.settings.max_observation_age_s - action.observation_age_s - ticket_age
                perception_deadline = ticket[1] + self.settings.max_observation_age_s - action.observation_age_s
                task = asyncio.create_task(self._pulse(action, perception_budget, plugin_generation, perception_deadline))
                self.requests[action.request_id] = (payload, task)
                self.active = task
                self.active_id = action.request_id
        # HTTP disconnect/cancellation must not cancel the on-board watchdog/final stop.
        return await asyncio.shield(task)

    async def _stop_native(self) -> dict:
        try:
            result = await asyncio.wait_for(self.robot.call("robot.stop", {}), timeout=0.35)
            if result.get("accepted") is not True:
                raise RobotError("robot.stop did not acknowledge acceptance")
            return {"accepted": True, "verified_physical_stop": False}
        except Exception as exc:
            self.fault = str(exc)
            return {"accepted": False, "verified_physical_stop": False, "error": str(exc)}

    async def _pulse(self, action: ActionRequest, perception_budget: float, plugin_generation: str | None = None,
                     perception_deadline: float | None = None) -> dict:
        started = time.monotonic()
        if perception_deadline is not None:
            perception_budget = min(perception_budget, perception_deadline - started)
        deadline = started + min(action.duration_s, perception_budget)
        status = "completed"
        error: str | None = None
        refreshes = 0
        try:
            if perception_budget <= 0:
                raise RobotError("perception expired before the pulse could start")
            while time.monotonic() < deadline:
                if action.epoch != self.epoch:
                    raise RobotError("action fenced by a stop")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                state = await asyncio.wait_for(self._state_with_plugins(plugin_generation), timeout=remaining)
                self._check_state(state)
                if self.settings.require_tof and (action.vx > 0 or action.vyaw != 0):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise RobotError("action deadline reached before ToF validation")
                    sample = await asyncio.wait_for(self.robot.tof_frame(), timeout=remaining)
                    self._check_tof(state, sample, action, remaining)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                if action.epoch != self.epoch:
                    raise RobotError("action fenced before refresh")
                result = await asyncio.wait_for(
                    self.robot.call("robot.move", {"vx": action.vx, "vy": action.vy, "vyaw": action.vyaw}),
                    timeout=remaining,
                )
                if result.get("accepted") is not True:
                    raise RobotError("robot.move was not accepted")
                refreshes += 1
                await asyncio.sleep(min(self.settings.refresh_s, max(0.0, deadline - time.monotonic())))
            if perception_budget < action.duration_s:
                status, error = "failed", "perception expired before the requested pulse finished"
        except asyncio.CancelledError:
            status, error = "cancelled", "stop requested"
        except TimeoutError:
            status, error = "failed", "native RPC exceeded the action deadline"
        except Exception as exc:
            status, error = "failed", str(exc)
        finally:
            # A stop may arrive while normal completion is already awaiting robot.stop.
            # Shield a separate cleanup task so that cancellation cannot skip this finalizer.
            cleanup = asyncio.create_task(self._finish_action(action.request_id))
            try:
                stop = await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                status, error = "cancelled", "stop requested during finalization"
                stop = await asyncio.shield(cleanup)
            if not stop["accepted"]:
                status, error = "failed", "stop acknowledgement failed: " + stop.get("error", "unknown")
        return {
            "request_id": action.request_id,
            "epoch": self.epoch,
            "status": status,
            "duration_s": time.monotonic() - started,
            "move_refreshes": refreshes,
            "stop": stop,
            "verified_success": False,
            "completion_semantics": "bounded_command_finished; perception_must_verify_goal",
            "error": error,
        }

    async def _finish_action(self, request_id: str) -> dict:
        stop = await self._stop_native()
        async with self.lock:
            if self.active_id == request_id:
                self.active = None
                self.active_id = None
        return stop

    async def stop(self) -> dict:
        async with self.stop_lock:
            async with self.lock:
                self.epoch += 1
                self.tickets.clear()
                self.ticket_plugin_generations.clear()
                self.stopping = True
                task = self.active
                if task and not task.done():
                    task.cancel()
            if task:
                with contextlib.suppress(asyncio.CancelledError):
                    await asyncio.shield(task)
            stop = await self._stop_native()
            async with self.lock:
                self.stopping = False
            return {"epoch": self.epoch, "stop": stop,
                    "scope": "zero_walk_velocity_only; does_not_abort_native_one_shot_skills"}

    async def close(self) -> None:
        # Do not send any commands merely because a read-only process was started/stopped.
        if self.active is not None:
            await self.stop()
        await self.robot.aclose()


def create_executor_app(settings: dict[str, Any] | ExecutorSettings | None = None,
                        robot: RobotdClient | None = None) -> FastAPI:
    cfg = ExecutorSettings.from_env() if settings is None else (
        settings if isinstance(settings, ExecutorSettings) else ExecutorSettings(**settings)
    )
    cfg.validate()
    service = ExecutorService(cfg, robot or RobotdClient(
        cfg.socket_path, media_socket=cfg.media_socket, tof_socket=cfg.tof_socket,
        robotd_version=cfg.robotd_version,
    ))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        await service.close()

    app = FastAPI(title="Microduck board executor", version=__version__, lifespan=lifespan)
    app.state.executor = service

    @app.middleware("http")
    async def authenticate(request: Request, call_next):
        if cfg.token and request.url.path != "/health":
            expected = "Bearer " + cfg.token
            supplied = request.headers.get("authorization", "")
            if not hmac.compare_digest(supplied, expected):
                return JSONResponse({"detail": "authentication required"}, status_code=401)
        return await call_next(request)

    @app.exception_handler(RobotError)
    async def unavailable(request: Request, exc: RobotError):
        return JSONResponse({"detail": str(exc)}, status_code=503)

    @app.get("/health")
    async def health():
        return {"service": "microduck-executor", "allow_motion": cfg.allow_motion,
                "require_tof": cfg.require_tof,
                "require_native_plugins": cfg.require_native_plugins,
                "robotd_version_configured": cfg.robotd_version,
                "firmware": getattr(service.robot, "firmware_status", None),
                "fault": service.fault, "robot_health": "read /state for measured status"}

    @app.get("/state")
    async def state():
        return await service.snapshot()

    @app.get("/plugins")
    async def plugins():
        return await service.plugin_status()

    @app.post("/action")
    async def action(body: ActionRequest):
        return await service.execute(body)

    @app.post("/stop")
    async def stop():
        # The stop continues even if the HTTP caller disconnects while waiting.
        return await asyncio.shield(service.stop())

    @app.get("/frame")
    async def frame():
        header, data = await service.robot.media_frame()
        return Response(data, media_type="application/octet-stream", headers={
            "Cache-Control": "no-store",
            "X-Frame-Captured-At": str(header["captured_at_unix_us"] / 1e6),
            "X-Frame-Id": str(header["captured_at_unix_us"]),
            "X-Frame-Age-S": str(header["age_s"]),
            "X-Frame-Width": str(header["width"]),
            "X-Frame-Height": str(header["height"]),
            "X-Frame-Format": header["format"],
            "X-Frame-Rotate": str(header["rotate"]),
        })

    @app.get("/tof")
    async def tof():
        return await service.robot.tof_frame()

    return app
