"""Deterministic motion eligibility. Neither LLM can change these bounds."""
from __future__ import annotations
import math
import time
from .config import Settings
from .models import Action, Detection, Goal, Observation


class Blocked(RuntimeError):
    pass


def check_observation(obs: Observation, settings: Settings, now: float | None = None):
    age = (time.time() if now is None else now)-obs.captured_at
    if age < -0.25:
        raise Blocked("camera clock is ahead; synchronize workstation and ZERO clocks")
    if age > settings.max_observation_age_s:
        raise Blocked("perception is stale; refresh the source frame")
    if settings.mode == "live" and ("demo" in obs.source.lower() or "simulat" in obs.source.lower()):
        raise Blocked("synthetic observations cannot control a live robot")


def match_target(obs: Observation, goal: Goal, locked_id: str | None = None) -> Detection | None:
    if not goal.target:
        return None
    targets = [d for d in obs.detections if d.confidence >= 0.65 and (
        d.id == goal.target or d.label.lower() == goal.target.lower())]
    if locked_id:
        return next((d for d in targets if d.id == locked_id), None)
    return max(targets, key=lambda d: d.confidence, default=None)


def check_motion(obs: Observation, action: Action, settings: Settings):
    check_observation(obs, settings)
    if action.id not in {"wait", "forward", "turn_left", "turn_right"}:
        raise Blocked("unknown action identifier")
    if action.duration_s > settings.pulse_s and action.id != "wait":
        raise Blocked("pulse exceeds configured maximum")
    if action.id == "wait":
        if action.vx != 0 or action.vy != 0 or action.vyaw != 0:
            raise Blocked("wait must have exactly zero motion")
        return
    if action.id == "forward" and (action.vx <= 0 or action.vy != 0 or action.vyaw != 0):
        raise Blocked("forward must be straight positive motion")
    if action.id.startswith("turn_") and (action.vx != 0 or action.vy != 0 or (
        action.vyaw <= 0 if action.id == "turn_left" else action.vyaw >= 0)):
        raise Blocked("turn must be pure rotation with matching direction")
    if not obs.pose_valid or obs.pose is None:
        raise Blocked("motion needs a fresh calibrated robot pose")
    if obs.clearance_m is None:
        raise Blocked("motion needs a fresh measured obstacle clearance")
    x0, x1, y0, y1 = settings.bounds
    m = settings.boundary_margin_m
    p = obs.pose
    # Walking candidates have no simultaneous turn, so this endpoint check is bounded.
    end_x = p.x + (action.vx*math.cos(p.yaw)-action.vy*math.sin(p.yaw))*action.duration_s
    end_y = p.y + (action.vx*math.sin(p.yaw)+action.vy*math.cos(p.yaw))*action.duration_s
    for x, y in [(p.x, p.y), (end_x, end_y)]:
        if not x0+m <= x <= x1-m or not y0+m <= y <= y1-m:
            raise Blocked("robot footprint or predicted pulse leaves the configured work zone")
    travel = math.hypot(action.vx, action.vy)*action.duration_s
    if obs.clearance_m < settings.min_clearance_m + travel:
        raise Blocked("obstacle clearance is insufficient for this pulse")
    if abs(action.vx) > settings.speed_m_s+1e-9 or abs(action.vy) > 0 or abs(action.vyaw) > settings.yaw_rate_rad_s+1e-9:
        raise Blocked("velocity is outside configured bounds")


def candidates(obs: Observation, goal: Goal, settings: Settings, target: Detection | None):
    possible = [Action(id="wait", description="Stay still and obtain another fresh observation.", duration_s=0.15),
                Action(id="turn_left", description="Turn left a short bounded step to scan or align with a target on the left.",
                       vyaw=settings.yaw_rate_rad_s, duration_s=settings.pulse_s),
                Action(id="turn_right", description="Turn right a short bounded step to scan or align with a target on the right.",
                       vyaw=-settings.yaw_rate_rad_s, duration_s=settings.pulse_s)]
    if target is None or abs(target.bearing_rad) <= 0.20:
        # If a visible target lacks range, approach is not grounded; keep only scan/wait.
        if goal.kind != "approach" or target is None or (
            target.range_m is not None and target.range_m > settings.arrival_distance_m
        ):
            possible.append(Action(id="forward", description="Walk straight ahead a short bounded step through the measured clear space.",
                                   vx=settings.speed_m_s, duration_s=settings.pulse_s))
    allowed = {}
    for action in possible:
        try:
            check_motion(obs, action, settings)
            allowed[action.id] = action
        except Blocked:
            pass
    return allowed
