#!/usr/bin/env python3
"""Offline validation of the printable landmark / camera configuration contract."""
from __future__ import annotations
import argparse
import json
import math
from pathlib import Path
import sys

from microduck_harness.vision import checked_transform


def check(config: dict, printed_width_mm=None, printed_height_mm=None, scale_bar_mm=None) -> dict:
    errors, warnings = [], []
    if config.get("dictionary") != "DICT_4X4_50":
        errors.append("This print pack requires dictionary DICT_4X4_50")
    matrix, size = config.get("camera_matrix"), config.get("image_size")
    try:
        if len(matrix) != 3 or any(len(row) != 3 for row in matrix):
            raise ValueError()
        if not all(math.isfinite(float(v)) for row in matrix for v in row):
            raise ValueError()
        if matrix[0][0] <= 0 or matrix[1][1] <= 0 or abs(matrix[2][2]-1) > 1e-6:
            raise ValueError()
        if len(size) != 2 or any(not isinstance(v, int) or v <= 0 for v in size):
            raise ValueError()
        if not 0 <= matrix[0][2] < size[0] or not 0 <= matrix[1][2] < size[1]:
            warnings.append("Principal point is outside the image; review calibration and pixel orientation")
    except (TypeError, ValueError, IndexError):
        errors.append("Fill measured camera_matrix and matching original image_size")
    distortion = config.get("distortion")
    if (not isinstance(distortion, list) or len(distortion) not in {4, 5, 8, 12, 14}
            or any(not isinstance(v, (int, float)) or not math.isfinite(v) for v in distortion)):
        errors.append("Fill measured distortion coefficients (4, 5, 8, 12 or 14 finite values)")
    markers = config.get("markers", {})
    if set(markers) != {"0", "1", "2", "3"}:
        errors.append("This four-marker print pack expects exactly IDs 0, 1, 2 and 3")
    for identifier, marker in markers.items():
        try:
            checked_transform(marker["T_world_marker"])
            length = float(marker["size_m"])
            if not math.isfinite(length) or length <= 0:
                raise ValueError()
            if abs(length-.16) > .0005:
                errors.append(f"Marker {identifier}: size_m differs from the 160 mm print pack")
        except (TypeError, ValueError, KeyError):
            errors.append(f"Marker {identifier}: invalid size or rigid world transform")
    for name, value, expected in (("printed width", printed_width_mm, 160),
                                  ("printed height", printed_height_mm, 160),
                                  ("scale bar", scale_bar_mm, 100)):
        if value is not None and (not math.isfinite(value) or abs(value-expected) > .5):
            errors.append(f"{name} differs from {expected} mm by more than 0.5 mm; review print scale")
    if printed_width_mm is None or printed_height_mm is None or scale_bar_mm is None:
        warnings.append("Physical print dimensions have not been supplied; measure both black sides and the 100 mm bar")
    if config.get("rotate_upright") and config.get("use_robot_camera_fk", True):
        errors.append("Rotated pixels cannot use the unmodified native camera FK")
    warnings.append("Offline checks do not verify physical mounting, landmark visibility, ToF or robot readiness")
    return {"configuration_ready": not errors, "errors": errors, "warnings": warnings}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config/aruco-lab.example.json")
    parser.add_argument("--printed-width-mm", type=float)
    parser.add_argument("--printed-height-mm", type=float)
    parser.add_argument("--scale-bar-mm", type=float)
    args = parser.parse_args()
    try:
        result = check(json.loads(Path(args.config).read_text()), args.printed_width_mm,
                       args.printed_height_mm, args.scale_bar_mm)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["configuration_ready"] else 2


if __name__ == "__main__":
    sys.exit(main())
