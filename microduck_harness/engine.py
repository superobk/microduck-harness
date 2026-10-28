"""Asynchronous goals, System-1 routing, grounded verification and durable receipts."""
from __future__ import annotations

import asyncio
from dataclasses import asdict
import json
import math
import time
import uuid

from .backends import DemoWorld, LiveBackend
from .config import Settings
from .decisions import Decision, DecisionError, GLMClient, JevClient
from .guard import Blocked, candidates, check_motion, check_observation, match_target
from .journal import Journal, TERMINAL
from .models import Goal, Observation, StartRequest
from .model_config import ModelConfigStore, ModelConfigUpdate
from . import __version__


class Conflict(RuntimeError):
    pass


class Engine:
    def __init__(self, settings: Settings, backend=None, jev=None, glm=None):
        self.model_store = ModelConfigStore(settings)
        self.settings = self.model_store.effective_settings(settings)
        settings = self.settings
        self.backend = backend or (DemoWorld(settings) if settings.mode == "demo" else LiveBackend(settings))
        self.journal = Journal(settings.database)
        self.jev = jev
        self.glm = glm
        ready, _ = self.model_store.readiness()
        if settings.decision_mode != "demo":
            self.glm = glm or (GLMClient(settings.glm_base_url, model=settings.glm_model,
                                       api_key=settings.glm_api_key, timeout_s=settings.glm_timeout_s,
                                       response_format=settings.glm_json_mode) if ready else None)
            if settings.decision_mode == "hybrid":
                self.jev = jev or JevClient(settings.jev_url, timeout_s=settings.jev_timeout_s)
        self.active_id: str | None = None
        self.runner: asyncio.Task | None = None
        self.lock = asyncio.Lock()
        self.applied_model_revision = self.model_store.revision
        self.applied_model_configuration = self.model_store.configuration
        self.latest_observation: Observation | None = None
        self.metrics = {"jev_calls": 0, "jev_accepted": 0, "glm_calls": 0,
                        "fallbacks": 0, "revalidations_rejected": 0}

    async def start(self, request: StartRequest):
        canonical = json.dumps(request.model_dump(), sort_keys=True, ensure_ascii=False)
        async with self.lock:
            old = self.journal.by_request(request.request_id)
            if old:
                if old["request_canonical"] != canonical:
                    raise Conflict("request_id already exists with a different goal")
                return self.public_task(old)
            if self.runner and not self.runner.done():
                raise Conflict("the robot already has an active owner; cancel that task first")
            await self._apply_model_configuration()
            if self.settings.decision_mode != "demo" and self.glm is None:
                raise Conflict("model service is not configured; save a usable model profile before starting a task")
            task = {"id": "task_"+uuid.uuid4().hex[:12], "request_id": request.request_id,
                    "request_canonical": canonical, "instruction": request.instruction,
                    "goal": request.goal.model_dump() if request.goal else None,
                    "status": "accepted", "verified_success": False, "step": 0,
                    "created_at": time.time(), "updated_at": time.time(), "evidence": [],
                    "reason": "queued for planning", "backend": self.settings.mode,
                    "decision_mode": self.settings.decision_mode,
                    "model_config_revision": self.applied_model_revision,
                    "model_id": self.settings.glm_model if self.settings.decision_mode != "demo" else None}
            self.journal.save(task)
            self.journal.event(task["id"], "task_accepted", self.public_task(task))
            self.active_id = task["id"]
            self.runner = asyncio.create_task(self._run(task, request), name=task["id"])
            return self.public_task(task)

    @staticmethod
    def public_task(task):
        return {k: v for k, v in task.items() if k != "request_canonical"}

    def task(self, task_id: str):
        task = self.journal.get(task_id)
        return self.public_task(task) if task else None

    async def cancel(self, task_id: str | None = None):
        async with self.lock:
            if task_id and task_id != self.active_id:
                old = self.journal.get(task_id)
                if old and old["status"] in TERMINAL:
                    return self.public_task(old)
                raise Conflict("task is not the active robot owner")
            # Cancel inference first, then fence any outstanding network action.
            runner = self.runner
            if runner and not runner.done():
                runner.cancel()
                try:
                    await runner
                except asyncio.CancelledError:
                    pass
            try:
                receipt = await self.backend.stop()
            except Exception as exc:
                receipt = {"stopped_walk": False, "error": str(exc),
                           "note": "stop not confirmed; local bounded pulse watchdog remains authoritative"}
            self.journal.event(task_id or self.active_id, "stop_requested", receipt)
            return {"task": self.task(task_id or self.active_id) if (task_id or self.active_id) else None,
                    "stop": receipt}

    async def observe(self):
        obs = await self.backend.observe()
        self.latest_observation = obs
        return obs

    async def status(self):
        try:
            robot = await self.backend.state()
            # Board token is a short-lived execution credential, not UI data.
            robot = {k: v for k, v in robot.items() if k != "state_token"}
        except Exception as exc:
            robot = {"healthy": False, "error": str(exc)}
        return {"version": __version__, "mode": self.settings.mode,
                "decision_mode": self.settings.decision_mode, "robot": robot,
                "active_task": self.task(self.active_id) if self.active_id else None,
                "tasks": [self.public_task(t) for t in self.journal.tasks()],
                "observation": self.latest_observation.model_dump() if self.latest_observation else None,
                "metrics": self.metrics, "scene": self.backend.scene(),
                "model_configuration": self.model_configuration_summary(),
                "native_tools_url": self.settings.tools_url}

    def model_configuration_summary(self):
        return {"active_revision": self.applied_model_revision,
                "saved_revision": self.model_store.revision,
                "pending": self.applied_model_revision != self.model_store.revision,
                "ready": self.settings.decision_mode == "demo" or self.glm is not None,
                "model_id": self.settings.glm_model,
                "application_policy": "current task keeps its revision; saved changes apply before the next task"}

    def model_configuration_status(self):
        return {"saved": self.model_store.public(),
                "active": {"revision": self.applied_model_revision,
                           "configuration": self.applied_model_configuration.model_dump(),
                           "ready": self.settings.decision_mode == "demo" or self.glm is not None},
                "pending": self.applied_model_revision != self.model_store.revision,
                "writes_enabled": bool(self.settings.gateway_token),
                "application_policy": "next_task"}

    async def update_model_configuration(self, change: ModelConfigUpdate):
        async with self.lock:
            self.model_store.update(change)
            if not self.runner or self.runner.done():
                await self._apply_model_configuration()
            self.journal.event(None, "model_configuration_saved", {
                "saved_revision": self.model_store.revision,
                "active_revision": self.applied_model_revision,
                "pending": self.model_store.revision != self.applied_model_revision,
            })
            return self.model_configuration_status()

    async def _apply_model_configuration(self):
        if self.applied_model_revision == self.model_store.revision:
            return
        if self.runner and not self.runner.done():
            raise Conflict("cannot replace model clients while a task is active")
        settings = self.model_store.effective_settings(self.settings)
        ready, _ = self.model_store.readiness()
        glm = None
        jev = None
        if settings.decision_mode != "demo":
            if ready:
                glm = GLMClient(settings.glm_base_url, model=settings.glm_model,
                                api_key=settings.glm_api_key, timeout_s=settings.glm_timeout_s,
                                response_format=settings.glm_json_mode)
            if settings.decision_mode == "hybrid":
                jev = JevClient(settings.jev_url, timeout_s=settings.jev_timeout_s)
        previous = (self.jev, self.glm)
        self.settings, self.jev, self.glm = settings, jev, glm
        self.applied_model_revision = self.model_store.revision
        self.applied_model_configuration = self.model_store.configuration
        for client in previous:
            if client:
                try:
                    await client.aclose()
                except Exception:
                    self.journal.event(None, "model_client_cleanup_warning", {"reason": "retired client cleanup failed"})

    def _update(self, task, **values):
        task.update(values, updated_at=time.time())
        self.journal.save(task)

    @staticmethod
    def _stop_accepted(receipt):
        if isinstance(receipt.get("stop"), dict):
            return receipt["stop"].get("accepted") is True
        return receipt.get("stopped_walk") is True

    def _demo_plan(self, instruction: str):
        text = instruction.lower()
        if any(w in text for w in ["捡", "抓", "搬", "清洁", "pick", "clean", "deliver"]):
            raise ValueError("supported goals are explore/find/approach; pickup requires a calibrated verified skill")
        if any(w in text for w in ["探索", "explore", "巡"]):
            return Goal(kind="explore", max_steps=20, deadline_s=120, rationale="bounded exploration demo")
        target = "paper" if any(w in text for w in ["纸", "paper"]) else "marker_1" if any(
            w in text for w in ["标记", "marker"]) else None
        if not target:
            raise ValueError("demo understands exploration, paper and marker_1; use GLM for natural-language planning")
        kind = "approach" if any(w in text for w in ["靠近", "走到", "approach", "go to"]) else "find"
        return Goal(kind=kind, target=target, max_steps=80, deadline_s=180,
                    rationale="explicit deterministic demo parser")

    async def _plan(self, request, obs):
        if request.goal:
            return request.goal
        if self.settings.decision_mode == "demo":
            return self._demo_plan(request.instruction)
        self.metrics["glm_calls"] += 1
        plan = await self.glm.plan_goal(request.instruction, {
            "available_goals": ["explore", "find", "approach"],
            "known_targets": sorted({d.label for d in obs.detections} | {"paper", "marker_1"}),
            "max_steps": self.settings.max_steps, "max_deadline_s": self.settings.max_task_s,
            "work_zone": list(self.settings.bounds),
            "constraints": "Manipulation is not supported. Targets use exact detector labels. No shell or direct motor commands."
        })
        return Goal.model_validate(plan)

    @staticmethod
    def _summary(goal, obs, target, step, history, visits):
        # Bound context before sending it to the 2048-token decision model.
        # Never include camera OCR/prompts, arbitrary files or unconstrained tool text.
        return {"goal": {"kind": goal.kind, "target": goal.target}, "step": step,
                "steps_left": goal.max_steps-step, "pose": obs.pose.model_dump() if obs.pose else None,
                "pose_valid": obs.pose_valid, "clearance_m": obs.clearance_m,
                "target": target.model_dump() if target else None,
                "recent_actions": history[-6:], "visited_cells": list(visits)[-12:],
                "frame_id": obs.frame_id, "observation_age_s": round(max(0, time.time()-obs.captured_at), 3)}

    async def _choose(self, summary, allowed, task, streak):
        options = {key: a.description for key, a in allowed.items()}
        if len(options) == 1:
            # Waiting alone cannot make an ungrounded robot explore; fail visibly.
            raise Blocked("no grounded movement is available: check pose, clearance and work zone")
        if self.settings.decision_mode == "demo":
            target = summary["target"]
            if target and abs(target["bearing_rad"]) > 0.15:
                chosen = "turn_left" if target["bearing_rad"] > 0 else "turn_right"
            elif target and summary["goal"]["kind"] == "find":
                chosen = "wait"
            elif target and target["range_m"] is not None and target["range_m"] <= self.settings.arrival_distance_m:
                chosen = "wait"
            elif "forward" in allowed and (target or summary["step"] % 7 < 5):
                chosen = "forward"
            else:
                chosen = "turn_left"
            if chosen not in allowed:
                chosen = next((key for key in ("turn_left", "turn_right", "wait") if key in allowed))
            return Decision(chosen, None, None, {}, 0.0, "deterministic-demo"), 0
        if self.jev is not None and streak < self.settings.jev_max_streak:
            self.metrics["jev_calls"] += 1
            try:
                decision = await self.jev.choose(summary, options)
                self.journal.event(task["id"], "jev_distribution", asdict(decision))
                if (decision.probability >= self.settings.jev_threshold
                        and decision.margin >= self.settings.jev_margin):
                    self.metrics["jev_accepted"] += 1
                    return decision, streak+1
                self.journal.event(task["id"], "glm_fallback", {"reason": "low AgentJev confidence or margin"})
            except DecisionError as exc:
                self.journal.event(task["id"], "glm_fallback", {"reason": str(exc)})
            self.metrics["fallbacks"] += 1
        elif self.jev:
            self.journal.event(task["id"], "glm_fallback", {"reason": "periodic System-2 guidance"})
            self.metrics["fallbacks"] += 1
        self.metrics["glm_calls"] += 1
        return await self.glm.choose(summary, options), 0

    async def _run(self, task, request):
        deadline = time.monotonic()+self.settings.max_task_s
        started = time.monotonic()
        history, visits, evidence = [], {}, []
        locked_id, streak = None, 0
        distance = 0.0
        previous_pose = None
        try:
            self._update(task, status="planning", reason="interpret goal")
            initial_stop = await self.backend.stop()
            if not self._stop_accepted(initial_stop):
                raise Blocked("initial walking stop was not acknowledged")
            obs = await self.observe()
            check_observation(obs, self.settings)
            goal = await self._plan(request, obs)
            if goal.max_steps > self.settings.max_steps or goal.deadline_s > self.settings.max_task_s:
                raise Blocked("goal budget exceeds local configured limits")
            deadline = min(deadline, started+goal.deadline_s)
            self._update(task, goal=goal.model_dump(), status="running", reason="observe and decide")
            self.journal.event(task["id"], "goal_planned", goal.model_dump())
            for step in range(goal.max_steps+1):
                if time.monotonic() >= deadline:
                    self._update(task, status="budget_exhausted", reason="task deadline reached")
                    break
                obs = await self.observe()
                check_observation(obs, self.settings)
                robot = await self.backend.state()
                if not robot.get("healthy") or not robot.get("enabled"):
                    raise Blocked("robot is unhealthy or its locomotion policy is not active")
                if obs.pose_valid and obs.pose:
                    cell = f"{math.floor(obs.pose.x/0.2)},{math.floor(obs.pose.y/0.2)}"
                    visits[cell] = visits.get(cell, 0)+1
                    if previous_pose:
                        distance += math.hypot(obs.pose.x-previous_pose.x, obs.pose.y-previous_pose.y)
                    previous_pose = obs.pose
                target = match_target(obs, goal, locked_id)
                # A class-level find verifies that the class is visible across frames.
                # YOLO boxes have per-frame IDs; locking those would make find impossible.
                # Approaching a specific object still requires stable identity and range.
                if target and not locked_id and (goal.kind == "approach" or goal.target == target.id):
                    locked_id = target.id
                condition = bool(target) and (goal.kind == "find" or (
                    goal.kind == "approach" and target.range_m is not None
                    and target.range_m <= self.settings.arrival_distance_m
                    and abs(target.bearing_rad) <= 0.20))
                if condition:
                    if not evidence or (evidence[-1]["frame_id"] != obs.frame_id
                                        and obs.captured_at > evidence[-1]["captured_at"]):
                        evidence.append({"frame_id": obs.frame_id, "captured_at": obs.captured_at,
                                         "target": target.model_dump(), "source": obs.source})
                    if len(evidence) >= self.settings.evidence_frames:
                        self._update(task, status="succeeded", verified_success=True, evidence=evidence,
                                     step=step, reason="target confirmed on distinct fresh frames")
                        break
                    # Verification never asks a model to move a robot that has already arrived.
                    await asyncio.sleep(0.10)
                    continue
                evidence = []
                if step == goal.max_steps:
                    success = goal.kind == "explore" and distance >= 0.08
                    self._update(task, status="succeeded" if success else "budget_exhausted",
                                 verified_success=success, step=step,
                                 reason="bounded exploration completed" if success else "goal not verified within step budget",
                                 evidence=[{"path_length_m": round(distance, 3), "visited_cells": list(visits),
                                            "scope": "observed path only; not proof of complete area coverage"}])
                    break
                allowed = candidates(obs, goal, self.settings, target)
                summary = self._summary(goal, obs, target, step, history, visits)
                # The task's wall-clock budget includes model time; a model can never hold it open.
                async with asyncio.timeout(max(0.001, deadline-time.monotonic())):
                    decision, streak = await self._choose(summary, allowed, task, streak)
                if time.monotonic() >= deadline:
                    raise TimeoutError("task deadline reached during decision")
                fresh = await self.observe()
                check_observation(fresh, self.settings)
                fresh_target = match_target(fresh, goal, locked_id)
                fresh_allowed = candidates(fresh, goal, self.settings, fresh_target)
                if decision.action not in fresh_allowed:
                    self.metrics["revalidations_rejected"] += 1
                    self.journal.event(task["id"], "action_rejected", {"reason": "preconditions changed during inference",
                                                                       "action": decision.action})
                    await self.backend.stop()
                    streak = self.settings.jev_max_streak
                    continue
                # For target-oriented motion, changed/disappeared target evidence requires a new choice.
                if target and (fresh_target is None or abs(fresh_target.bearing_rad-target.bearing_rad) > 0.20):
                    self.journal.event(task["id"], "action_rejected", {"reason": "target moved or disappeared"})
                    await self.backend.stop()
                    streak = self.settings.jev_max_streak
                    continue
                action = fresh_allowed[decision.action]
                check_motion(fresh, action, self.settings)
                if time.monotonic()+action.duration_s+0.1 >= deadline:
                    raise TimeoutError("insufficient task time for another bounded pulse")
                self.journal.event(task["id"], "decision", {**asdict(decision), "frame_id": fresh.frame_id,
                                                            "step": step, "action": action.model_dump()})
                if action.id == "wait":
                    await self.backend.stop()
                    await asyncio.sleep(action.duration_s)
                    receipt = {"status": "waited", "verified_success": False}
                else:
                    receipt = await self.backend.execute(action, fresh, f"{task['id']}:{step}")
                self.journal.event(task["id"], "action_receipt", receipt)
                if receipt.get("status") not in {"completed", "waited"}:
                    raise Blocked("executor did not complete the pulse: "+str(receipt.get("error") or receipt.get("status")))
                history.append(action.id)
                # Repeated waits/turns without forward progress trigger System-2 guidance.
                if len(history) >= 6 and "forward" not in history[-6:]:
                    streak = self.settings.jev_max_streak
                self._update(task, step=step+1, reason="bounded pulse complete; observe again")
        except asyncio.CancelledError:
            self._update(task, status="cancelled", verified_success=False, reason="cancelled by operator")
            raise
        except TimeoutError:
            self._update(task, status="budget_exhausted", verified_success=False, reason="task deadline reached")
        except Blocked as exc:
            self._update(task, status="blocked", verified_success=False, reason=str(exc))
        except Exception as exc:
            self._update(task, status="failed", verified_success=False, reason=str(exc))
        finally:
            try:
                receipt = await self.backend.stop()
                self.journal.event(task["id"], "final_stop", receipt)
                accepted = self._stop_accepted(receipt)
                self._update(task, stop_command_accepted=accepted, physical_stop_verified=False)
                if not accepted:
                    self._update(task, status="failed", verified_success=False,
                                 reason="final stop command was not acknowledged")
            except Exception as exc:
                self.journal.event(task["id"], "stop_unconfirmed", {"error": str(exc)})
                self._update(task, stop_command_accepted=False, physical_stop_verified=False,
                             status="failed", verified_success=False, reason="final stop command unconfirmed")
            self.journal.event(task["id"], "task_finished", self.public_task(task))

    async def close(self):
        if self.runner and not self.runner.done():
            await self.cancel()
        for client in (self.jev, self.glm):
            if client:
                await client.aclose()
        await self.backend.close()
        self.journal.close()
