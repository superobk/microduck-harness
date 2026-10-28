#!/usr/bin/env python3
"""Capture native camera PNGs or calibrate the supplied 8 x 5 inner-corner board.

Capture reads only GET /frame; it never enables torque or sends motion commands.
Calibration writes a separate result, never a live vision configuration.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import time
from urllib.parse import urlsplit


INNER_CORNERS = (8, 5)
SQUARE_M = .020
MIN_VIEWS = 12


def find_corners(gray):
    import cv2
    flags = cv2.CALIB_CB_NORMALIZE_IMAGE
    found, corners = cv2.findChessboardCornersSB(gray, INNER_CORNERS, flags=flags)
    if found:
        return corners
    found, corners = cv2.findChessboardCorners(
        gray, INNER_CORNERS, flags=flags | cv2.CALIB_CB_ADAPTIVE_THRESH)
    if not found:
        return None
    return cv2.cornerSubPix(gray, corners, (5, 5), (-1, -1),
                           (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_MAX_ITER, 40, .001))


def calibrate(images: Path) -> dict:
    """Estimate pinhole K/D from original-size, diverse PNG observations."""
    import cv2
    import numpy as np

    files = sorted(p for p in images.iterdir() if p.is_file() and p.suffix.lower() == ".png")
    if not files:
        raise ValueError("No PNG images found; use original unrotated, unscaled camera captures")
    object_points = np.zeros((INNER_CORNERS[0]*INNER_CORNERS[1], 3), np.float32)
    object_points[:, :2] = np.mgrid[0:INNER_CORNERS[0], 0:INNER_CORNERS[1]].T.reshape(-1, 2)*SQUARE_M
    image_size = None
    accepted, rejected, points, seen = [], [], [], set()
    for path in files:
        raw = path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        if digest in seen:
            rejected.append({"file": path.name, "reason": "duplicate image bytes"})
            continue
        seen.add(digest)
        gray = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_GRAYSCALE)
        if gray is None:
            rejected.append({"file": path.name, "reason": "PNG decode failed"})
            continue
        size = (gray.shape[1], gray.shape[0])
        if image_size is None:
            image_size = size
        if size != image_size:
            raise ValueError(f"Image size mismatch in {path.name}: {size} versus {image_size}; never resize to hide it")
        corners = find_corners(gray)
        if corners is None:
            rejected.append({"file": path.name, "reason": "complete 8 x 5 inner-corner pattern not found"})
            continue
        corners = np.asarray(corners, np.float32)
        # Repeated stationary captures are not independent calibration views.
        # Account for the 180-degree ordering ambiguity of a symmetric board.
        if any(min(float(np.sqrt(np.mean(np.sum((corners-old)**2, axis=2)))),
                   float(np.sqrt(np.mean(np.sum((corners[::-1]-old)**2, axis=2))))) < 2.0 for old in points):
            rejected.append({"file": path.name, "reason": "near-duplicate corner positions (<2 px RMS)"})
            continue
        points.append(corners)
        accepted.append(path.name)
    if len(points) < MIN_VIEWS:
        raise ValueError(f"Need at least {MIN_VIEWS} distinct detected views; accepted {len(points)} of {len(files)}. "
                         "Change board distance, tilt and image position; repeated frames do not count. "
                         f"Rejected: {json.dumps(rejected, ensure_ascii=False)}")
    rms, matrix, distortion, rotations, translations = cv2.calibrateCamera(
        [object_points.copy() for _ in points], points, image_size, None, None)
    if (not math.isfinite(float(rms)) or not np.isfinite(matrix).all()
            or not np.isfinite(distortion).all() or matrix[0, 0] <= 0 or matrix[1, 1] <= 0):
        raise ValueError("Calibration produced invalid parameters")
    views = []
    for name, observed, rvec, tvec in zip(accepted, points, rotations, translations):
        projected, _ = cv2.projectPoints(object_points, rvec, tvec, matrix, distortion)
        error = float(np.sqrt(np.mean(np.sum((observed-projected)**2, axis=2))))
        views.append({"file": name, "rms_px": error})
    centers = np.array([p.reshape(-1, 2).mean(axis=0) for p in points])
    cloud = np.concatenate(points).reshape(-1, 2)
    sizes = [float(np.linalg.norm(p.reshape(INNER_CORNERS[1], INNER_CORNERS[0], 2)[0, -1]
                                 - p.reshape(INNER_CORNERS[1], INNER_CORNERS[0], 2)[0, 0])) for p in points]
    width, height = image_size
    center_span = (np.ptp(centers, axis=0)/np.array([width, height])).tolist()
    corner_span = (np.ptp(cloud, axis=0)/np.array([width, height])).tolist()
    scale_ratio = max(sizes)/min(sizes)
    warnings = ["Twelve views and low residuals do not prove physical accuracy; validate held-out views and landmark scale.",
                "This result calibrates the lens at this resolution and native pixel orientation, not camera-to-body mounting."]
    if center_span[0] < .25 or center_span[1] < .25:
        warnings.append("Board centres cover a narrow image region; capture more views near different image edges.")
    if corner_span[0] < .6 or corner_span[1] < .6:
        warnings.append("Detected corners have limited image coverage; distortion near the edges may be poorly constrained.")
    if scale_ratio < 1.25:
        warnings.append("Board scale varies little; capture different distances as well as tilts.")
    if rms > 1.0 or max(v["rms_px"] for v in views) > 2.0:
        warnings.append("Reprojection residuals are large; inspect blur, board flatness, detections and image consistency.")
    return {
        "camera_matrix": matrix.tolist(), "distortion": distortion.reshape(-1).tolist(),
        "image_size": list(image_size),
        "calibration": {
            "method": "OpenCV pinhole calibrateCamera", "opencv_version": cv2.__version__,
            "inner_corners": list(INNER_CORNERS), "square_size_m": SQUARE_M,
            "image_orientation": "native raw camera; no resize, crop or rotation",
            "overall_rms_px": float(rms), "accepted_views": len(views), "views": views,
            "rejected": rejected, "centre_span_fraction": center_span,
            "corner_span_fraction": corner_span, "scale_ratio": scale_ratio,
            "needs_physical_validation": True, "warnings": warnings,
        },
    }


def frame_to_png(response, request_elapsed_s: float):
    """Validate a native executor frame without assuming workstation/board clock sync."""
    import cv2
    from microduck_harness.vision import decode_frame

    response.raise_for_status()
    headers = response.headers
    if headers.get("x-frame-format", "").upper() != "UYVY":
        raise ValueError("Calibration capture requires the executor's native UYVY frame")
    source_age = float(headers["x-frame-age-s"])
    captured_at = float(headers["x-frame-captured-at"])
    frame_id = headers["x-frame-id"]
    if (not frame_id or not math.isfinite(source_age) or source_age < 0
            or not math.isfinite(request_elapsed_s) or request_elapsed_s < 0
            or source_age+request_elapsed_s > 1.0 or not math.isfinite(captured_at) or captured_at <= 0):
        raise ValueError("Missing, malformed or stale camera frame metadata")
    frame = decode_frame(response.content, headers, {"rotate_upright": False, "use_robot_camera_fk": True})
    ok, png = cv2.imencode(".png", frame)
    if not ok:
        raise ValueError("PNG encoding failed")
    return png.tobytes(), {
        "frame_id": frame_id, "source_captured_at": captured_at,
        "source_age_s": source_age, "request_elapsed_s": request_elapsed_s,
        "image_size": [frame.shape[1], frame.shape[0]], "source_format": "UYVY",
        "source_display_rotation_degrees": int(headers.get("x-frame-rotate", 0)),
        "pixel_rotation_applied_degrees": 0,
    }


def capture(base_url: str, directory: Path):
    import httpx

    parsed = urlsplit(base_url)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path not in {"", "/"}):
        raise ValueError("Use an executor origin such as http://ZERO_IP:8766, without credentials, path or query")
    token = os.environ.get("EXECUTOR_TOKEN")
    if not token:
        raise ValueError("Set EXECUTOR_TOKEN in the environment; do not pass secrets on the command line")
    directory.mkdir(parents=True, exist_ok=True)
    seen_ids = set()
    for meta_path in directory.glob("capture-*.json"):
        try:
            seen_ids.add(json.loads(meta_path.read_text())["frame_id"])
        except (OSError, ValueError, KeyError):
            pass
    print("Read-only calibration capture. Keep the robot stationary; move the board between captures.")
    print("Use 9 x 6 squares / 8 x 5 inner corners. Enter saves one native frame; q exits.")
    with httpx.Client(headers={"Authorization": f"Bearer {token}"}, timeout=3,
                      follow_redirects=False, trust_env=False) as client:
        while True:
            try:
                command = input("Different board view ready? [Enter / q] ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print()
                return
            if command == "q":
                return
            if command:
                print("Press Enter once to capture, or q to finish.")
                continue
            try:
                started = time.monotonic()
                response = client.get(base_url.rstrip("/")+"/frame")
                png, meta = frame_to_png(response, time.monotonic()-started)
                if meta["frame_id"] in seen_ids:
                    raise ValueError("Repeated source frame_id; wait for a new frame")
                # Timestamp-based names preserve prior sessions; exclusive creation
                # protects user files even in the unlikely event of a collision.
                stem = f"capture-{time.time_ns()}"
                image_path = directory/f"{stem}.png"
                with image_path.open("xb") as output:
                    output.write(png)
                with (directory/f"{stem}.json").open("x") as output:
                    json.dump(meta, output, indent=2)
                seen_ids.add(meta["frame_id"])
                print(f"Saved {image_path} at native size {meta['image_size']}; change board tilt, distance or position.")
            except (httpx.HTTPError, OSError, ValueError, KeyError) as exc:
                print(f"Capture rejected: {type(exc).__name__}: {exc}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    grab = commands.add_parser("capture", help="Enter-driven, read-only native PNG capture")
    grab.add_argument("--executor-url", required=True)
    grab.add_argument("--directory", type=Path, default=Path("data/calibration-raw"))
    solve = commands.add_parser("calibrate", help="Estimate intrinsics from at least 12 distinct PNG views")
    solve.add_argument("--images", type=Path, required=True)
    solve.add_argument("--output", type=Path, default=Path("data/camera-calibration.json"))
    args = parser.parse_args()
    try:
        if args.command == "capture":
            capture(args.executor_url, args.directory)
            return 0
        if args.output.exists():
            raise ValueError("Output already exists; select a new file name to preserve the previous result")
        result = calibrate(args.images)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x") as output:
            json.dump(result, output, ensure_ascii=False, indent=2, allow_nan=False)
            output.write("\n")
        print(json.dumps({"output": str(args.output), "image_size": result["image_size"],
                          "accepted_views": result["calibration"]["accepted_views"],
                          "overall_rms_px": result["calibration"]["overall_rms_px"],
                          "warnings": result["calibration"]["warnings"]}, ensure_ascii=False, indent=2))
        print("Review the result, then explicitly copy camera_matrix, distortion and image_size into your vision config.")
        return 0
    except (OSError, ValueError, KeyError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    sys.exit(main())
