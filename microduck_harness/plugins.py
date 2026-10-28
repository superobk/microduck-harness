"""Read-only evidence that native calibration/camera plugins are actually active.

The native daemons remain the only place where joint/gyro mappings are applied.
Receipts are startup snapshots, not heartbeats. Their lifetime is bound to the
current systemd MainPID and /proc start time, rather than an arbitrary age limit.
"""
from __future__ import annotations

from dataclasses import dataclass
import errno
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import subprocess
import threading
import time
import tomllib
from typing import Callable


TARGETS = {"daemon-v0.16.3", "daemon-v0.17.8"}
GYRO_PROFILES = {
    "st_125dps": 1 / .004375, "st_250dps": 1 / .00875,
    "st_500dps": 1 / .0175, "st_1000dps": 1 / .035,
    "st_2000dps": 1 / .070, "st_4000dps": 1 / .140,
    "custom_raw_div_16_4": 16.4,
}


class PluginEvidenceError(ValueError):
    """Only fixed, non-secret diagnostics may be placed in this exception."""


@dataclass(frozen=True)
class PluginPaths:
    native_config_path: str = "/etc/robot/robotd.toml"
    calibration_runtime_path: str = "/run/robotd/calibration-effective.json"
    camera_runtime_path: str = "/run/mediad/camera-effective.json"


def _read_mapping(path: Path, kind: str) -> tuple[dict, os.stat_result]:
    # No endpoints accept paths. These names come only from local deployment
    # configuration. Open once, reject links/devices and bound content size.
    fd = None
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | os.O_NONBLOCK)
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size > 256_000:
            raise PluginEvidenceError("plugin evidence must be a bounded regular file")
        with os.fdopen(fd, "rb") as stream:
            fd = None
            raw = stream.read(256_001)
            after = os.fstat(stream.fileno())
        if (len(raw) > 256_000 or before.st_mtime_ns != after.st_mtime_ns
                or before.st_size != after.st_size):
            raise PluginEvidenceError("plugin evidence changed while being read")
        value = tomllib.loads(raw.decode("utf-8")) if kind == "toml" else json.loads(raw)
        if not isinstance(value, dict):
            raise PluginEvidenceError("plugin evidence must be a mapping")
        return value, after
    except PluginEvidenceError:
        raise
    except (OSError, ValueError, UnicodeError):
        # Parser exceptions quote input. Never echo any file content to HTTP.
        raise PluginEvidenceError("plugin evidence is missing, unreadable or malformed") from None
    finally:
        if fd is not None:
            os.close(fd)


def _number(value, label: str) -> float:
    if type(value) not in (int, float) or not math.isfinite(value):
        raise PluginEvidenceError(label + " must be finite numeric data")
    return float(value)


def _vector(value, length: int, label: str) -> list[float]:
    if not isinstance(value, list) or len(value) != length:
        raise PluginEvidenceError(label + " has the wrong number of values")
    return [_number(x, label) for x in value]


def _calibration_values(value: dict, *, saved: bool) -> dict:
    """Match the pinned microduck-tools schema; do not import/run its installer."""
    schema = value.get("schema", 2)
    if saved:
        common = {"schema", "target_tag", "joint_zero_rad", "joint_direction", "imu_mount_wxyz"}
        extended = {"imu_module", "gyro_profile", "gyro_raw_counts_per_dps"}
        if type(schema) is not int or schema not in (1, 2) or set(value) != common | (extended if schema == 2 else set()):
            raise PluginEvidenceError("calibration must match microduck-tools schema 1 or 2")
    zeros = _vector(value.get("joint_zero_rad"), 15, "joint_zero_rad")
    if any(abs(x) > .35 for x in zeros):
        raise PluginEvidenceError("software joint zero exceeds 0.35 radians")
    directions = value.get("joint_direction")
    if not isinstance(directions, list) or len(directions) != 15 or any(type(x) is not int or x not in (-1, 1) for x in directions):
        raise PluginEvidenceError("joint_direction requires fifteen signed unit integers")
    mount = _vector(value.get("imu_mount_wxyz"), 4, "imu_mount_wxyz")
    if abs(sum(x*x for x in mount) - 1) > 1e-4:
        raise PluginEvidenceError("IMU mounting quaternion is not normalized")
    counts = GYRO_PROFILES["st_500dps"] if saved and schema == 1 else _number(value.get("gyro_raw_counts_per_dps"), "gyro sensitivity")
    module = "bus_imu200" if saved and schema == 1 else value.get("imu_module")
    if module != "bus_imu200" or not 5 <= counts <= 300:
        raise PluginEvidenceError("unsupported IMU module or gyro sensitivity")
    if saved and schema == 2:
        profile = value.get("gyro_profile")
        if not isinstance(profile, str) or profile not in {*GYRO_PROFILES, "manual"}:
            raise PluginEvidenceError("unknown gyro profile")
        if profile != "manual" and not math.isclose(counts, GYRO_PROFILES[profile], abs_tol=1e-5, rel_tol=0):
            raise PluginEvidenceError("gyro profile and sensitivity disagree")
    tag = value.get("target_tag")
    if tag not in TARGETS:
        raise PluginEvidenceError("unsupported native plugin target")
    return {"target_tag": tag, "joint_zero_rad": zeros, "joint_direction": directions,
            "imu_mount_wxyz": mount, "imu_module": module, "gyro_raw_counts_per_dps": counts}


def _same_values(left: dict, right: dict) -> bool:
    for key in ("target_tag", "joint_direction", "imu_module"):
        if left[key] != right[key]:
            return False
    for key in ("joint_zero_rad", "imu_mount_wxyz"):
        if any(not math.isclose(a, b, abs_tol=1e-7, rel_tol=0) for a, b in zip(left[key], right[key])):
            return False
    return math.isclose(left["gyro_raw_counts_per_dps"], right["gyro_raw_counts_per_dps"], abs_tol=1e-7, rel_tol=0)


class ProcessVerifier:
    """Verify Linux daemon identity without signals or service mutations.

    Only the systemd property snapshot is cached (200 ms). /proc process/start
    identity is read for every receipt, including every motion refresh. When
    ptrace permissions hide another UID's exe link, the official direct-launch
    systemd contract is a separate, strictly checked executable evidence path.
    """

    def __init__(self):
        self._units: dict[str, tuple[float, dict[str, str]]] = {}
        self._lock = threading.Lock()

    def _systemd_properties(self, name: str) -> dict[str, str]:
        with self._lock:
            old = self._units.get(name)
            if old and time.monotonic() - old[0] < .2:
                return old[1]
        try:
            required = {"MainPID", "ExecMainPID", "ExecStart", "Type", "ActiveState", "SubState"}
            result = subprocess.run(["systemctl", "show", name + ".service", "--property=" + ",".join(sorted(required))],
                                    capture_output=True, text=True, timeout=.15, check=True)
            if len(result.stdout) > 32_000:
                raise ValueError("oversize systemd response")
            values = {}
            for line in result.stdout.splitlines():
                key, separator, value = line.partition("=")
                if not separator or key not in required or key in values:
                    raise ValueError("unexpected systemd property")
                values[key] = value
            if set(values) != required:
                raise ValueError("incomplete systemd response")
        except (OSError, ValueError, subprocess.SubprocessError):
            raise PluginEvidenceError("current systemd daemon identity could not be verified") from None
        with self._lock:
            self._units[name] = (time.monotonic(), values)
        return values

    def _systemd_pid(self, name: str) -> int:
        try:
            return int(self._systemd_properties(name)["MainPID"])
        except (ValueError, KeyError):
            raise PluginEvidenceError("current systemd daemon identity could not be verified") from None

    def _verify_systemd_executable(self, name: str, pid: int) -> None:
        """Accept only the official Type=exec direct command for this live PID.

        A unit's configured command alone is insufficient: ExecMainPID and the
        ExecStart runtime PID must both identify the same currently running main
        process. No shell/wrapper, alternate argv[0], path guessing or fallback
        to a receipt's executable claim is allowed.
        """
        try:
            unit = self._systemd_properties(name)
            if (unit["Type"] != "exec" or unit["ActiveState"] != "active" or unit["SubState"] != "running"
                    or int(unit["MainPID"]) != pid or int(unit["ExecMainPID"]) != pid):
                raise ValueError("unit is not the expected running main process")
            command = unit["ExecStart"]
            if not command.startswith("{") or not command.endswith("}") or command.count("{") != 1 or command.count("}") != 1:
                raise ValueError("expected exactly one native command")
            fields = {}
            for part in command[1:-1].split(";"):
                key, separator, value = part.strip().partition("=")
                if not separator or key in fields:
                    raise ValueError("ambiguous native command")
                fields[key] = value.strip()
            if set(fields) != {"path", "argv[]", "ignore_errors", "start_time", "stop_time", "pid", "code", "status"}:
                raise ValueError("unknown native command representation")
            # Both supported upstream tags ship exactly this direct launch path.
            # The current symlink belongs to the native release installer. Other
            # layouts need readable /proc/exe or an explicitly reviewed contract.
            expected = f"/opt/robot/daemon/current/bin/{name}"
            argv = fields["argv[]"].split()
            if (fields["path"] != expected or not argv or argv[0] != expected
                    or fields["ignore_errors"] != "no" or int(fields["pid"]) != pid):
                raise ValueError("native executable identity is not direct and exact")
        except (ValueError, KeyError):
            raise PluginEvidenceError("permission-restricted daemon executable lacks trusted systemd identity") from None

    def __call__(self, name: str, pid: int, receipt_stat: os.stat_result) -> dict:
        if name not in {"robotd", "mediad"} or type(pid) is not int or pid <= 1:
            raise PluginEvidenceError("native receipt has an invalid process identity")
        if self._systemd_pid(name) != pid:
            raise PluginEvidenceError("receipt PID does not match the current systemd daemon")
        try:
            evidence = "proc_exe"
            try:
                executable = os.readlink(f"/proc/{pid}/exe")
            except OSError as exc:
                if exc.errno not in {errno.EACCES, errno.EPERM}:
                    raise
                self._verify_systemd_executable(name, pid)
                evidence = "systemd_exec"
            else:
                if Path(executable).name != name:
                    raise PluginEvidenceError("receipt executable is not the expected native daemon")
            header, raw_fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)
            if evidence == "systemd_exec" and header != f"{pid} ({name}":
                raise PluginEvidenceError("permission-restricted process name is not the native daemon")
            fields = raw_fields.split()
            if fields[0] in {"Z", "X", "x"}:
                raise PluginEvidenceError("receipt daemon is not alive")
            start_ticks = int(fields[19])  # /proc stat field 22, relative to field 3.
            ticks_per_second = os.sysconf("SC_CLK_TCK")
            boot_elapsed = time.clock_gettime(time.CLOCK_BOOTTIME)
            started_at = time.time() - boot_elapsed + start_ticks / ticks_per_second
            # Native receipts are not periodic. A days-old receipt is valid for
            # its same running process; a receipt predating a reused PID is not.
            if receipt_stat.st_mtime < started_at - 1 / ticks_per_second:
                raise PluginEvidenceError("receipt predates the current process; stale PID or reboot")
            if receipt_stat.st_mtime > time.time() + 1:
                raise PluginEvidenceError("receipt timestamp is in the future")
            return {"pid": pid, "start_ticks": start_ticks, "executable": name,
                    "executable_evidence": evidence, "receipt_mtime_ns": receipt_stat.st_mtime_ns}
        except PluginEvidenceError:
            raise
        except (OSError, ValueError, IndexError, AttributeError):
            raise PluginEvidenceError("native process start identity could not be verified") from None


def unavailable(required: bool, reason: str) -> dict:
    entry = {"status": "unavailable", "saved": None, "active": None, "reasons": [reason]}
    return {"required": required, "ready": not required, "status": "unavailable" if required else "disabled",
            "generation": None, "calibration": dict(entry), "camera": dict(entry)}


class NativePluginInspector:
    def __init__(self, paths: PluginPaths, *, required: bool = False, robotd_version: str = "auto",
                 process_verifier: Callable | None = None):
        self.paths = paths
        self.required = required
        self.robotd_version = robotd_version
        self.verify_process = process_verifier or ProcessVerifier()

    def inspect(self, firmware: dict | None = None) -> dict:
        try:
            configured = self.robotd_version
            actual = firmware.get("profile_version") if isinstance(firmware, dict) else None
            if configured == "auto":
                if actual not in {"0.16.3", "0.17.8"}:
                    raise PluginEvidenceError("read native state before checking an automatic plugin target")
                configured = actual
            if actual is not None and actual != configured:
                raise PluginEvidenceError("native firmware and selected plugin target differ")
            target = "daemon-v" + configured
            if target not in TARGETS:
                raise PluginEvidenceError("native plugin target is not supported")
            config, _ = _read_mapping(Path(self.paths.native_config_path), "toml")
        except PluginEvidenceError as exc:
            return unavailable(self.required, str(exc))

        calibration = self._calibration(config, target)
        camera = self._camera(config, target)
        active = calibration["status"] == camera["status"] == "active"
        generation = None
        if active:
            data = {"calibration": calibration, "camera": camera}
            generation = hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
        statuses = {calibration["status"], camera["status"]}
        status = ("active" if active else "pending" if "pending" in statuses else
                  "saved" if "saved" in statuses else "unavailable")
        return {"required": self.required, "ready": active or not self.required,
                "status": status if self.required else "disabled", "generation": generation,
                "calibration": calibration, "camera": camera}

    def _calibration(self, config: dict, target: str) -> dict:
        result = {"status": "unavailable", "saved": None, "active": None, "reasons": []}
        try:
            section = config.get("bus")
            path_text = section.get("calibration_path") if isinstance(section, dict) else None
            if not isinstance(path_text, str) or not path_text or not Path(path_text).is_absolute():
                raise PluginEvidenceError("bus.calibration_path must select an absolute calibration file")
            saved, _ = _read_mapping(Path(path_text), "toml")
            expected = _calibration_values(saved, saved=True)
            if expected["target_tag"] != target:
                raise PluginEvidenceError("saved calibration targets a different native version")
            result["saved"] = {**expected, "schema": saved["schema"], "calibration_path": path_text}
            result["status"] = "saved"
            receipt, receipt_stat = _read_mapping(Path(self.paths.calibration_runtime_path), "json")
            if receipt.get("source") != "robotd:DynamixelIo:live" or receipt.get("bus_verified") is not True:
                raise PluginEvidenceError("calibration receipt is not a verified live bus snapshot")
            identity = self.verify_process("robotd", receipt.get("pid"), receipt_stat)
            effective = _calibration_values(receipt, saved=False)
            if effective["target_tag"] != target:
                raise PluginEvidenceError("running calibration targets a different native version")
            active_path = receipt.get("calibration_path")
            if not isinstance(active_path, str) or not Path(active_path).is_absolute():
                raise PluginEvidenceError("running calibration has no absolute file identity")
            result["active"] = {**effective, "identity": identity, "calibration_path": active_path}
            if Path(active_path).resolve() != Path(path_text).resolve() or not _same_values(expected, effective):
                result.update(status="pending", reasons=["saved calibration differs from the running decoder; explicit native restart is required"])
            else:
                result["status"] = "active"
        except PluginEvidenceError as exc:
            result["reasons"] = [str(exc)]
        except (OSError, ValueError, TypeError, KeyError):
            result["reasons"] = ["calibration evidence could not be verified"]
        return result

    def _camera(self, config: dict, target: str) -> dict:
        result = {"status": "unavailable", "saved": None, "active": None, "reasons": []}
        try:
            media, board = config.get("media", {}), config.get("board", {})
            if not isinstance(media, dict) or not isinstance(board, dict):
                raise PluginEvidenceError("native camera configuration is malformed")
            configured = media.get("camera_rotate")
            if configured is not None and (type(configured) is not int or configured not in {0, 90, 180, 270}):
                raise PluginEvidenceError("camera rotation must be a quarter turn")
            board_version = board.get("version", "zero3")
            if board_version not in {"zero3", "beta"}:
                raise PluginEvidenceError("unknown camera board default")
            expected = configured if configured is not None else (90 if board_version == "zero3" else 0)
            result.update(status="saved", saved={"camera_rotate": configured, "expected_rotation_degrees": expected})
            receipt, receipt_stat = _read_mapping(Path(self.paths.camera_runtime_path), "json")
            if (receipt.get("source") != "mediad:camera:live" or receipt.get("pipeline_started") is not True
                    or receipt.get("target_tag") != target):
                raise PluginEvidenceError("camera receipt is not a matching live pipeline snapshot")
            identity = self.verify_process("mediad", receipt.get("pid"), receipt_stat)
            active_path = receipt.get("config_path")
            if not isinstance(active_path, str) or Path(active_path).resolve() != Path(self.paths.native_config_path).resolve():
                raise PluginEvidenceError("running camera uses a different native config file")
            rotation = receipt.get("rotation_degrees")
            frame_rotation = receipt.get("frame_rotation_degrees")
            if type(rotation) is not int or rotation not in {0, 90, 180, 270} or type(frame_rotation) is not int:
                raise PluginEvidenceError("camera runtime rotation is malformed")
            if receipt.get("flip_in_pipeline") is not False:
                raise PluginEvidenceError("pipeline pixel rotation is incompatible with the raw-camera K/FK contract")
            if frame_rotation != rotation:
                raise PluginEvidenceError("raw frame metadata and camera rotation disagree")
            if receipt.get("rotation_source") not in {"configuration", "board_default", "command_line"}:
                raise PluginEvidenceError("camera rotation source is unknown")
            if any(type(receipt.get(k)) is not int or not 0 < receipt[k] <= 16384 for k in ("width", "height")):
                raise PluginEvidenceError("camera runtime image dimensions are invalid")
            result["active"] = {"rotation_degrees": rotation, "rotation_source": receipt["rotation_source"],
                                "frame_rotation_degrees": frame_rotation, "flip_in_pipeline": False,
                                "width": receipt["width"], "height": receipt["height"], "identity": identity}
            if receipt["rotation_source"] == "command_line":
                result.update(status="pending", reasons=["camera command-line rotation overrides unified configuration"])
            elif rotation != expected:
                result.update(status="pending", reasons=["saved camera rotation differs from the running pipeline; explicit mediad restart is required"])
            else:
                result["status"] = "active"
        except PluginEvidenceError as exc:
            result["reasons"] = [str(exc)]
        except (OSError, ValueError, TypeError, KeyError):
            result["reasons"] = ["camera evidence could not be verified"]
        return result
