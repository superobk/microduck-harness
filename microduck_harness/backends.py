"""A deterministic kinematic test world and real HTTP service adapters."""
from __future__ import annotations
import asyncio
import math
import time
import uuid

import httpx
from .models import Action, Observation
from .config import Settings


def wrap(angle: float) -> float:
    return math.atan2(math.sin(angle), math.cos(angle))


class DemoWorld:
    """2D software test fixture, explicitly not a dynamics or robot-policy simulator."""
    def __init__(self, settings: Settings, realtime: bool = True):
        self.settings = settings
        self.x = self.y = self.yaw = 0.0
        self.epoch = 0
        self.seq = 0
        self.realtime = realtime
        self.actions: list[dict] = []
        self.objects = [
            {"id": "paper_1", "label": "paper", "x": 0.70, "y": 0.18},
            {"id": "marker_1", "label": "marker_1", "x": -0.55, "y": 0.80},
        ]
        self.obstacles = [{"x": 0.65, "y": -0.65, "radius": 0.18}]

    async def state(self):
        return {"observed_at": time.time(), "enabled": True, "healthy": True,
                "roll": 0.0, "pitch": 0.0, "epoch": self.epoch,
                "servo_count": 15, "backend": "kinematic-demo", "policy": "simulated"}

    def clearance(self):
        x0, x1, y0, y1 = self.settings.bounds
        # Three rays cover a forward cone. Point robot approximation is explicit.
        smallest = 5.0
        for offset in (-0.25, 0, 0.25):
            angle = self.yaw + offset
            for i in range(1, 501):
                d = i * 0.01
                x, y = self.x + math.cos(angle)*d, self.y + math.sin(angle)*d
                if not x0 < x < x1 or not y0 < y < y1 or any(
                    math.hypot(x-o["x"], y-o["y"]) < o["radius"] for o in self.obstacles
                ):
                    smallest = min(smallest, d)
                    break
        return smallest

    async def observe(self):
        self.seq += 1
        detections = []
        for obj in self.objects:
            dx, dy = obj["x"]-self.x, obj["y"]-self.y
            bearing = wrap(math.atan2(dy, dx)-self.yaw)
            distance = math.hypot(dx, dy)
            if abs(bearing) < 0.67 and distance < 2.0:
                detections.append({"id": obj["id"], "label": obj["label"], "confidence": 0.98,
                                   "bearing_rad": bearing, "range_m": distance})
        return Observation(frame_id=f"demo-{self.seq}", captured_at=time.time(), source="kinematic-demo",
                           pose={"x": self.x, "y": self.y, "yaw": self.yaw}, pose_valid=True,
                           clearance_m=self.clearance(), detections=detections,
                           notes=["Synthetic observations; no real camera, motor dynamics or model inference."])

    async def execute(self, action: Action, observation: Observation, request_id: str):
        epoch = self.epoch
        self.actions.append(action.model_dump())
        steps = 10
        for _ in range(steps):
            await asyncio.sleep(action.duration_s/steps if self.realtime else 0)
            if self.epoch != epoch:
                raise RuntimeError("action cancelled by stop epoch")
            dt = action.duration_s/steps
            self.x += (action.vx*math.cos(self.yaw)-action.vy*math.sin(self.yaw))*dt
            self.y += (action.vx*math.sin(self.yaw)+action.vy*math.cos(self.yaw))*dt
            self.yaw = wrap(self.yaw+action.vyaw*dt)
        return {"request_id": request_id, "status": "completed", "verified_success": False,
                "backend": "kinematic-demo", "epoch": self.epoch}

    async def stop(self):
        self.epoch += 1
        return {"stopped_walk": True, "epoch": self.epoch, "backend": "kinematic-demo"}

    async def close(self):
        await self.stop()

    def scene(self):
        return {"pose": {"x": self.x, "y": self.y, "yaw": self.yaw}, "objects": self.objects,
                "obstacles": self.obstacles, "bounds": self.settings.bounds, "synthetic": True}


class LiveBackend:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None):
        self.settings = settings
        self.client = client or httpx.AsyncClient(timeout=3, trust_env=False, follow_redirects=False)
        self.owned = client is None

    async def _request(self, service: str, method: str, path: str, body: dict | None = None):
        url = getattr(self.settings, service+"_url").rstrip("/") + path
        token = getattr(self.settings, service+"_token")
        try:
            response = await self.client.request(method, url, json=body,
                                                  headers={"Authorization": "Bearer "+token})
            response.raise_for_status()
            return response.json()
        except httpx.HTTPError as exc:
            code = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else "transport"
            raise RuntimeError(f"{service} {path} failed ({code}); action was not retried") from None

    async def state(self):
        return await self._request("executor", "GET", "/state")

    async def observe(self):
        return Observation.model_validate(await self._request("vision", "GET", "/observe"))

    async def execute(self, action: Action, observation: Observation, request_id: str):
        # Get a fresh board-issued token AFTER planning/revalidation, never before an LLM call.
        state = await self.state()
        age = time.time()-observation.captured_at
        if age < -0.25 or age > self.settings.max_observation_age_s:
            raise RuntimeError("observation expired before board dispatch")
        payload = {"request_id": request_id, "epoch": state["epoch"], "state_token": state["state_token"],
                   "vx": action.vx, "vy": action.vy, "vyaw": action.vyaw,
                   "duration_s": action.duration_s, "observation_age_s": max(0, age)}
        return await self._request("executor", "POST", "/action", payload)

    async def stop(self):
        return await self._request("executor", "POST", "/stop", {})

    async def close(self):
        if self.owned:
            await self.client.aclose()

    def scene(self):
        return {"synthetic": False, "bounds": self.settings.bounds}

