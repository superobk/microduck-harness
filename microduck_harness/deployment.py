"""Render a reviewed two-host deployment from one non-secret TOML profile.

This command writes configuration and explicit launch scripts only. It never
connects to a robot, installs daemons, restarts services or enables motion.
Runtime model profiles and calibration measurements remain separate assets.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import sys
import tempfile
import tomllib
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .config import Settings
from .vision import checked_transform

TOOLS_REVISION = "2b93cb3afc32fa343cc612ee060e94c5bdd7b01d"
ROOT = Path(__file__).resolve().parents[1]


class DeploymentError(ValueError):
    pass


class Section(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Robot(Section):
    version: Literal["0.16.3", "0.17.8"] = "0.17.8"
    board: Literal["zero3"] = "zero3"
    native_config: str = "/etc/robot/robotd.toml"


class Network(Section):
    transport: Literal["ssh", "lan"] = "ssh"
    ssh_target: str = "YOUR_USER@YOUR_ZERO_HOST"
    zero_host: str = "YOUR_ZERO_IP"
    gateway_port: int = Field(8765, ge=1024, le=65535)
    executor_port: int = Field(8766, ge=1024, le=65535)
    vision_port: int = Field(8767, ge=1024, le=65535)
    tools_port: int = Field(8768, ge=1024, le=65535)

    @model_validator(mode="after")
    def validate_network(self):
        if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*@[A-Za-z0-9_][A-Za-z0-9_.-]*", self.ssh_target):
            raise ValueError("ssh_target must be user@hostname or user@IPv4, without options")
        if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", self.zero_host):
            raise ValueError("zero_host must be a hostname or IPv4 address without a port or URL")
        ports = [self.gateway_port, self.executor_port, self.vision_port, self.tools_port]
        if len(set(ports)) != len(ports):
            raise ValueError("gateway, executor, vision and tools ports must be distinct")
        return self


class Paths(Section):
    workstation_root: str = ".."  # relative to the TOML file
    workstation_data: str = "data/live"  # relative to workstation_root; separate from demo
    zero_root: str = "/opt/microduck-harness"
    workstation_user: str = "YOUR_PC_USER"
    zero_user: str = "YOUR_USER"

    @model_validator(mode="after")
    def validate_paths(self):
        for name in ("workstation_user", "zero_user"):
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]*", getattr(self, name)):
                raise ValueError("service account names must not contain options or whitespace")
        return self


class CalibrationPlugin(Section):
    path: str = "/etc/robot/robot-calibration.toml"
    runtime_receipt: str = "/run/robotd/calibration-effective.json"


class CameraPlugin(Section):
    rotation: Literal[0, 90, 180, 270] = 90
    runtime_receipt: str = "/run/mediad/camera-effective.json"


class Plugins(Section):
    required: bool = True
    calibration: CalibrationPlugin = Field(default_factory=CalibrationPlugin)
    camera: CameraPlugin = Field(default_factory=CameraPlugin)


class ModelService(Section):
    decision_mode: Literal["hybrid", "glm"] = "hybrid"
    base_url: str = ""
    model_id: str = Field("GLM5.3-Flash", min_length=1, max_length=200)
    timeout_s: float = Field(25, gt=0, le=120)
    json_mode: bool = True


class Jev(Section):
    url: str = "http://127.0.0.1:8149"
    timeout_s: float = Field(2, gt=0, le=15)
    threshold: float = Field(0.85, ge=0, le=1)
    margin: float = Field(0.20, ge=0, le=1)
    max_streak: int = Field(8, ge=1, le=50)


class Vision(Section):
    source: str = "aruco-lab.example.json"  # relative to the profile
    calibration_result: str = ""  # optional measured calibration JSON, same base


class Motion(Section):
    allow: bool = False
    bounds: tuple[float, float, float, float] = (-1, 1, -1, 1)
    boundary_margin_m: float = Field(0.20, ge=0.1, le=1)
    arrival_distance_m: float = Field(0.55, ge=0.2, le=2)
    min_clearance_m: float = Field(0.30, ge=0.15, le=2)
    speed_m_s: float = Field(0.06, gt=0, le=0.08)
    yaw_rate_rad_s: float = Field(0.4, gt=0, le=0.5)
    pulse_s: float = Field(0.6, gt=0, le=1)


class Deployment(Section):
    schema_version: Literal[1] = Field(default=1, alias="schema")
    robot: Robot = Field(default_factory=Robot)
    network: Network = Field(default_factory=Network)
    paths: Paths = Field(default_factory=Paths)
    plugins: Plugins = Field(default_factory=Plugins)
    model: ModelService = Field(default_factory=ModelService)
    agentjev: Jev = Field(default_factory=Jev)
    vision: Vision = Field(default_factory=Vision)
    motion: Motion = Field(default_factory=Motion)

    @model_validator(mode="after")
    def validate_profile(self):
        for path in (self.robot.native_config, self.paths.zero_root,
                     self.plugins.calibration.path, self.plugins.calibration.runtime_receipt,
                     self.plugins.camera.runtime_receipt):
            if not path.startswith("/") or any(ord(c) < 32 for c in path) or ".." in Path(path).parts:
                raise ValueError("board paths must be absolute and have no control characters or parent traversals")
        if self.robot.native_config == self.plugins.calibration.path:
            raise ValueError("calibration file must differ from native robotd config")
        for url in (self.model.base_url, self.agentjev.url):
            if not url:
                continue
            p = urlsplit(url)
            if (p.scheme not in {"http", "https"} or not p.hostname or p.username is not None
                    or p.password is not None or p.query or p.fragment
                    or any(c.isspace() or ord(c) < 32 for c in url)):
                raise ValueError("model endpoints must be HTTP(S) without credentials, query, fragment or whitespace")
            _ = p.port
        return self


def load_profile(path: Path) -> Deployment:
    try:
        return Deployment.model_validate(tomllib.loads(path.read_text(encoding="utf-8")))
    except Exception as exc:
        # Pydantic errors can contain submitted values. A profile is not a place
        # for secrets; still avoid echoing an accidental API-key field.
        if isinstance(exc, OSError):
            raise DeploymentError("cannot read deployment TOML") from None
        raise DeploymentError("invalid deployment TOML; check the documented sections, values, paths and distinct ports") from None


def resolved(path: str, base: Path) -> Path:
    if any(ord(c) < 32 for c in path):
        raise DeploymentError("paths cannot contain control characters")
    p = Path(path).expanduser()
    return (p if p.is_absolute() else base / p).resolve()


def json_text(value) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"


def validate_vision(cfg: dict) -> bool:
    if cfg.get("mode") != "camera" or cfg.get("rotate_upright") or not cfg.get("use_robot_camera_fk", True):
        raise DeploymentError("unified ArUco deployment requires camera mode, raw pixels and native camera FK")
    if not isinstance(cfg.get("markers"), dict) or not cfg["markers"]:
        raise DeploymentError("vision source must include measured landmark definitions")
    try:
        for marker in cfg["markers"].values():
            if not 0 < float(marker["size_m"]) <= 10:
                raise ValueError()
            checked_transform(marker["T_world_marker"])
        k, d, size = cfg.get("camera_matrix"), cfg.get("distortion"), cfg.get("image_size")
        if k is None or d is None:
            return False
        import math
        if (len(k) != 3 or any(len(row) != 3 for row in k)
                or any(not math.isfinite(float(v)) for row in k for v in row)
                or float(k[0][0]) <= 0 or float(k[1][1]) <= 0 or abs(float(k[2][2])-1) > 1e-6
                or len(d) not in {4, 5, 8, 12, 14} or any(not math.isfinite(float(v)) for v in d)
                or len(size) != 2 or any(type(v) is not int or v <= 0 for v in size)):
            raise ValueError()
    except (ValueError, TypeError, KeyError):
        raise DeploymentError("invalid landmark transform, measured camera matrix, distortion or image size") from None
    return True


def launch_script(project_root: str, command: list[str], *, needs_secrets=True) -> str:
    parts = ["#!/usr/bin/env bash", "set -euo pipefail",
             'SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"']
    if needs_secrets:
        parts += ['test -f "$SCRIPT_DIR/secrets.env" || { printf \'Missing local secrets.env\\n\' >&2; exit 2; }',
                  "set -a", '. "$SCRIPT_DIR/secrets.env"', "set +a"]
    parts += ["cd -- " + shlex.quote(project_root), "exec " + " ".join(command)]
    return "\n".join(parts) + "\n"


def unit(title: str, user: str, script: Path, *, board=False) -> str:
    def quote(value):
        return '"' + str(value).replace("%", "%%").replace("\\", "\\\\").replace('"', '\\"') + '"'
    return ("[Unit]\nDescription=" + title + "\nAfter=network-online.target"
            + (" robotd.service mediad.service tofd.service" if board else "")
            + "\n\n[Service]\nType=simple\nUser=" + user
            + ("\nSupplementaryGroups=robot" if board else "")
            + "\nExecStart=" + quote(script) + "\nRestart=on-failure\nRestartSec=3\n"
            + "TimeoutStopSec=8\nNoNewPrivileges=true\n\n[Install]\nWantedBy=multi-user.target\n")


def plan(profile_path: Path, output: Path) -> tuple[dict[str, str], dict]:
    cfg = load_profile(profile_path)
    base = profile_path.resolve().parent
    pc_root = resolved(cfg.paths.workstation_root, base)
    data = resolved(cfg.paths.workstation_data, pc_root)
    zero_root = Path(cfg.paths.zero_root)
    out = output.resolve()
    n = cfg.network
    host = "127.0.0.1" if n.transport == "ssh" else n.zero_host
    executor_url = f"http://{host}:{n.executor_port}"
    gw = {"mode": "live", "decision_mode": cfg.model.decision_mode,
          "executor_url": executor_url, "vision_url": f"http://127.0.0.1:{n.vision_port}",
          "tools_url": f"http://127.0.0.1:{n.tools_port}",
          "glm_base_url": cfg.model.base_url, "glm_model": cfg.model.model_id,
          "glm_timeout_s": cfg.model.timeout_s, "glm_json_mode": cfg.model.json_mode,
          "jev_url": cfg.agentjev.url, "jev_timeout_s": cfg.agentjev.timeout_s,
          "jev_threshold": cfg.agentjev.threshold, "jev_margin": cfg.agentjev.margin,
          "jev_max_streak": cfg.agentjev.max_streak,
          **cfg.motion.model_dump(exclude={"allow"}),
          "database": str(data / "harness.sqlite3"),
          "model_config_file": str(data / "model-settings.json")}
    Settings.model_validate({**gw, "gateway_token": "validation-only", "vision_token": "validation-only",
                             "executor_token": "validation-only"})
    try:
        vision = json.loads(resolved(cfg.vision.source, base).read_text(encoding="utf-8"))
        if cfg.vision.calibration_result:
            calibration = json.loads(resolved(cfg.vision.calibration_result, base).read_text(encoding="utf-8"))
            vision.update({name: calibration[name] for name in ("camera_matrix", "distortion", "image_size")})
    except (OSError, ValueError, KeyError, TypeError):
        raise DeploymentError("cannot read the vision source or its measured calibration result") from None
    vision_ready = validate_vision(vision)
    vision.update(camera_url=executor_url + "/frame", state_url=executor_url + "/state",
                  tof_url=executor_url + "/tof", executor_token_env="EXECUTOR_TOKEN")
    executor = {"robotd_version": cfg.robot.version,
                "bind_host": "127.0.0.1" if n.transport == "ssh" else "0.0.0.0",
                "allow_motion": cfg.motion.allow, "require_native_plugins": cfg.plugins.required,
                "native_config_path": cfg.robot.native_config,
                "calibration_runtime_path": cfg.plugins.calibration.runtime_receipt,
                "camera_runtime_path": cfg.plugins.camera.runtime_receipt}
    native = ("# Overlay only: merge reviewed keys into the existing native config.\n"
              "# This is not a complete robotd configuration or a calibration measurement.\n"
              "[bus]\ncalibration_path = " + json.dumps(cfg.plugins.calibration.path)
              + "\n\n[media]\ncamera_rotate = " + str(cfg.plugins.camera.rotation) + "\n")
    commands = {
        "pc/start-gateway.sh": launch_script(str(pc_root),
            [shlex.quote(str(pc_root / ".venv/bin/duck-harness")), "gateway", "--config", '"$SCRIPT_DIR/gateway.json"',
             "--host", "127.0.0.1", "--port", str(n.gateway_port)]),
        "pc/start-vision.sh": launch_script(str(pc_root),
            [shlex.quote(str(pc_root / ".venv/bin/duck-harness")), "vision", "--config", '"$SCRIPT_DIR/vision.json"',
             "--host", "127.0.0.1", "--port", str(n.vision_port)]),
        "zero/start-executor.sh": launch_script(str(zero_root),
            [shlex.quote(str(zero_root / ".venv/bin/duck-harness")), "executor", "--config", '"$SCRIPT_DIR/executor.json"',
             "--host", executor["bind_host"], "--port", str(n.executor_port)]),
        "zero/start-tools.sh": launch_script(str(zero_root),
            ["python3", shlex.quote(str(zero_root / "scripts/tools_web.py")), "--target", "daemon-v" + cfg.robot.version,
             "--config", shlex.quote(cfg.robot.native_config), "--dest", shlex.quote(cfg.plugins.calibration.path),
             "--runtime", shlex.quote(cfg.plugins.calibration.runtime_receipt),
             "--camera-runtime", shlex.quote(cfg.plugins.camera.runtime_receipt), "--port", str(n.tools_port)],
             needs_secrets=False),
    }
    tunnel = ["ssh", "-N", "-o", "ExitOnForwardFailure=yes", "-o", "ServerAliveInterval=15",
              "-o", "ServerAliveCountMax=3"]
    if n.transport == "ssh":
        tunnel += ["-L", f"127.0.0.1:{n.executor_port}:127.0.0.1:{n.executor_port}"]
    tunnel += ["-L", f"127.0.0.1:{n.tools_port}:127.0.0.1:{n.tools_port}", n.ssh_target]
    commands["pc/open-tunnel.sh"] = "#!/usr/bin/env bash\nset -euo pipefail\nexec " + shlex.join(tunnel) + "\n"
    notes = []
    if not vision_ready:
        notes.append("Camera K/D are not yet measured; render succeeded but ArUco positioning is not ready.")
    if not cfg.model.base_url:
        notes.append("GLM endpoint is empty; configure it in the authenticated workbench before starting goals.")
    if any("YOUR_" in s for s in (n.ssh_target, n.zero_host if n.transport == "lan" else "",
                                   cfg.paths.zero_user, cfg.paths.workstation_user)):
        notes.append("Replace YOUR_* host/account placeholders before connecting or installing system services.")
    notes.append("Native plugin runtime readiness is checked on ZERO; rendering files is not activation.")
    if cfg.motion.allow:
        notes.append("Executor configuration permits bounded motion after all runtime checks; rendering sends no action.")
    summary = {"schema": 1, "tools_revision": TOOLS_REVISION, "robotd_version": cfg.robot.version,
               "profile": str(profile_path.resolve()), "profile_sha256": hashlib.sha256(profile_path.read_bytes()).hexdigest(),
               "workstation_root": str(pc_root), "zero_root": str(zero_root),
               "native_config": cfg.robot.native_config, "calibration_path": cfg.plugins.calibration.path,
               "transport": n.transport, "executor_url": executor_url,
               "gateway_url": f"http://127.0.0.1:{n.gateway_port}",
               "tools_url": f"http://127.0.0.1:{n.tools_port}",
               "vision_calibrated_fields_present": vision_ready, "motion_allowed_in_config": cfg.motion.allow,
               "native_plugins_required": cfg.plugins.required, "notes": notes}
    files = {".gitignore": "# Generated local deployment: contains host paths and private credentials.\n*\n",
             "pc/gateway.json": json_text(gw), "pc/vision.json": json_text(vision),
             "zero/executor.json": json_text(executor), "zero/native-overlay.toml": native,
             "deployment.json": json_text(summary), **commands,
             "pc/microduck-gateway.service": unit("Microduck autonomy gateway", cfg.paths.workstation_user, out / "pc/start-gateway.sh"),
             "pc/microduck-vision.service": unit("Microduck vision", cfg.paths.workstation_user, out / "pc/start-vision.sh"),
             "zero/microduck-executor.service": unit("Microduck bounded executor", cfg.paths.zero_user,
                    zero_root / "deploy/generated/zero/start-executor.sh", board=True)}
    return files, summary


def atomic_text(path: Path, content: str, mode: int):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".deploy-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            os.fchmod(f.fileno(), mode)
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def render(profile: Path, output: Path) -> dict:
    files, summary = plan(profile, output)
    output = output.resolve()
    manifest = output / "render-manifest.json"
    previous = {}
    if manifest.exists():
        previous = json.loads(manifest.read_text()).get("files", {})
    for name, content in files.items():
        dest = output / name
        if dest.is_symlink() or not dest.resolve().is_relative_to(output):
            raise DeploymentError("refusing a symlink in the generated deployment")
        if dest.exists():
            old = hashlib.sha256(dest.read_bytes()).hexdigest()
            new = hashlib.sha256(content.encode()).hexdigest()
            if old != new and previous.get(name) != old:
                raise DeploymentError(f"generated file has local edits; preserve it and render to a new directory: {name}")
    hashes = {name: hashlib.sha256(content.encode()).hexdigest() for name, content in files.items()}
    for name, content in files.items():
        atomic_text(output / name, content, 0o700 if name.endswith(".sh") else 0o600)
    atomic_text(manifest, json_text({"schema": 1, "files": hashes}), 0o600)
    return {**summary, "output": str(output), "files_written": len(files)}


def create_secrets(output: Path) -> dict:
    """Split credentials by host. Never copy GLM/Gateway credentials to ZERO."""
    output = output.resolve()
    if not (output / "render-manifest.json").is_file():
        raise DeploymentError("render a deployment bundle before creating credentials")
    destinations = [output / "pc/secrets.env", output / "zero/secrets.env"]
    if any(not p.resolve().is_relative_to(output) for p in destinations):
        raise DeploymentError("refusing a symlink outside the generated deployment")
    if any(p.exists() or p.is_symlink() for p in destinations):
        raise DeploymentError("credentials already exist; they were not overwritten or rotated")
    executor, gateway, vision = (secrets.token_urlsafe(32) for _ in range(3))
    pc = ("# Private operator environment. Never commit this file.\n"
          + "EXECUTOR_TOKEN=" + shlex.quote(executor) + "\nGATEWAY_TOKEN=" + shlex.quote(gateway)
          + "\nVISION_TOKEN=" + shlex.quote(vision) + "\nGLM_API_KEY=''\n")
    atomic_text(destinations[0], pc, 0o600)
    atomic_text(destinations[1], "EXECUTOR_TOKEN=" + shlex.quote(executor) + "\n", 0o600)
    return {"created": [str(p) for p in destinations], "permissions": "0600", "secret_values_returned": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("validate", "render"):
        p = sub.add_parser(name)
        p.add_argument("--config", type=Path, default=Path("config/deployment.local.toml"))
        p.add_argument("--out", type=Path, default=Path("build/deployment"))
    p = sub.add_parser("secrets")
    p.add_argument("--out", type=Path, default=Path("build/deployment"))
    args = parser.parse_args()
    try:
        if args.command == "secrets":
            result = create_secrets(args.out)
        elif args.command == "render":
            result = render(args.config, args.out)
        else:
            _, result = plan(args.config, args.out)
            result = {"configuration_valid": True, **result}
        print(json_text(result), end="")
        return 0
    except DeploymentError as exc:
        print(str(exc) + ". No daemon or robot action was requested.", file=sys.stderr)
        return 2
    except (OSError, ValueError):
        # Do not include arbitrary parser contents or accidentally supplied secrets.
        print("Deployment command failed. Check the profile schema, paths, JSON calibration, port conflicts, "
              "or existing generated-file edits/credentials. No daemon or robot action was requested.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
