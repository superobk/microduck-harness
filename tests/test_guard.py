"""Physical action eligibility, with measurements kept separate from assertions."""
import math

import pytest
from pydantic import ValidationError

from microduck_harness.config import Settings
from microduck_harness.guard import Blocked, candidates, check_motion, check_observation, match_target
from microduck_harness.models import Action, Detection, Goal, Observation


@pytest.fixture
def now(monkeypatch):
    value = 10000.0
    monkeypatch.setattr("microduck_harness.guard.time.time", lambda: value)
    return value


def observation(now, **overrides):
    data = {
        "frame_id": "camera-100", "captured_at": now, "source": "calibrated-camera",
        "pose": {"x": 0.0, "y": 0.0, "yaw": 0.0}, "pose_valid": True,
        "clearance_m": 1.0,
    }
    data.update(overrides)
    return Observation.model_validate(data)


def forward(**overrides):
    data = {"id": "forward", "description": "Forward bounded pulse", "vx": .06, "duration_s": .6}
    data.update(overrides)
    return Action.model_validate(data)


def live_settings(**overrides):
    values = {
        "mode": "live", "decision_mode": "hybrid", "glm_base_url": "http://localhost:8000/v1",
        "gateway_token": "g" * 24, "executor_token": "e" * 24, "vision_token": "v" * 24,
    }
    values.update(overrides)
    return Settings.model_validate(values)


def test_fresh_calibrated_motion_passes(now):
    check_motion(observation(now), forward(), Settings())


@pytest.mark.parametrize("offset", [-1.1, -100, 0.5, 10])
def test_stale_or_future_frames_cannot_authorize_motion(now, offset):
    with pytest.raises(Blocked):
        check_motion(observation(now, captured_at=now + offset), forward(), Settings())


@pytest.mark.parametrize("change", [{"pose": None, "pose_valid": False}, {"pose_valid": False}])
def test_unknown_or_invalid_pose_blocks_motion(now, change):
    with pytest.raises(Blocked, match="pose"):
        check_motion(observation(now, **change), forward(), Settings())


@pytest.mark.parametrize("action", [forward(), forward(id="turn_left", vx=0, vyaw=.4)])
def test_unknown_clearance_blocks_walking_and_turning(now, action):
    with pytest.raises(Blocked, match="clearance"):
        check_motion(observation(now, clearance_m=None), action, Settings())


def test_real_stationary_wait_can_request_more_evidence(now):
    obs = observation(now, pose=None, pose_valid=False, clearance_m=None)
    check_motion(obs, forward(id="wait", vx=0, duration_s=.15), Settings())


def test_stationary_wait_still_requires_a_fresh_frame(now):
    obs = observation(now, captured_at=now - 10, pose=None, pose_valid=False, clearance_m=None)
    with pytest.raises(Blocked):
        check_motion(obs, forward(id="wait", vx=0, duration_s=.15), Settings())


@pytest.mark.parametrize("parameters", [
    {"id": "wait", "vx": .04},
    {"id": "wait", "vx": 0, "vyaw": .2},
    {"id": "shell", "vx": .04},
    {"id": "forward", "vx": -.04},
    {"id": "forward", "vx": .04, "vyaw": .2},
    {"id": "turn_left", "vx": .03, "vyaw": .2},
    {"id": "turn_left", "vx": 0, "vyaw": -.2},
    {"id": "turn_right", "vx": 0, "vyaw": .2},
    {"id": "forward", "vx": .04, "duration_s": .9},
])
def test_rejected_action_ids_and_shapes_never_bypass_guard(now, parameters):
    with pytest.raises(Blocked):
        check_motion(observation(now), forward(**parameters), Settings())


@pytest.mark.parametrize("parameters", [
    {"vx": .09}, {"vy": .01}, {"id": "turn_left", "vx": 0, "vyaw": .6},
])
def test_configured_velocity_limits_apply(now, parameters):
    with pytest.raises(Blocked):
        check_motion(observation(now), forward(**parameters), Settings())


def test_geofence_includes_start_and_predicted_end(now):
    for x in (1.35, 1.29):
        with pytest.raises(Blocked, match="zone"):
            check_motion(observation(now, pose={"x": x, "y": 0, "yaw": 0}), forward(), Settings())


def test_geofence_rotates_body_velocity_into_world_axes(now):
    obs = observation(now, pose={"x": 0, "y": 1.29, "yaw": math.pi / 2})
    with pytest.raises(Blocked, match="zone"):
        check_motion(obs, forward(), Settings())


def test_clearance_reserves_full_pulse_distance(now):
    # .30 m minimum + .06 m/s * .60 s = .336 m required.
    with pytest.raises(Blocked, match="clearance"):
        check_motion(observation(now, clearance_m=.335), forward(), Settings())
    check_motion(observation(now, clearance_m=.34), forward(), Settings())


@pytest.mark.parametrize("source", ["kinematic-demo", "simulated-camera", "DEMO"])
def test_synthetic_observations_cannot_drive_live_backend(now, source):
    with pytest.raises(Blocked, match="synthetic"):
        check_observation(observation(now, source=source), live_settings())


def test_visible_target_without_metric_range_cannot_authorize_approach(now):
    target = Detection(id="paper-1", label="paper", confidence=.98, bearing_rad=0, range_m=None)
    obs = observation(now, detections=[target.model_dump()])
    allowed = candidates(obs, Goal(kind="approach", target="paper"), Settings(), target)
    assert "forward" not in allowed
    assert set(allowed) == {"wait", "turn_left", "turn_right"}


def test_invalid_perception_leaves_only_stationary_candidate(now):
    obs = observation(now, pose=None, pose_valid=False, clearance_m=None)
    allowed = candidates(obs, Goal(kind="explore"), Settings(), None)
    assert set(allowed) == {"wait"}


def test_stale_perception_produces_no_legal_candidates(now):
    obs = observation(now, captured_at=now - 3)
    assert candidates(obs, Goal(kind="explore"), Settings(), None) == {}


def test_target_lock_does_not_silently_jump_to_similar_object(now):
    obs = observation(now, detections=[
        {"id": "paper-1", "label": "paper", "confidence": .7, "bearing_rad": 0, "range_m": .5},
        {"id": "paper-2", "label": "paper", "confidence": .99, "bearing_rad": .1, "range_m": .8},
    ])
    goal = Goal(kind="find", target="paper")
    assert match_target(obs, goal).id == "paper-2"
    assert match_target(obs, goal, locked_id="paper-1").id == "paper-1"
    assert match_target(obs, goal, locked_id="missing") is None


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_observations_and_actions_rejected_at_boundary(now, value):
    for field in ("captured_at", "clearance_m"):
        with pytest.raises(ValidationError):
            observation(now, **{field: value})
    with pytest.raises(ValidationError):
        observation(now, pose={"x": value, "y": 0, "yaw": 0})
    with pytest.raises(ValidationError):
        forward(vx=value)
    with pytest.raises(ValidationError):
        Settings(speed_m_s=value)


@pytest.mark.parametrize("url", ["http://user:secret@localhost", "http://localhost?token=secret", "file:///tmp/socket"])
@pytest.mark.parametrize("field", ["executor_url", "vision_url", "jev_url"])
def test_service_urls_cannot_smuggle_credentials_into_public_settings(field, url):
    with pytest.raises(ValidationError):
        live_settings(**{field: url})
