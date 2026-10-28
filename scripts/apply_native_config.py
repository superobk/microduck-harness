#!/usr/bin/env python3
"""Merge the generated calibration/camera overlay into an existing native TOML.

Default is a dry run. --apply edits and backs up that file only. It does not
install calibration measurements, restart services, access a bus, or enable a
robot. The calibration file must have been installed and reviewed separately.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import tomllib

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from integrations.microduck_tools.bridge import load_components


def prepare(config_path: Path, overlay_path: Path, target: str):
    cal, camera = load_components()
    if config_path.is_symlink() or not config_path.is_file() or overlay_path.is_symlink():
        raise ValueError("existing regular native config and overlay files are required")
    overlay = tomllib.loads(overlay_path.read_text(encoding="utf-8"))
    if (set(overlay) != {"bus", "media"} or set(overlay["bus"]) != {"calibration_path"}
            or set(overlay["media"]) != {"camera_rotate"}):
        raise ValueError("overlay may only set bus.calibration_path and media.camera_rotate")
    dest = Path(overlay["bus"]["calibration_path"])
    if not dest.is_absolute() or dest.is_symlink() or dest.resolve() == config_path.resolve():
        raise ValueError("calibration must be a distinct, installed absolute regular file")
    if dest.name.startswith("demo-"):
        raise ValueError("bundled demo calibration cannot become a live configuration")
    calibration = cal.parse_calibration(dest, target)
    original = config_path.read_text(encoding="utf-8")
    before = tomllib.loads(original)
    if before.get("board", {}).get("version", "zero3") != "zero3":
        raise ValueError("this deployment targets the complete ZERO3W / 15-servo robot")
    updated = cal.replace_bus_key(original, "calibration_path", str(dest))
    updated = camera.edit(updated, overlay["media"]["camera_rotate"])
    expected = dict(before)
    expected["bus"] = {**before.get("bus", {}), "calibration_path": str(dest)}
    expected["media"] = {**before.get("media", {}), "camera_rotate": overlay["media"]["camera_rotate"]}
    if tomllib.loads(updated) != expected:
        raise ValueError("merge would alter unrelated native settings")
    return cal, calibration, original, updated


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("/etc/robot/robotd.toml"))
    parser.add_argument("--overlay", type=Path, required=True)
    parser.add_argument("--target", choices=["daemon-v0.16.3", "daemon-v0.17.8"], required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm-firmware-scale", action="store_true")
    args = parser.parse_args(argv)
    try:
        cal, calibration, original, updated = prepare(args.config, args.overlay, args.target)
        gyro = cal.effective_gyro(calibration)
        if args.apply and gyro["gyro_profile"] != cal.DEFAULT_GYRO_PROFILE and not args.confirm_firmware_scale:
            raise ValueError("custom IMU raw scale requires --confirm-firmware-scale after firmware verification")
        backup = None
        if args.apply and original != updated:
            backup = cal.make_backup(args.config)
            cal.atomic_write(args.config, updated.encode("utf-8"), args.config.stat().st_mode & 0o777)
        print(json.dumps({"mode": "applied" if args.apply else "dry_run", "changed": original != updated,
                          "config": str(args.config), "target": args.target,
                          "backup": str(backup) if backup else None,
                          "keys": ["bus.calibration_path", "media.camera_rotate"],
                          "services_restarted": False, "motion_requested": False}, indent=2))
        return 0
    except Exception:
        # Config files can contain unrelated local values. Keep errors concise.
        parser.exit(2, "Native merge refused: check the existing config, installed measured calibration, target, "
                       "two-key overlay and explicit custom-gyro confirmation. Nothing was restarted.\n")


if __name__ == "__main__":
    sys.exit(main())
