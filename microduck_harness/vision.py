"""Calibrated perception feed and optional camera/ArUco workstation service.

No image, localization or distance is synthesized here. The demo world lives in
the harness simulator. OpenCV and YOLO are optional, lazily imported dependencies.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager, suppress
import hmac
import json
import math
import os
from pathlib import Path
import time
from typing import Any

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator


class Pose(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    x: float
    y: float
    yaw: float


class Detection(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    id: str = Field(min_length=1, max_length=100)
    label: str = Field(min_length=1, max_length=100)
    confidence: float = Field(ge=0, le=1)
    bearing_rad: float = Field(ge=-math.pi, le=math.pi)
    range_m: float | None = Field(default=None, gt=0, le=100)


class Observation(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    frame_id: str = Field(min_length=1, max_length=160)
    captured_at: float = Field(gt=0)
    source: str = Field(min_length=1, max_length=160)
    pose: Pose | None = None
    pose_valid: bool = False
    clearance_m: float | None = Field(default=None, ge=0, le=100)
    detections: list[Detection] = Field(default_factory=list, max_length=100)
    image_url: str | None = None
    notes: list[str] = Field(default_factory=list, max_length=30)

    @model_validator(mode="after")
    def valid_pose_has_coordinates(self):
        if self.pose_valid and self.pose is None:
            raise ValueError("pose_valid requires measured pose")
        return self


def checked_transform(value: Any) -> list[list[float]]:
    """Validate an SE(3) matrix; no numpy required on the ingestion path."""
    if not isinstance(value, list) or len(value) != 4 or any(len(r) != 4 for r in value):
        raise ValueError("transform must be a 4 by 4 matrix")
    m = [[float(v) for v in row] for row in value]
    if not all(math.isfinite(v) for row in m for v in row):
        raise ValueError("nonfinite transform")
    if any(abs(m[3][i] - (1.0 if i == 3 else 0.0)) > 1e-6 for i in range(4)):
        raise ValueError("invalid homogeneous row")
    for i in range(3):
        for j in range(3):
            dot = sum(m[k][i] * m[k][j] for k in range(3))
            if abs(dot - (1.0 if i == j else 0.0)) > 1e-3:
                raise ValueError("rotation is not orthonormal")
    det = (m[0][0] * (m[1][1] * m[2][2] - m[1][2] * m[2][1])
           - m[0][1] * (m[1][0] * m[2][2] - m[1][2] * m[2][0])
           + m[0][2] * (m[1][0] * m[2][1] - m[1][1] * m[2][0]))
    if abs(det - 1) > 1e-3:
        raise ValueError("rotation must be right handed")
    return m


def matmul(a: list[list[float]], b: list[list[float]]) -> list[list[float]]:
    return [[sum(a[i][k] * b[k][j] for k in range(4)) for j in range(4)] for i in range(4)]


def inverse_transform(m: list[list[float]]) -> list[list[float]]:
    r = [[m[j][i] for j in range(3)] for i in range(3)]
    return [r[i] + [-sum(r[i][j] * m[j][3] for j in range(3))] for i in range(3)] + [[0, 0, 0, 1]]


def transform_point(m: list[list[float]], p: list[float]) -> list[float]:
    return [sum(m[i][j] * p[j] for j in range(3)) + m[i][3] for i in range(3)]


def pose_transform(pos: list[float], quat: list[float]) -> list[list[float]]:
    if len(pos) != 3 or len(quat) != 4:
        raise ValueError("pose requires xyz and scalar-first quaternion")
    if not all(math.isfinite(float(x)) for x in pos + quat):
        raise ValueError("nonfinite pose")
    norm = math.sqrt(sum(float(x) ** 2 for x in quat))
    if not 0.95 <= norm <= 1.05:
        raise ValueError("invalid quaternion norm")
    w, x, y, z = [float(v) / norm for v in quat]
    return [
        [1-2*(y*y+z*z), 2*(x*y-z*w), 2*(x*z+y*w), float(pos[0])],
        [2*(x*y+z*w), 1-2*(x*x+z*z), 2*(y*z-x*w), float(pos[1])],
        [2*(x*z-y*w), 2*(y*z+x*w), 1-2*(x*x+y*y), float(pos[2])],
        [0, 0, 0, 1],
    ]


def marker_robot_pose(world_marker: list[list[float]], camera_marker: list[list[float]],
                      body_camera: list[list[float]]) -> Pose:
    """T_world_body = T_world_marker inv(T_camera_marker) inv(T_body_camera)."""
    m = matmul(matmul(checked_transform(world_marker), inverse_transform(checked_transform(camera_marker))),
               inverse_transform(checked_transform(body_camera)))
    return Pose(x=m[0][3], y=m[1][3], yaw=math.atan2(m[1][0], m[0][0]))


def body_camera_transform(state: dict, config: dict, captured_at: float) -> list[list[float]] | None:
    """Current robot FK first; calibrated fixed-head fallback only with measured joints."""
    if abs(float(state.get("observed_at", 0)) - captured_at) > float(config.get("max_pair_skew_s", 0.25)):
        return None
    raw = state.get("raw", state)
    if config.get("use_robot_camera_fk", True):
        camera = state.get("frames", raw.get("frames", {})) or {}
        camera = camera.get("camera")
        if camera:
            try:
                return pose_transform(camera["pos"], camera["quat"])
            except (ValueError, TypeError, KeyError):
                return None
    expected = config.get("fixed_head_rad")
    measured = state.get("measured_head") or raw.get("joints", [])[5:9]
    if not isinstance(expected, list) or len(expected) != 4 or len(measured) != 4:
        return None
    if any(not math.isfinite(float(v)) for v in measured):
        return None
    if any(abs(float(a)-float(b)) > float(config.get("head_tolerance_rad", 0.02))
           for a, b in zip(expected, measured)):
        return None
    try:
        return checked_transform(config["T_body_camera"])
    except (ValueError, KeyError, TypeError):
        return None


def conservative_clearance(tof: dict, state: dict, max_age_s: float = 0.5) -> float | None:
    """Forward range gate, not a complete collision map or cliff detector.

    All central 4x4 zones must carry positive status5/9 ranges. Sensor must point
    forwards in the trunk and be paired with the measured FK. Floor returns are
    retained conservatively. NoTarget255 and unusable samples do not mean free.
    """
    try:
        age = float(tof["age_s"])
        frame = tof["frame"]
        raw = state.get("raw", state)
        if not 0 <= age <= max_age_s:
            return None
        if frame["rows"] != 8 or frame["cols"] != 8:
            return None
        if len(frame["distance_mm"]) != 64 or len(frame["status"]) != 64:
            return None
        # Both clocks are CLOCK_MONOTONIC on the same ZERO3W.
        if not frame.get("t_ns") or not raw.get("t_ns"):
            return None
        if abs(int(frame["t_ns"]) - int(raw["t_ns"])) > 250_000_000:
            return None
        frames = state.get("frames", raw.get("frames", {})) or {}
        sensor = pose_transform(frames["tof"]["pos"], frames["tof"]["quat"])
        if sensor[0][0] < math.cos(math.radians(12)):
            return None
        central = [r * 8 + c for r in range(2, 6) for c in range(2, 6)]
        if any(frame["status"][i] not in (5, 9) or frame["distance_mm"][i] <= 0 for i in central):
            return None
        distances = []
        for i in central:
            # Native microduck kinematics uses 45-degree FOV and inset zone centres.
            elevation = math.radians(19.6875 - (i // 8) * 5.625)
            azimuth = math.radians(19.6875 - (i % 8) * 5.625)
            ray = [math.cos(elevation)*math.cos(azimuth), math.cos(elevation)*math.sin(azimuth), math.sin(elevation)]
            p = transform_point(sensor, [float(frame["distance_mm"][i]) / 1000 * v for v in ray])
            if p[0] <= 0:
                return None
            distances.append(p[0])
        return min(distances)
    except (KeyError, ValueError, TypeError, IndexError, OverflowError):
        return None


class ObservationStore:
    def __init__(self):
        self.latest: Observation | None = None
        self.last_error: str | None = None
        self.image: bytes | None = None

    def put(self, observation: Observation, now: float | None = None):
        now = time.time() if now is None else now
        if observation.captured_at > now + 0.25:
            raise ValueError("capture timestamp is in the future; synchronize clocks")
        if now - observation.captured_at > 30:
            raise ValueError("observation is older than 30 seconds")
        if self.latest and observation.captured_at <= self.latest.captured_at:
            raise ValueError("out-of-order or repeated capture timestamp")
        if self.latest and observation.source == self.latest.source and observation.frame_id == self.latest.frame_id:
            raise ValueError("repeated source frame_id")
        self.latest = observation
        self.last_error = None


def decode_frame(body: bytes, headers: httpx.Headers, config: dict):
    import cv2
    import numpy as np

    if len(body) > 16 * 1024 * 1024:
        raise ValueError("frame exceeds 16 MiB")
    if headers.get("x-frame-format", "").upper() == "UYVY":
        width, height = int(headers["x-frame-width"]), int(headers["x-frame-height"])
        if width <= 0 or height <= 0 or width % 2 or width * height * 2 != len(body):
            raise ValueError("malformed UYVY geometry")
        frame = cv2.cvtColor(np.frombuffer(body, np.uint8).reshape(height, width, 2), cv2.COLOR_YUV2BGR_UYVY)
        rotate = int(headers.get("x-frame-rotate", 0))
        if rotate not in (0, 90, 180, 270):
            raise ValueError("unsupported camera rotation")
        # Native optical coordinates match robot FK. Rotate only when the operator
        # explicitly supplied an extrinsic transform and calibration for that image.
        if config.get("rotate_upright", False) and rotate:
            if config.get("use_robot_camera_fk", True):
                raise ValueError("rotated pixels require explicitly calibrated camera extrinsics")
            code = {90: cv2.ROTATE_90_CLOCKWISE, 180: cv2.ROTATE_180, 270: cv2.ROTATE_90_COUNTERCLOCKWISE}[rotate]
            frame = cv2.rotate(frame, code)
    else:
        frame = cv2.imdecode(np.frombuffer(body, np.uint8), cv2.IMREAD_COLOR)
    if frame is None:
        raise ValueError("image decode failed")
    if config.get("image_size") and [frame.shape[1], frame.shape[0]] != config["image_size"]:
        raise ValueError("frame size does not match calibrated image_size")
    return frame


class CameraPipeline:
    def __init__(self, config: dict, store: ObservationStore):
        self.config, self.store = config, store
        self.yolo = None

    def detect(self, frame, frame_id: str, captured_at: float, state: dict, tof: dict,
               *, source_captured_at: float | None = None) -> Observation:
        import cv2
        import numpy as np

        cfg = self.config
        matrix = np.asarray(cfg["camera_matrix"], dtype=np.float64)
        distortion = np.asarray(cfg.get("distortion", [0, 0, 0, 0, 0]), dtype=np.float64)
        if matrix.shape != (3, 3) or not np.isfinite(matrix).all() or matrix[0, 0] <= 0 or matrix[1, 1] <= 0:
            raise ValueError("invalid calibrated camera_matrix")
        dictionary_name = cfg.get("dictionary", "DICT_4X4_50")
        if not dictionary_name.startswith("DICT_") or not hasattr(cv2.aruco, dictionary_name):
            raise ValueError("unknown ArUco dictionary")
        dictionary = cv2.aruco.getPredefinedDictionary(getattr(cv2.aruco, dictionary_name))
        detector_parameters = cv2.aruco.DetectorParameters()
        # Integer-pixel corners noticeably amplify planar pose error for small
        # distant markers. Refine the observed corners before metric PnP.
        detector_parameters.cornerRefinementMethod = cv2.aruco.CORNER_REFINE_SUBPIX
        detector = cv2.aruco.ArucoDetector(dictionary, detector_parameters)
        corners, ids, _ = detector.detectMarkers(frame)
        # FK and mediad capture timestamps originate on the same board. Keep
        # that clock for sensor pairing even when the outward observation time
        # was conservatively rebased to the workstation clock for its TTL.
        pair_time = captured_at if source_captured_at is None else source_captured_at
        body_camera = body_camera_transform(state, cfg, pair_time)
        detections, poses, notes = [], [], []
        if body_camera is None:
            notes.append("camera-to-body transform unavailable or not paired with frame")
        markers = cfg.get("markers", {})
        for corner, identifier in zip(corners, ids.flatten() if ids is not None else []):
            key = str(int(identifier))
            marker = markers.get(key, {})
            label = marker.get("label", f"marker:{key}")
            center = corner.reshape(4, 2).mean(axis=0)
            # Camera image right is trunk left-negative when optical axis is forward.
            undistorted = cv2.undistortPoints(np.array([[center]], dtype=np.float64), matrix, distortion)
            ray = [float(undistorted[0, 0, 0]), float(undistorted[0, 0, 1]), 1.0]
            bearing = -math.atan2(ray[0], 1.0)
            if body_camera is not None:
                direction = [sum(body_camera[i][j]*ray[j] for j in range(3)) for i in range(3)]
                bearing = math.atan2(direction[1], direction[0])
            marker_range = None
            size = marker.get("size_m")
            if size is not None and float(size) > 0:
                half = float(size) / 2
                obj = np.array([[-half, half, 0], [half, half, 0], [half, -half, 0], [-half, -half, 0]], dtype=np.float64)
                ok, rvec, tvec = cv2.solvePnP(obj, corner.reshape(4, 2), matrix, distortion, flags=cv2.SOLVEPNP_ITERATIVE)
                if ok and float(tvec[2, 0]) > 0 and np.isfinite(tvec).all():
                    projected, _ = cv2.projectPoints(obj, rvec, tvec, matrix, distortion)
                    error = float(np.sqrt(np.mean(np.sum((projected.reshape(4, 2)-corner.reshape(4, 2))**2, axis=1))))
                    if error <= float(cfg.get("max_reprojection_error_px", 2.0)) and body_camera is not None:
                        point = transform_point(body_camera, tvec.flatten().tolist())
                        bearing = math.atan2(point[1], point[0])
                        marker_range = math.hypot(point[0], point[1])
                        if marker.get("T_world_marker"):
                            rotation, _ = cv2.Rodrigues(rvec)
                            camera_marker = [rotation[i].tolist() + [float(tvec[i, 0])] for i in range(3)] + [[0, 0, 0, 1]]
                            poses.append((error, marker_robot_pose(marker["T_world_marker"], camera_marker, body_camera)))
            detections.append(Detection(id=f"marker:{key}", label=label, confidence=1.0, bearing_rad=bearing, range_m=marker_range))
        pose = None
        if poses:
            poses.sort(key=lambda pair: pair[0])
            pose = poses[0][1]
            if any(math.hypot(p.x-pose.x, p.y-pose.y) > float(cfg.get("max_marker_disagreement_m", 0.15))
                   or abs(math.atan2(math.sin(p.yaw-pose.yaw), math.cos(p.yaw-pose.yaw))) > 0.2 for _, p in poses):
                pose = None
                notes.append("visible landmark localization disagrees")
        if cfg.get("yolo_weights"):
            if self.yolo is None:
                path = Path(cfg["yolo_weights"])
                if not path.is_file():
                    raise ValueError("yolo_weights must reference an existing local file")
                from ultralytics import YOLO
                self.yolo = YOLO(str(path))
            results = self.yolo.predict(frame, device=cfg.get("yolo_device", "cpu"), verbose=False)
            for result in results:
                for index, box in enumerate(result.boxes):
                    coords = box.xyxy[0].tolist()
                    center = [[[(coords[0]+coords[2])/2, (coords[1]+coords[3])/2]]]
                    ray = cv2.undistortPoints(np.asarray(center, dtype=np.float64), matrix, distortion)[0, 0]
                    bearing = -math.atan2(float(ray[0]), 1.0)
                    if body_camera is not None:
                        camera_ray = [float(ray[0]), float(ray[1]), 1.0]
                        direction = [sum(body_camera[i][j]*camera_ray[j] for j in range(3)) for i in range(3)]
                        bearing = math.atan2(direction[1], direction[0])
                    # No object range is inferred from bbox size or a generic ToF minimum.
                    detections.append(Detection(id=f"{frame_id}:box:{index}", label=result.names[int(box.cls[0])],
                        confidence=float(box.conf[0]), bearing_rad=bearing, range_m=None))
            notes.append("YOLO detections have no geometric range; approach remains unavailable")
        clearance = conservative_clearance(tof, state)
        if clearance is None:
            notes.append("forward ToF clearance unknown")
        return Observation(frame_id=frame_id, captured_at=captured_at, source="camera-aruco",
                           pose=pose, pose_valid=pose is not None, clearance_m=clearance,
                           detections=detections[:100], notes=notes)

    async def run(self):
        cfg = self.config
        token = os.environ.get(cfg.get("executor_token_env", "EXECUTOR_TOKEN"), "")
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        async with httpx.AsyncClient(timeout=float(cfg.get("request_timeout_s", 2.0)), headers=headers, follow_redirects=False) as client:
            while True:
                started = time.time()
                try:
                    frame_response, state_response, tof_response = await asyncio.gather(
                        client.get(cfg["camera_url"]), client.get(cfg["state_url"]), client.get(cfg["tof_url"]))
                    frame_response.raise_for_status()
                    state_response.raise_for_status()
                    timestamp = float(frame_response.headers["x-frame-captured-at"])
                    source_age = float(frame_response.headers["x-frame-age-s"])
                    if not math.isfinite(timestamp) or not math.isfinite(source_age) or source_age < 0:
                        raise ValueError("invalid camera capture timestamp")
                    # Conservative local time: never freshen oldframes when host clocks disagree.
                    captured_at = min(timestamp, started-source_age)
                    if time.time()-captured_at > float(cfg.get("max_frame_age_s", 1.0)):
                        raise ValueError("camera frame is stale")
                    state = state_response.json()
                    tof = tof_response.json() if tof_response.status_code == 200 else {}
                    if tof:
                        tof["age_s"] = float(tof["age_s"]) + (time.time()-started)
                    frame = await asyncio.to_thread(decode_frame, frame_response.content, frame_response.headers, cfg)
                    observation = await asyncio.to_thread(self.detect, frame,
                        frame_response.headers.get("x-frame-id", str(timestamp)), captured_at, state, tof,
                        source_captured_at=timestamp)
                    import cv2
                    ok, encoded = await asyncio.to_thread(cv2.imencode, ".jpg", frame)
                    self.store.put(observation)
                    self.store.image = encoded.tobytes() if ok else None
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    # A lost camera never refreshes the previous observation's timestamp.
                    self.store.last_error = f"{type(exc).__name__}: {str(exc)[:180]}"
                await asyncio.sleep(float(cfg.get("poll_interval_s", 0.1)))


def create_vision_app(settings: dict | None = None) -> FastAPI:
    settings = dict(settings or {})
    token = settings.get("token", os.environ.get("VISION_TOKEN", ""))
    config_file = settings.get("config_file", os.environ.get("VISION_CONFIG", ""))
    config = json.loads(Path(config_file).read_text()) if config_file else settings.get("camera_config", {})
    store = ObservationStore()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        task = None
        if config.get("mode") == "camera":
            task = asyncio.create_task(CameraPipeline(config, store).run())
        try:
            yield
        finally:
            if task:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

    app = FastAPI(title="Microduck vision", lifespan=lifespan)
    app.state.observations = store

    async def auth(authorization: str | None = Header(default=None)):
        if not token or not hmac.compare_digest(authorization or "", f"Bearer {token}"):
            raise HTTPException(401, "valid VISION_TOKEN is required", headers={"WWW-Authenticate": "Bearer"})

    @app.get("/health")
    async def health():
        return {"service": "microduck-vision", "mode": config.get("mode", "external"),
                "has_frame": store.latest is not None,
                "frame_age_s": time.time()-store.latest.captured_at if store.latest else None,
                "last_error": store.last_error}

    @app.get("/observe", dependencies=[Depends(auth)])
    async def observe():
        if store.latest is None:
            raise HTTPException(503, "no observation received")
        return store.latest.model_dump()

    @app.get("/image", dependencies=[Depends(auth)])
    async def image():
        from fastapi.responses import Response
        if store.image is None or store.latest is None:
            raise HTTPException(404, "no camera image available")
        return Response(store.image, media_type="image/jpeg", headers={
            "X-Frame-Id": store.latest.frame_id,
            "X-Frame-Captured-At": str(store.latest.captured_at),
            "Cache-Control": "no-store",
        })

    @app.post("/observations", dependencies=[Depends(auth)])
    async def ingest(observation: Observation):
        if config.get("mode") == "camera":
            raise HTTPException(409, "camera mode owns the observation stream")
        try:
            store.put(observation)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"accepted": True, "frame_id": observation.frame_id, "captured_at": observation.captured_at}

    return app


def main():
    parser = argparse.ArgumentParser(description="Microduck calibrated perception service")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8767)
    parser.add_argument("--config", default=os.environ.get("VISION_CONFIG", ""))
    args = parser.parse_args()
    if not os.environ.get("VISION_TOKEN"):
        parser.error("set VISION_TOKEN before starting the service")
    import uvicorn
    uvicorn.run(create_vision_app({"config_file": args.config}), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
