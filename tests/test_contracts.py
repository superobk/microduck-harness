"""Exercise actual board serialization, vision geometry, and live HTTP adapters together."""
from __future__ import annotations

import math
import time

import httpx
import pytest

from microduck_harness.backends import LiveBackend
from microduck_harness.config import Settings
from microduck_harness.executor import create_executor_app
from microduck_harness.models import Action
from microduck_harness.robot import normalize_state
from microduck_harness.vision import (
    Observation as VisionObservation,
    body_camera_transform,
    conservative_clearance,
    create_vision_app,
    decode_frame,
    marker_robot_pose,
    matmul,
    pose_transform,
    transform_point,
)


class ContractRobot:
    """Only the daemon byte boundary is faked; all public-service code is real."""
    def __init__(self):
        self.calls = []

    async def state(self):
        raw = {
            "t_ns": time.monotonic_ns(), "policy": "stand", "loop": {"hz": 50},
            "joints": [0.0] * 15,
            # Deliberately different: clients must select measured joints, not this intent.
            "head": [0.7] * 4,
            "imu": {"quat": [1, 0, 0, 0]},
            "safety": {"gravity": [0, 0, -1], "fallen": False, "limp": False, "picked_up": False},
            "frames": {
                # Native scalar-first quaternion: cv2 +z forward -> trunk +x.
                "camera": {"pos": [0.1, 0, 0.2], "quat": [0.5, -0.5, 0.5, -0.5]},
                "tof": {"pos": [0.08, 0, 0.18], "quat": [1, 0, 0, 0]},
            },
        }
        health = {"healthy": True, "imu": {"ready": True, "consecutive_stale_blocks": 0},
                  "bus": {"consecutive_errors": 0}, "control_loop": {"last_tick_age_ms": 1}}
        return normalize_state(raw, health)

    async def tof_frame(self):
        return {"age_s": 0.005, "frame": {"t_ns": time.monotonic_ns(), "rows": 8, "cols": 8,
                "distance_mm": [1000] * 64, "status": [5] * 64}}

    async def media_frame(self):
        # Two rows with visibly different luminance; camera mount metadata is180 degrees.
        body = bytes([128, 20, 128, 40, 128, 160, 128, 180])
        return {"captured_at_unix_us": int(time.time()*1e6), "age_s": 0.005,
                "width": 2, "height": 2, "format": "UYVY", "rotate": 180}, body

    async def call(self, method, params=None):
        self.calls.append((method, params))
        return {"accepted": True}

    async def aclose(self):
        pass


class ServiceTransport(httpx.AsyncBaseTransport):
    def __init__(self, executor, vision):
        self.transports = {"executor": httpx.ASGITransport(app=executor),
                           "vision": httpx.ASGITransport(app=vision)}

    async def handle_async_request(self, request):
        return await self.transports[request.url.host].handle_async_request(request)


@pytest.mark.asyncio
async def test_normalized_board_to_vision_to_live_backend_contract():
    pytest.importorskip("cv2")
    robot = ContractRobot()
    token = "contract-only-token-with-32-characters"
    executor = create_executor_app({"allow_motion": True, "token": token}, robot=robot)
    vision = create_vision_app({"token": token})
    settings = Settings(mode="live", decision_mode="hybrid", glm_base_url="http://private-glm/v1",
                        executor_url="http://executor", vision_url="http://vision",
                        gateway_token=token, executor_token=token, vision_token=token)
    async with httpx.AsyncClient(transport=ServiceTransport(executor, vision)) as client:
        backend = LiveBackend(settings, client=client)
        state = await backend.state()
        assert state["healthy"] and state["measured_head"] == [0.0] * 4
        assert state["raw"]["head"] != state["measured_head"]
        response = await client.get("http://executor/frame", headers={"Authorization": "Bearer "+token})
        assert response.status_code == 200
        captured_at = float(response.headers["x-frame-captured-at"])
        image = decode_frame(response.content, response.headers, {"use_robot_camera_fk": True})
        assert image.shape == (2, 2, 3)
        assert image[0, 0, 0] < image[1, 0, 0]  # Raw optical rows are not silently rotated.
        with pytest.raises(ValueError, match="rotated pixels"):
            decode_frame(response.content, response.headers, {"use_robot_camera_fk": True, "rotate_upright": True})
        body_camera = body_camera_transform(state, {"use_robot_camera_fk": True}, captured_at)
        assert body_camera is not None
        point = transform_point(body_camera, [0.1, 0, 1])
        assert point == pytest.approx([1.1, -0.1, 0.2])
        assert math.atan2(point[1], point[0]) < 0  # Image-right is robot-right, negative bearing.
        world_body = pose_transform([0.2, 0.3, 0], [math.cos(0.2), 0, 0, math.sin(0.2)])
        camera_marker = pose_transform([0.1, 0, 1], [0, 1, 0, 0])
        world_marker = matmul(matmul(world_body, body_camera), camera_marker)
        pose = marker_robot_pose(world_marker, camera_marker, body_camera)
        assert [pose.x, pose.y, pose.yaw] == pytest.approx([0.2, 0.3, 0.4])
        tof = (await client.get("http://executor/tof", headers={"Authorization": "Bearer "+token})).json()
        clearance = conservative_clearance(tof, state)
        assert clearance is not None and 1.0 < clearance < 1.1  # mm→m and sensor→trunk.
        vision.state.observations.put(VisionObservation(
            frame_id=response.headers["x-frame-id"], captured_at=captured_at, source="contract-camera",
            pose=pose, pose_valid=True, clearance_m=clearance,
            detections=[{"id": "marker:1", "label": "marker_1", "confidence": 1,
                         "bearing_rad": math.atan2(point[1], point[0]), "range_m": math.hypot(point[0], point[1])}],
        ))
        observed = await backend.observe()
        assert observed.pose_valid and observed.detections[0].range_m == pytest.approx(math.hypot(1.1, 0.1))
        receipt = await backend.execute(Action(id="forward", description="contract test", vx=0.02,
                                               duration_s=0.03), observed, "contract-move-1")
        assert receipt["status"] == "completed" and receipt["verified_success"] is False
        assert robot.calls[0] == ("robot.move", {"vx": 0.02, "vy": 0.0, "vyaw": 0.0})
        assert robot.calls[-1][0] == "robot.stop"
        stopped = await backend.stop()
        assert stopped["epoch"] > state["epoch"]
