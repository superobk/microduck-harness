from __future__ import annotations

from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Pose(StrictModel):
    x: float
    y: float
    yaw: float


class Detection(StrictModel):
    id: str = Field(min_length=1, max_length=100)
    label: str = Field(min_length=1, max_length=100)
    confidence: float = Field(ge=0, le=1)
    bearing_rad: float
    range_m: float | None = Field(default=None, gt=0)


class Observation(BaseModel):
    model_config = ConfigDict(extra="ignore", allow_inf_nan=False)
    frame_id: str = Field(min_length=1, max_length=200)
    captured_at: float
    source: str
    pose: Pose | None = None
    pose_valid: bool = False
    clearance_m: float | None = Field(default=None, ge=0)
    detections: list[Detection] = Field(default_factory=list, max_length=100)
    notes: list[str] = Field(default_factory=list)
    image_url: str | None = None


class Goal(StrictModel):
    kind: Literal["explore", "find", "approach"]
    target: str | None = Field(default=None, max_length=100)
    max_steps: int = Field(default=40, ge=1, le=100)
    deadline_s: float = Field(default=120, ge=5, le=600)
    rationale: str = Field(default="", max_length=1000)

    @model_validator(mode="after")
    def target_needed(self):
        if self.kind != "explore" and not self.target:
            raise ValueError("find/approach require an exact target label or id")
        return self


class StartRequest(StrictModel):
    request_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    instruction: str = Field(default="", max_length=4000)
    goal: Goal | None = None

    @model_validator(mode="after")
    def has_goal(self):
        if not self.instruction.strip() and self.goal is None:
            raise ValueError("instruction or goal is required")
        return self


class Action(StrictModel):
    id: str
    description: str
    vx: float = 0
    vy: float = 0
    vyaw: float = 0
    duration_s: float = Field(gt=0, le=1)

