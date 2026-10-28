import math
import time

import httpx
import pytest
from pydantic import ValidationError

from microduck_harness.vision import (
    CameraPipeline, Observation, ObservationStore, body_camera_transform,
    checked_transform, conservative_clearance, create_vision_app, inverse_transform,
    marker_robot_pose, matmul, pose_transform,
)


def identity(x=0.0, y=0.0, z=0.0):
    return [[1, 0, 0, x], [0, 1, 0, y], [0, 0, 1, z], [0, 0, 0, 1]]


def observation(**kwargs):
    value = {"frame_id": "f1", "captured_at": time.time(), "source": "calibrated-slam",
             "pose": {"x": 1, "y": 2, "yaw": 0}, "pose_valid": True,
             "clearance_m": None, "detections": []}
    value.update(kwargs)
    return value


def test_transform_order_and_body_yaw():
    world_body = pose_transform([1, 2, 0.3], [math.cos(0.2), 0, 0, math.sin(0.2)])
    body_camera = pose_transform([0.07, 0.02, 0.1], [0.5, -0.5, 0.5, -0.5])
    camera_marker = identity(0.1, 0.05, 1.2)
    world_marker = matmul(matmul(world_body, body_camera), camera_marker)
    result = marker_robot_pose(world_marker, camera_marker, body_camera)
    assert result.x == pytest.approx(1)
    assert result.y == pytest.approx(2)
    assert result.yaw == pytest.approx(0.4)
    assert matmul(world_body, inverse_transform(world_body))[0][3] == pytest.approx(0, abs=1e-12)


def test_invalid_transform_and_nonfinite_observation_rejected():
    with pytest.raises(ValueError):
        checked_transform([[1, 0, 0, 0], [0, -1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]])
    with pytest.raises(ValidationError):
        Observation(**observation(clearance_m=float("nan")))
    with pytest.raises(ValidationError):
        Observation(**observation(pose=None))


def test_source_timestamp_and_frame_id_are_not_refreshed():
    now = time.time()
    store = ObservationStore()
    store.put(Observation(**observation(captured_at=now)), now)
    with pytest.raises(ValueError, match="repeated source frame_id"):
        store.put(Observation(**observation(captured_at=now+0.1)), now+0.1)
    with pytest.raises(ValueError, match="future"):
        store.put(Observation(**observation(frame_id="f2", captured_at=now+5)), now)
    assert store.latest.captured_at == now


def test_measured_head_required_for_fixed_extrinsics_and_stale_fk_rejected():
    config = {"use_robot_camera_fk": False, "fixed_head_rad": [0, 0, 0, 0], "T_body_camera": identity()}
    now = time.time()
    state = {"observed_at": now, "head": [0, 0, 0, 0], "raw": {"joints": [0]*15}}
    assert body_camera_transform(state, config, now) == identity()
    state["raw"]["joints"][7] = 0.1
    assert body_camera_transform(state, config, now) is None
    state["raw"]["joints"] = []
    assert body_camera_transform(state, config, now) is None
    state["frames"] = {"camera": {"pos": [0, 0, 0], "quat": [1, 0, 0, 0]}}
    assert body_camera_transform(state, {"use_robot_camera_fk": True}, now-1) is None


def tof_fixture():
    state = {"raw": {"t_ns": 1_000_000_000}, "frames": {"tof": {"pos": [0.05, 0, 0.1], "quat": [1, 0, 0, 0]}}}
    tof = {"age_s": 0.1, "frame": {"rows": 8, "cols": 8, "t_ns": 1_000_000_000,
        "distance_mm": [1000]*64, "status": [5]*64}}
    return tof, state


def test_tof_no_target_unusable_stale_and_wrong_head_are_unknown():
    tof, state = tof_fixture()
    distance = conservative_clearance(tof, state)
    assert 0.9 < distance < 1.05
    tof["frame"]["status"][18] = 255
    assert conservative_clearance(tof, state) is None
    tof["frame"]["status"][18] = 4
    assert conservative_clearance(tof, state) is None
    tof["frame"]["status"][18] = 9
    assert conservative_clearance(tof, state) is not None
    tof["frame"]["distance_mm"][18] = -3
    assert conservative_clearance(tof, state) is None
    tof["frame"]["distance_mm"][18] = 1000
    tof["age_s"] = 2
    assert conservative_clearance(tof, state) is None
    tof["age_s"] = 0.1
    state["frames"]["tof"]["quat"] = [math.sqrt(0.5), 0, 0, math.sqrt(0.5)]
    assert conservative_clearance(tof, state) is None


@pytest.mark.parametrize("board_clock_offset_s", [0.0, 60.0])
def test_real_aruco_detection_and_localization_from_synthetic_calibrated_image(board_clock_offset_s):
    cv2 = pytest.importorskip("cv2")
    np = pytest.importorskip("numpy")
    frame = np.full((480, 640, 3), 255, dtype=np.uint8)
    marker = cv2.aruco.generateImageMarker(cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50), 7, 100)
    frame[190:290, 270:370] = cv2.cvtColor(marker, cv2.COLOR_GRAY2BGR)
    body_camera = [[0, 0, 1, 0.08], [-1, 0, 0, 0], [0, -1, 0, 0.2], [0, 0, 0, 1]]
    camera_marker = [[1, 0, 0, 0], [0, -1, 0, 0], [0, 0, -1, 0.4], [0, 0, 0, 1]]
    world_marker = matmul(matmul(identity(1, 2, 0), body_camera), camera_marker)
    cfg = {"camera_matrix": [[500, 0, 319.5], [0, 500, 239.5], [0, 0, 1]],
           "use_robot_camera_fk": False, "fixed_head_rad": [0, 0, 0, 0],
           "T_body_camera": body_camera, "markers": {"7": {"size_m": 0.08, "label": "home", "T_world_marker": world_marker}}}
    now = time.time()
    source_time = now + board_clock_offset_s
    state = {"observed_at": source_time, "raw": {"joints": [0]*15}}
    result = CameraPipeline(cfg, ObservationStore()).detect(frame, "marker-test", now, state, {},
                                                            source_captured_at=source_time)
    # Frame TTL stays on the workstation clock; FK pairing stays on the board.
    assert result.captured_at == now
    assert result.pose_valid
    assert result.pose.x == pytest.approx(1, abs=0.03)
    assert result.pose.y == pytest.approx(2, abs=0.03)
    assert result.detections[0].label == "home"
    assert result.detections[0].range_m == pytest.approx(0.48, abs=0.03)
    assert result.clearance_m is None
    # Detection without camera-to-body calibration is not an invented range.
    result = CameraPipeline(cfg, ObservationStore()).detect(frame, "uncalibrated", now, {}, {})
    assert not result.pose_valid
    assert result.detections[0].range_m is None


@pytest.mark.asyncio
async def test_ingestion_auth_and_unchanged_capture_time():
    app = create_vision_app({"token": "vision-test"})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/observe")).status_code == 401
        client.headers["Authorization"] = "Bearer vision-test"
        assert (await client.get("/observe")).status_code == 503
        payload = observation()
        assert (await client.post("/observations", json=payload)).status_code == 200
        result = (await client.get("/observe")).json()
        assert result["captured_at"] == payload["captured_at"]
        assert result["clearance_m"] is None
        assert (await client.post("/observations", json=payload)).status_code == 409
        assert (await client.get("/image")).status_code == 404


@pytest.mark.asyncio
async def test_camera_stream_cannot_be_overwritten_by_external_publisher():
    app = create_vision_app({"token": "test", "camera_config": {"mode": "camera"}})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                                 headers={"Authorization": "Bearer test"}) as client:
        assert (await client.post("/observations", json=observation())).status_code == 409
