"""Verify the actual printable-layout matrices against image projection and PnP."""
import copy
import importlib.util
import json
import math
from pathlib import Path
import time

import pytest

from microduck_harness.vision import CameraPipeline, ObservationStore, checked_transform, pose_transform

ROOT = Path(__file__).resolve().parents[1]


def manifest():
    return json.loads((ROOT/"config/aruco-landmarks.json").read_text())


def test_manifest_matches_vision_profile_and_physical_print_contract():
    data = manifest()
    profile = json.loads((ROOT/"config/aruco-lab.example.json").read_text())
    assert profile["markers"] == data["markers"]
    assert profile["dictionary"] == data["dictionary"] == "DICT_4X4_50"
    assert profile["camera_matrix"] is None and profile["distortion"] is None
    assert data["print"]["black_square_mm"] == 160
    assert data["print"]["quiet_zone_min_mm"] >= 20
    assert data["print"]["paper_bottom_height_above_floor_mm"] + data["print"]["marker_center_from_paper_bottom_mm"] == 200
    for marker in data["markers"].values():
        matrix = checked_transform(marker["T_world_marker"])
        assert [matrix[i][1] for i in range(3)] == [0, 0, 1]  # TOP is vertical.
        assert matrix[2][3] == .2
        position = [matrix[0][3], matrix[1][3]]
        assert math.hypot(*position) == pytest.approx(1.2)
        assert [matrix[i][2] for i in range(2)] == pytest.approx([-v/1.2 for v in position])
        assert marker["size_m"] == .16


@pytest.mark.parametrize("identifier", [0, 1, 2, 3])
def test_each_wall_marker_image_recovers_world_robot_pose(identifier):
    cv2, np = pytest.importorskip("cv2"), pytest.importorskip("numpy")
    data = manifest()
    marker = data["markers"][str(identifier)]
    # Synthetic calibrated camera mounted 8 cm forward and 8 cm above trunk.
    body_camera = np.array([[0,0,1,.08],[-1,0,0,0],[0,-1,0,.08],[0,0,0,1]], float)
    position = [.07, -.04, .12]
    yaw = identifier*math.pi/2 + .06
    world_body = np.array(pose_transform(position, [math.cos(yaw/2),0,0,math.sin(yaw/2)]))
    camera_marker = np.linalg.inv(world_body @ body_camera) @ np.array(marker["T_world_marker"])
    camera_matrix = np.array([[900,0,639.5],[0,900,359.5],[0,0,1]], float)
    half = marker["size_m"]/2
    object_points = np.array([[-half,half,0],[half,half,0],[half,-half,0],[-half,-half,0]], float)
    rvec, _ = cv2.Rodrigues(camera_marker[:3,:3])
    corners, _ = cv2.projectPoints(object_points, rvec, camera_marker[:3,3], camera_matrix, np.zeros(5))
    pixels = corners.reshape(4,2).astype(np.float32)
    assert np.all((pixels[:,0] > 0) & (pixels[:,0] < 1280) & (pixels[:,1] > 0) & (pixels[:,1] < 720))
    source = cv2.aruco.generateImageMarker(cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50),identifier,600)
    # Render at 4x and integrate pixels to preserve fractional boundary positions.
    # A nearest-neighbour low-resolution raster can erase the tiny perspective
    # cue of a near-frontal planar target and is not a precise geometry oracle.
    transform = cv2.getPerspectiveTransform(np.array([[0,0],[599,0],[599,599],[0,599]],np.float32),pixels*4)
    image = cv2.warpPerspective(source,transform,(5120,2880),flags=cv2.INTER_NEAREST,borderValue=255)
    image = cv2.resize(image,(1280,720),interpolation=cv2.INTER_AREA)
    cfg = {"camera_matrix":camera_matrix.tolist(),"distortion":[0]*5,"dictionary":data["dictionary"],
           "markers":data["markers"],"use_robot_camera_fk":False,"fixed_head_rad":[0]*4,
           "T_body_camera":body_camera.tolist()}
    now = time.time()
    result = CameraPipeline(cfg,ObservationStore()).detect(image,f"wall-{identifier}",now,
                                                           {"observed_at":now,"measured_head":[0]*4},{})
    assert result.pose_valid
    assert result.detections[0].id == f"marker:{identifier}"
    assert result.detections[0].label == f"marker_{identifier}"
    assert result.pose.x == pytest.approx(position[0],abs=.045)
    assert result.pose.y == pytest.approx(position[1],abs=.045)
    error = math.atan2(math.sin(result.pose.yaw-yaw),math.cos(result.pose.yaw-yaw))
    assert abs(error) < .04


def test_setup_check_rejects_unmeasured_intrinsics_and_bad_print_size():
    spec = importlib.util.spec_from_file_location("aruco_setup_check",ROOT/"scripts/check_aruco_setup.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    profile = json.loads((ROOT/"config/aruco-lab.example.json").read_text())
    assert not module.check(profile)["configuration_ready"]
    profile.update(camera_matrix=[[500,0,319.5],[0,500,239.5],[0,0,1]],distortion=[0]*5)
    assert module.check(profile,160,160,100)["configuration_ready"]
    assert not module.check(profile,155,160,100)["configuration_ready"]
    profile["markers"]["0"]["T_world_marker"][0][2] = 1
    assert not module.check(profile,160,160,100)["configuration_ready"]
