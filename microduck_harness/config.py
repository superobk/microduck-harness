"""Small explicit configuration surface; credentials never appear in status output."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    mode: Literal["demo", "live"] = "demo"
    decision_mode: Literal["demo", "hybrid", "glm"] = "demo"
    gateway_token: str = ""
    executor_url: str = "http://127.0.0.1:8766"
    executor_token: str = ""
    vision_url: str = "http://127.0.0.1:8767"
    vision_token: str = ""
    tools_url: str = "http://127.0.0.1:8768"
    jev_url: str = "http://127.0.0.1:8149"
    jev_timeout_s: float = Field(2.0, gt=0, le=15)
    jev_threshold: float = Field(0.85, ge=0, le=1)
    jev_margin: float = Field(0.20, ge=0, le=1)
    jev_max_streak: int = Field(8, ge=1, le=50)
    glm_base_url: str = ""
    glm_model: str = "GLM5.3-Flash"
    glm_api_key: str = ""
    glm_timeout_s: float = Field(25, gt=0, le=120)
    glm_json_mode: bool = True
    database: str = "data/harness.sqlite3"
    model_config_file: str = ""
    # Robot base coordinates, metres. This is a bounded work zone, not a map.
    bounds: tuple[float, float, float, float] = (-1.5, 1.5, -1.5, 1.5)
    boundary_margin_m: float = Field(0.20, ge=0.1, le=1)
    min_clearance_m: float = Field(0.30, ge=0.15, le=2)
    arrival_distance_m: float = Field(0.35, ge=0.20, le=2)
    max_observation_age_s: float = Field(0.8, gt=0, le=1)
    speed_m_s: float = Field(0.06, gt=0, le=0.08)
    yaw_rate_rad_s: float = Field(0.4, gt=0, le=0.5)
    pulse_s: float = Field(0.6, gt=0, le=1)
    max_steps: int = Field(100, ge=1, le=100)
    max_task_s: float = Field(300, ge=5, le=600)
    evidence_frames: int = Field(3, ge=2, le=10)

    @model_validator(mode="after")
    def valid_config(self):
        for name in ("executor_url", "vision_url", "tools_url", "jev_url", "glm_base_url"):
            value = getattr(self, name)
            if not value and name == "glm_base_url":
                continue
            parsed = urlsplit(value)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
                raise ValueError(f"{name} must be an HTTP(S) URL without credentials, query or fragment")
        x0, x1, y0, y1 = self.bounds
        if x1-x0 <= 2*self.boundary_margin_m or y1-y0 <= 2*self.boundary_margin_m:
            raise ValueError("bounds too small for the robot boundary margin")
        if self.mode == "live":
            if self.decision_mode == "demo":
                raise ValueError("live mode requires decision_mode=hybrid or glm")
            # A live gateway may start read-only before a model endpoint is supplied.
            # Engine rejects goals until a usable operator model profile is present.
            if not self.gateway_token or not self.executor_token or not self.vision_token:
                raise ValueError("live mode requires separate gateway/executor/vision tokens")
        return self

    @classmethod
    def load(cls, path: str | None = None) -> "Settings":
        values = json.loads(Path(path).read_text()) if path else {}
        mapping = {
            "HARNESS_MODE": "mode", "DECISION_MODE": "decision_mode",
            "GATEWAY_TOKEN": "gateway_token", "EXECUTOR_URL": "executor_url",
            "EXECUTOR_TOKEN": "executor_token", "VISION_URL": "vision_url",
            "VISION_TOKEN": "vision_token", "AGENTJEV_URL": "jev_url",
            "TOOLS_URL": "tools_url",
            "GLM_BASE_URL": "glm_base_url", "GLM_MODEL": "glm_model",
            "GLM_API_KEY": "glm_api_key", "GLM_JSON_MODE": "glm_json_mode",
            "HARNESS_DB": "database",
            "MODEL_CONFIG_FILE": "model_config_file",
        }
        for env, field in mapping.items():
            if env in os.environ:
                values[field] = os.environ[env]
        return cls.model_validate(values)

    def public(self) -> dict:
        return self.model_dump(exclude={"gateway_token", "executor_token", "vision_token", "glm_api_key"})

