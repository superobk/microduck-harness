"""Task lifecycle and decisions tested against a 2D fixture, not robot dynamics."""
import asyncio
import contextlib
import time

import pytest

from microduck_harness.backends import DemoWorld
from microduck_harness.config import Settings
from microduck_harness.decisions import Decision
from microduck_harness.engine import Conflict, Engine
from microduck_harness.models import Goal, StartRequest


class FixedModel:
    def __init__(self, action="forward", source="glm"):
        self.action, self.source = action, source
        self.calls = 0

    async def choose(self, state, options):
        self.calls += 1
        return Decision(self.action, .95 if self.source == "agentjev" else None,
                        .9 if self.source == "agentjev" else None,
                        {}, 1.0, self.source)

    async def aclose(self):
        pass


class DelayedGLM(FixedModel):
    def __init__(self):
        super().__init__()
        self.entered = asyncio.Event()
        self.cancelled = False

    async def choose(self, state, options):
        self.entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise


def settings_for(tmp_path, **kwargs):
    return Settings(database=str(tmp_path / "harness.sqlite3"), **kwargs)


async def finish(engine, timeout=5):
    assert engine.runner is not None
    await asyncio.wait_for(engine.runner, timeout)
    return engine.task(engine.active_id)


@pytest.mark.asyncio
async def test_find_class_accepts_distinct_per_frame_box_ids(tmp_path):
    class BoxWorld(DemoWorld):
        async def observe(self):
            observation = await super().observe()
            for detection in observation.detections:
                detection.id = f"{observation.frame_id}:box:0"
                detection.range_m = None
            return observation
    cfg = settings_for(tmp_path)
    world = BoxWorld(cfg, realtime=False)
    engine = Engine(cfg, backend=world)
    try:
        await engine.start(StartRequest(request_id="find-boxes", goal=Goal(kind="find", target="paper")))
        result = await finish(engine)
        assert result["verified_success"] is True
        assert len({e["target"]["id"] for e in result["evidence"]}) == cfg.evidence_frames
        assert world.actions == []
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_passive_gateway_shutdown_does_not_stop_other_controllers(tmp_path):
    class PassiveBackend(DemoWorld):
        async def close(self):
            pass  # Same no-motion close contract as LiveBackend.
    cfg = settings_for(tmp_path)
    world = PassiveBackend(cfg)
    engine = Engine(cfg, backend=world)
    await engine.status()
    await engine.observe()
    await engine.close()
    assert world.epoch == 0
    assert not world.actions


@pytest.mark.asyncio
async def test_demo_approach_finishes_with_distinct_observation_evidence(tmp_path):
    cfg = settings_for(tmp_path)
    world = DemoWorld(cfg, realtime=False)
    engine = Engine(cfg, backend=world)
    try:
        accepted = await engine.start(StartRequest(request_id="approach-paper", goal=Goal(
            kind="approach", target="paper", max_steps=80, deadline_s=120)))
        assert accepted["status"] == "accepted" and accepted["verified_success"] is False
        task = await finish(engine)
        assert task["status"] == "succeeded"
        assert task["verified_success"] is True
        assert len(task["evidence"]) == cfg.evidence_frames
        assert len({row["frame_id"] for row in task["evidence"]}) == cfg.evidence_frames
        assert all(row["target"]["range_m"] <= cfg.arrival_distance_m for row in task["evidence"])
        assert all(row["source"] == "kinematic-demo" for row in task["evidence"])
        assert world.actions and any(a["vx"] > 0 for a in world.actions)
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_cancel_during_glm_inference_fences_motion_and_cancels_request(tmp_path):
    cfg = settings_for(tmp_path, decision_mode="glm")
    world, glm = DemoWorld(cfg, realtime=False), DelayedGLM()
    engine = Engine(cfg, backend=world, glm=glm)
    try:
        task = await engine.start(StartRequest(request_id="cancel-me", goal=Goal(kind="explore")))
        await asyncio.wait_for(glm.entered.wait(), 2)
        await asyncio.wait_for(engine.cancel(task["id"]), 2)
        result = engine.task(task["id"])
        assert result["status"] == "cancelled"
        assert result["verified_success"] is False
        assert glm.cancelled is True
        assert world.actions == []
        assert world.epoch >= 2
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_active_owner_and_idempotent_request_ids(tmp_path):
    cfg = settings_for(tmp_path, decision_mode="glm")
    world, glm = DemoWorld(cfg, realtime=False), DelayedGLM()
    engine = Engine(cfg, backend=world, glm=glm)
    request = StartRequest(request_id="owner-1", goal=Goal(kind="explore"))
    try:
        first = await engine.start(request)
        same = await engine.start(request)
        assert same["id"] == first["id"]
        with pytest.raises(Conflict, match="different goal"):
            await engine.start(StartRequest(request_id="owner-1", goal=Goal(kind="find", target="paper")))
        with pytest.raises(Conflict, match="active owner"):
            await engine.start(StartRequest(request_id="owner-2", goal=Goal(kind="explore")))
        await asyncio.wait_for(glm.entered.wait(), 2)
        await engine.cancel(first["id"])
        assert world.actions == []
    finally:
        await engine.close()


@pytest.mark.parametrize("change", ["blocked-clearance", "stale-frame"])
@pytest.mark.asyncio
async def test_model_decision_is_rechecked_against_new_observation(tmp_path, change):
    cfg = settings_for(tmp_path, decision_mode="hybrid")

    class ChangedWorld(DemoWorld):
        async def observe(self):
            obs = await super().observe()
            if self.seq == 3:  # Planning frame, decision frame, execution revalidation frame.
                if change == "blocked-clearance":
                    return obs.model_copy(update={"clearance_m": .01})
                return obs.model_copy(update={"captured_at": time.time() - 10})
            return obs

    world = ChangedWorld(cfg, realtime=False)
    jev = FixedModel(source="agentjev")
    glm = FixedModel()
    engine = Engine(cfg, backend=world, jev=jev, glm=glm)
    try:
        await engine.start(StartRequest(request_id="changed-frame", goal=Goal(kind="explore", max_steps=1)))
        result = await finish(engine)
        assert result["verified_success"] is False
        assert result["status"] in {"budget_exhausted", "blocked"}
        assert jev.calls == 1
        assert world.actions == []
        if change == "blocked-clearance":
            assert engine.metrics["revalidations_rejected"] == 1
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_completed_receipt_remains_idempotent_after_restart(tmp_path):
    cfg = settings_for(tmp_path)
    request = StartRequest(request_id="persistent-find", goal=Goal(kind="find", target="paper"))
    engine = Engine(cfg, backend=DemoWorld(cfg, realtime=False))
    first = await engine.start(request)
    assert (await finish(engine))["status"] == "succeeded"
    await engine.close()

    second_world = DemoWorld(cfg, realtime=False)
    second = Engine(cfg, backend=second_world)
    try:
        duplicate = await second.start(request)
        assert duplicate["id"] == first["id"]
        assert duplicate["status"] == "succeeded"
        assert second.runner is None
        assert second_world.actions == []
        assert any(e["kind"] == "task_finished" for e in second.journal.events())
    finally:
        await second.close()


@pytest.mark.asyncio
async def test_restart_marks_unfinished_task_interrupted_and_never_resumes(tmp_path):
    cfg = settings_for(tmp_path)
    world = DemoWorld(cfg, realtime=False)
    first = Engine(cfg, backend=world)
    request = StartRequest(request_id="power-loss", goal=Goal(kind="explore"))
    accepted = await first.start(request)
    # Simulate process loss before the accepted background task gets its first turn.
    # Do not invoke graceful Engine.close(), which would correctly mark cancellation.
    first.runner.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await first.runner
    first.journal.close()
    await world.close()

    new_world = DemoWorld(cfg, realtime=False)
    second = Engine(cfg, backend=new_world)
    try:
        interrupted = second.task(accepted["id"])
        assert interrupted["status"] == "interrupted"
        assert interrupted["verified_success"] is False
        duplicate = await second.start(request)
        assert duplicate["status"] == "interrupted"
        assert second.runner is None
        assert new_world.actions == []
    finally:
        await second.close()


@pytest.mark.asyncio
async def test_failed_action_receipt_is_not_treated_as_completed_step(tmp_path):
    cfg = settings_for(tmp_path, decision_mode="glm")

    class FailedWorld(DemoWorld):
        async def execute(self, action, observation, request_id):
            return {"request_id": request_id, "status": "failed", "error": "native command refused",
                    "verified_success": False}

    world = FailedWorld(cfg, realtime=False)
    engine = Engine(cfg, backend=world, glm=FixedModel())
    try:
        await engine.start(StartRequest(request_id="failed-pulse", goal=Goal(kind="explore", max_steps=2)))
        result = await finish(engine)
        assert result["status"] in {"failed", "blocked"}
        assert result["verified_success"] is False
        assert engine.glm.calls == 1  # No silent repeated attempt.
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_rejected_initial_stop_blocks_task(tmp_path):
    cfg = settings_for(tmp_path)

    class StopFailedWorld(DemoWorld):
        async def stop(self):
            return {"epoch": 1, "stop": {"accepted": False, "verified_physical_stop": False}}

    world = StopFailedWorld(cfg, realtime=False)
    engine = Engine(cfg, backend=world)
    try:
        await engine.start(StartRequest(request_id="stop-failed", goal=Goal(kind="find", target="paper")))
        result = await finish(engine)
        assert result["status"] in {"failed", "blocked"}
        assert result["verified_success"] is False
        assert world.actions == []
    finally:
        await engine.close()
