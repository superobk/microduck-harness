"""Operator-controlled, revisioned model settings; no model can mutate this store.

The persisted file is local and mode 0600. Public representations never contain
API keys. Settings are immutable snapshots; Engine pins an active task to one
revision and applies a newer saved revision only outside a running task.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import tempfile
from typing import Literal
from urllib.parse import urlsplit
import uuid

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .config import Settings


class ModelConfigError(ValueError):
    """Sanitized operator configuration error, safe to show in the UI."""


class ModelConfigConflict(ModelConfigError):
    pass


MODEL_FIELDS = (
    "decision_mode", "glm_base_url", "glm_model", "glm_timeout_s", "glm_json_mode",
    "jev_url", "jev_timeout_s", "jev_threshold", "jev_margin", "jev_max_streak",
)


def model_origin(url: str) -> str:
    parsed = urlsplit(url)
    if not parsed.scheme or not parsed.hostname:
        return ""
    port = parsed.port if parsed.port is not None else (443 if parsed.scheme == "https" else 80)
    return f"{parsed.scheme}://{parsed.hostname.lower()}:{port}"


class ModelConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, frozen=True)
    decision_mode: Literal["demo", "hybrid", "glm"] = "demo"
    glm_base_url: str = Field(default="", max_length=2048)
    glm_model: str = Field(default="GLM5.3-Flash", min_length=1, max_length=200)
    glm_timeout_s: float = Field(default=25, gt=0, le=120)
    glm_json_mode: bool = True
    glm_auth_mode: Literal["none", "environment", "stored"] = "none"
    glm_api_key_env: str = Field(default="GLM_API_KEY", min_length=1, max_length=100)
    jev_url: str = Field(default="http://127.0.0.1:8149", max_length=2048)
    jev_timeout_s: float = Field(default=2, gt=0, le=15)
    jev_threshold: float = Field(default=0.85, ge=0, le=1)
    jev_margin: float = Field(default=0.20, ge=0, le=1)
    jev_max_streak: int = Field(default=8, ge=1, le=50)

    @model_validator(mode="after")
    def validate_values(self):
        for name in ("glm_base_url", "jev_url"):
            value = getattr(self, name)
            if name == "glm_base_url" and not value:
                continue
            try:
                parsed = urlsplit(value)
                valid = (parsed.scheme in {"http", "https"} and parsed.hostname
                         and parsed.username is None and parsed.password is None
                         and not parsed.query and not parsed.fragment
                         and not any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in value))
                _ = parsed.port
            except ValueError:
                valid = False
            if not valid:
                raise ValueError(f"{name} must be HTTP(S), without credentials, query or fragment")
        if not self.glm_model.strip() or any(ord(ch) < 32 for ch in self.glm_model):
            raise ValueError("model identifier must be nonempty and contain no control characters")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", self.glm_api_key_env):
            raise ValueError("API-key environment reference is not a valid variable name")
        return self


class ModelConfigUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    expected_revision: int = Field(ge=0, strict=True)
    decision_mode: Literal["demo", "hybrid", "glm"] | None = None
    glm_base_url: str | None = Field(default=None, max_length=2048)
    glm_model: str | None = Field(default=None, min_length=1, max_length=200)
    glm_timeout_s: float | None = Field(default=None, gt=0, le=120)
    glm_json_mode: bool | None = None
    glm_auth_mode: Literal["none", "environment", "stored"] | None = None
    glm_api_key_env: str | None = Field(default=None, min_length=1, max_length=100)
    glm_api_key_action: Literal["keep", "set", "clear"] = "keep"
    glm_api_key: str | None = Field(default=None, repr=False, max_length=8192)
    jev_url: str | None = Field(default=None, max_length=2048)
    jev_timeout_s: float | None = Field(default=None, gt=0, le=15)
    jev_threshold: float | None = Field(default=None, ge=0, le=1)
    jev_margin: float | None = Field(default=None, ge=0, le=1)
    jev_max_streak: int | None = Field(default=None, ge=1, le=50)

    @model_validator(mode="after")
    def validate_key_action(self):
        # Empty password fields and UI mask placeholders must never overwrite a key.
        if self.glm_api_key_action == "set":
            key = self.glm_api_key
            if not key or not key.strip() or key.strip() in {"***", "********", "••••••••"}:
                raise ValueError("setting a key requires a new nonempty key, not a masked placeholder")
            if any(ord(c) < 33 or ord(c) > 126 for c in key):
                raise ValueError("API key must use visible ASCII characters without whitespace")
            if self.glm_auth_mode not in {None, "stored"}:
                raise ValueError("setting a local key requires stored authentication mode")
        elif self.glm_api_key is not None:
            raise ValueError("a key value is accepted only with glm_api_key_action=set")
        return self


class ModelConfigStore:
    """Persist one active operator profile with optimistic revision checking."""

    def __init__(self, settings: Settings):
        self.robot_mode = settings.mode
        self.path = Path(settings.model_config_file) if settings.model_config_file else (
            None if settings.database == ":memory:" else Path(settings.database).parent / "model-settings.json"
        )
        self.credential_path = self.path.with_name(self.path.stem + ".credentials.json") if self.path else None
        self._credential_id = ""
        self._persistence_warning = None
        self._environment_binding = {"name": "", "origin": ""}
        values = {name: getattr(settings, name) for name in MODEL_FIELDS}
        self._stored_key = ""
        self._stored_key_origin = model_origin(settings.glm_base_url)
        if settings.glm_api_key:
            if os.getenv("GLM_API_KEY") == settings.glm_api_key:
                values.update(glm_auth_mode="environment", glm_api_key_env="GLM_API_KEY")
                self._environment_binding = {"name": "GLM_API_KEY", "origin": model_origin(settings.glm_base_url)}
            else:
                values["glm_auth_mode"] = "stored"
                self._stored_key = settings.glm_api_key
        self.configuration = ModelConfiguration.model_validate(values)
        self.revision = 0
        if self.path and (self.path.exists() or self.path.is_symlink()):
            self._load()

    @staticmethod
    def _read_private_json(path: Path) -> dict:
        if path.is_symlink() or not path.is_file():
            raise ModelConfigError("model settings and credentials must be regular local files")
        if path.stat().st_size > 128_000:
            raise ModelConfigError("model settings or credentials file is too large")
        if os.name == "posix" and path.stat().st_mode & 0o077:
            raise ModelConfigError("model settings and credential file permissions must be 0600")
        return json.loads(path.read_text(encoding="utf-8"))

    def _load(self):
        try:
            saved = self._read_private_json(self.path)
            if (not isinstance(saved, dict) or saved.get("schema_version") != 1
                    or type(saved.get("revision")) is not int or saved["revision"] < 1):
                raise ModelConfigError("unsupported model settings format")
            configuration = ModelConfiguration.model_validate(saved["configuration"])
            if self.robot_mode == "live" and configuration.decision_mode == "demo":
                raise ModelConfigError("a saved demo model profile cannot control a live robot")
            reference = saved.get("stored_key_ref", "")
            if not isinstance(reference, str) or (reference and not re.fullmatch(r"[0-9a-f]{32}", reference)):
                raise ModelConfigError("stored credential reference is invalid")
            key = ""
            if reference:
                credentials = self._read_private_json(self.credential_path)
                if (not isinstance(credentials, dict) or credentials.get("schema_version") != 1
                        or not isinstance(credentials.get("keys"), dict) or reference not in credentials["keys"]):
                    raise ModelConfigError("stored credential reference cannot be resolved")
                key = credentials["keys"][reference]
            if not isinstance(key, str) or len(key) > 8192 or any(ord(c) < 33 or ord(c) > 126 for c in key):
                raise ModelConfigError("stored model key is invalid")
            self.configuration, self.revision = configuration, saved["revision"]
            self._credential_id = reference
            self._stored_key = key
            self._stored_key_origin = saved.get("stored_key_origin", model_origin(configuration.glm_base_url))
            if not isinstance(self._stored_key_origin, str):
                raise ModelConfigError("stored model key origin is invalid")
            binding = saved.get("environment_key_binding", {"name": "", "origin": ""})
            if (not isinstance(binding, dict) or set(binding) != {"name", "origin"}
                    or not all(isinstance(value, str) for value in binding.values())):
                raise ModelConfigError("environment key binding is invalid")
            self._environment_binding = binding
        except ModelConfigError:
            raise
        except Exception:
            # Raw parser errors can quote secret file contents; never expose them.
            raise ModelConfigError("could not load model settings; inspect the local configuration file") from None

    def key(self) -> str:
        if self.configuration.glm_auth_mode == "stored":
            return self._stored_key if self._stored_key_origin == model_origin(self.configuration.glm_base_url) else ""
        if self.configuration.glm_auth_mode == "environment":
            authorized = self._environment_binding == {
                "name": self.configuration.glm_api_key_env, "origin": model_origin(self.configuration.glm_base_url)}
            return os.getenv(self.configuration.glm_api_key_env, "") if authorized else ""
        return ""

    def readiness(self) -> tuple[bool, str]:
        if self.configuration.decision_mode == "demo":
            return True, "deterministic demo"
        if not self.configuration.glm_base_url:
            return False, "configure the GLM-compatible Base URL"
        if self.configuration.glm_auth_mode != "none" and not self.key():
            return False, "configured authentication has no available API key"
        key = self.key()
        if len(key) > 8192 or any(ord(c) < 33 or ord(c) > 126 for c in key):
            return False, "configured authentication key is invalid"
        return True, "configured; network/model availability is not yet tested"

    def effective_settings(self, base: Settings) -> Settings:
        values = {name: getattr(self.configuration, name) for name in MODEL_FIELDS}
        values["glm_api_key"] = self.key()
        return Settings.model_validate({**base.model_dump(), **values})

    def public(self) -> dict:
        ready, reason = self.readiness()
        return {"revision": self.revision, "configuration": self.configuration.model_dump(),
                "api_key_configured": bool(self.key()), "ready": ready, "readiness_reason": reason,
                "persistence": "local_owner_only_file" if self.path else "memory_only",
                "persistence_warning": self._persistence_warning}

    def update(self, change: ModelConfigUpdate):
        if change.expected_revision != self.revision:
            raise ModelConfigConflict("model settings changed; reload before saving")
        fields = change.model_dump(exclude_unset=True)
        fields = {k: v for k, v in fields.items()
                  if k not in {"expected_revision", "glm_api_key_action", "glm_api_key"}}
        if any(value is None for value in fields.values()):
            raise ModelConfigError("configuration fields cannot be null")
        stored_key = self._stored_key
        stored_key_origin = self._stored_key_origin
        environment_binding = self._environment_binding
        if change.glm_api_key_action == "set":
            stored_key = change.glm_api_key
            fields["glm_auth_mode"] = "stored"
        elif change.glm_api_key_action == "clear":
            stored_key = ""
            if "glm_auth_mode" not in fields:
                fields["glm_auth_mode"] = "none"
            elif fields["glm_auth_mode"] == "stored":
                raise ModelConfigError("clearing a key requires none or environment authentication")
        try:
            candidate = ModelConfiguration.model_validate({**self.configuration.model_dump(), **fields})
        except Exception:
            # Deliberately omit submitted values from exceptions and logs.
            raise ModelConfigError("invalid model settings: check URL, model ID, authentication and numeric limits") from None
        if self.robot_mode == "live" and candidate.decision_mode == "demo":
            raise ModelConfigError("live robots cannot use the deterministic demo decision mode")
        changed_origin = model_origin(candidate.glm_base_url) != model_origin(self.configuration.glm_base_url)
        explicit_environment = (fields.get("glm_auth_mode") == "environment"
                                and "glm_api_key_env" in fields)
        if explicit_environment:
            environment_binding = {"name": candidate.glm_api_key_env, "origin": model_origin(candidate.glm_base_url)}
        if (candidate.glm_auth_mode == "environment" and environment_binding != {
                "name": candidate.glm_api_key_env, "origin": model_origin(candidate.glm_base_url)}):
            raise ModelConfigError("environment authentication requires an explicit mode and variable reference for this model origin")
        if (changed_origin and self.key() and change.glm_api_key_action == "keep"
                and candidate.glm_auth_mode != "none"):
            if not explicit_environment:
                raise ModelConfigError("new model origin requires a new key, clear/no authentication, or an explicit environment-key reference")
        if change.glm_api_key_action == "set":
            stored_key_origin = model_origin(candidate.glm_base_url)
        elif change.glm_api_key_action == "clear":
            stored_key_origin = ""
        revision = self.revision + 1
        if self.path:
            self._credential_id, self._persistence_warning = self._persist(
                candidate, stored_key, stored_key_origin, environment_binding, revision)
        self.configuration, self._stored_key, self.revision = candidate, stored_key, revision
        self._stored_key_origin = stored_key_origin
        self._environment_binding = environment_binding

    def _persist(self, configuration: ModelConfiguration, key: str, key_origin: str, environment_binding: dict, revision: int):
        reference = uuid.uuid4().hex if key else ""
        payload = {"schema_version": 1, "revision": revision,
                   "configuration": configuration.model_dump(), "stored_key_ref": reference,
                   "stored_key_origin": key_origin, "environment_key_binding": environment_binding}
        # Write a credential generation containing both references first. If the
        # configuration replacement fails, its prior reference is still valid.
        # A committed configuration never points at a not-yet-written secret.
        keys = {reference: key} if reference else {}
        if self._credential_id and self._stored_key:
            keys[self._credential_id] = self._stored_key
        durable = True
        try:
            if keys:
                durable = self._atomic_write(self.credential_path, {"schema_version": 1, "keys": keys}) and durable
            durable = self._atomic_write(self.path, payload) and durable
        except OSError:
            raise ModelConfigError("could not save model settings atomically") from None
        # Remove retired keys after the authoritative configuration commits.
        # A failed cleanup leaves an unused local secret, never a broken profile;
        # the next successful save retries this cleanup.
        if self.credential_path.exists() and durable:
            try:
                durable = self._atomic_write(self.credential_path, {
                    "schema_version": 1, "keys": {reference: key} if reference else {}}) and durable
            except OSError:
                pass
        warning = None if durable else "configuration was atomically replaced, but directory sync failed; crash durability is not confirmed"
        return reference, warning

    @staticmethod
    def _atomic_write(path: Path, payload: dict):
        temporary = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary = tempfile.mkstemp(prefix=".model-settings-", dir=path.parent)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                os.chmod(temporary, 0o600)
                json.dump(payload, stream, ensure_ascii=False, indent=2, allow_nan=False)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            temporary = None
            # The rename is the commit point. A subsequent directory fsync
            # failure must not leave memory claiming the old revision: future
            # credential rotations could otherwise delete the disk reference.
            if os.name == "posix":
                try:
                    directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
                    try:
                        os.fsync(directory_fd)
                    finally:
                        os.close(directory_fd)
                except OSError:
                    return False
            return True
        finally:
            if temporary:
                Path(temporary).unlink(missing_ok=True)
