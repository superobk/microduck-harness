"""Prepare software calibration and camera configuration on exact native sources.

This module never builds, installs, restarts, changes torque or opens the bus.
Original microduck-tools files remain unchanged. The camera extension is ported
to the second pinned source tag here, with no additional image transformation.
"""
from __future__ import annotations

import difflib
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parent
UPSTREAM = ROOT / "upstream"
TOOLS_COMMIT = "2b93cb3afc32fa343cc612ee060e94c5bdd7b01d"
TARGETS = {
    "daemon-v0.16.3": "0b2b82f56ac1e32d69018837ac1cd7d4c687f5a0",
    "daemon-v0.17.8": "942e58e3aa6dbb28e7037203fff11866122c7567",
}
NEW_MODULE = "mediad/src/camera_rotation.rs"
PATHS = (
    "robotd-params/src/lib.rs", "robotd-params/src/registry.rs",
    "robotd-params/src/edit.rs", "duck-control/src/bus.rs",
    "duck-control/src/imu.rs", "robotd/src/main.rs", "mediad/src/lib.rs",
    "mediad/src/main.rs", "mediad/src/web.rs", NEW_MODULE,
)


class PreparationError(ValueError):
    pass


def verify_upstream() -> dict:
    """Detect accidental drift before executing the vendored Python entrypoints."""
    record = json.loads((ROOT / "UPSTREAM.json").read_text(encoding="utf-8"))
    if record.get("commit") != TOOLS_COMMIT:
        raise PreparationError("unexpected microduck-tools source revision")
    for relative, expected in record["files"].items():
        path = UPSTREAM / relative
        if path.is_symlink() or not path.is_file():
            raise PreparationError(f"missing regular upstream file: {relative}")
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected["sha256"]:
            raise PreparationError(f"pinned upstream file has changed: {relative}")
    return record


def load_source_patcher():
    verify_upstream()
    path = UPSTREAM / "calibration/common/apply_source_patch.py"
    spec = importlib.util.spec_from_file_location("microduck_tools_source_patcher", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def once(text: str, before: str, after: str, path: str) -> str:
    if text.count(before) != 1:
        raise PreparationError(f"{path}: expected exactly one reviewed source anchor")
    return text.replace(before, after, 1)


def camera_params(text: str) -> str:
    path = "robotd-params/src/lib.rs"
    text = once(text, "    pub sensor: MediaSensor,\n",
                "    pub sensor: MediaSensor,\n"
                "    /// Clockwise display correction; None follows the board mount.\n"
                "    pub camera_rotate: Option<u32>,\n", path)
    text = once(text, "            sensor: MediaSensor::Board,\n",
                "            sensor: MediaSensor::Board,\n            camera_rotate: None,\n", path)
    text = once(text, "    Rate { path: String, got: u32 },\n",
                "    Rate { path: String, got: u32 },\n"
                '    #[error("{path}: media.camera_rotate must be 0, 90, 180 or 270, got {got}")]\n'
                "    CameraRotation { path: String, got: u32 },\n", path)
    return once(text,
                "    fn validate(&self, path: &Path) -> Result<(), ParamsError> {\n",
                "    fn validate(&self, path: &Path) -> Result<(), ParamsError> {\n"
                "        if let Some(got) = self.media.camera_rotate {\n"
                "            if ![0, 90, 180, 270].contains(&got) {\n"
                "                return Err(ParamsError::CameraRotation {\n"
                "                    path: path.display().to_string(), got,\n"
                "                });\n"
                "            }\n"
                "        }\n", path)


def camera_main(text: str) -> str:
    path = "mediad/src/main.rs"
    text = once(text,
                "    let rotate = args\n        .rotate\n"
                "        .unwrap_or_else(|| params.board.version.camera_mount_degrees());",
                "    let camera_rotation = match mediad::camera_rotation::resolve(\n"
                "        params.board.version.camera_mount_degrees(),\n"
                "        params.media.camera_rotate, args.rotate,\n"
                "    ) {\n"
                "        Ok(rotation) => rotation,\n"
                "        Err(error) => {\n"
                '            tracing::error!(%error, "mediad cannot start");\n'
                "            return ExitCode::FAILURE;\n"
                "        }\n"
                "    };\n"
                "    let rotate = camera_rotation.degrees;", path)
    anchor = "        let frame_rotate = if args.flip_in_pipeline { 0 } else { rotate };\n"
    text = once(text, anchor, anchor +
                "        if let Err(error) = mediad::camera_rotation::publish(\n"
                '            std::path::Path::new("/run/mediad/camera-effective.json"),\n'
                "            &config, camera_rotation, args.flip_in_pipeline, width, height,\n"
                "        ) {\n"
                '            tracing::warn!(%error, "cannot publish runtime camera rotation");\n'
                "        }\n", path)
    return once(text,
                "    /// **The board's mount by default** (`Board::camera_mount_degrees`): 90 on the Zero 3W, whose\n",
                "    /// Overrides [media].camera_rotate; otherwise the board mount is the default:\n"
                "    /// 90 on the Zero 3W, whose\n", path)


def camera_web_tests(text: str) -> str:
    path = "mediad/src/web.rs"
    text = once(text,
                "        for (rotate, size, bright) in [(0, (2, 1), (1, 0)), (90, (1, 2), (0, 1))] {",
                "        for (rotate, size, bright) in [\n"
                "            (0, (2, 1), (1, 0)), (90, (1, 2), (0, 1)),\n"
                "            (180, (2, 1), (0, 0)), (270, (1, 2), (0, 0)),\n"
                "        ] {", path)
    return once(text,
                "        // The dark pixel is the frame's first either way round: a clockwise turn takes source\n"
                "        // (0, 0) to upright (0, 0), so only the bright one has to move for the turn to be real.\n"
                "        assert!(decoded.get_pixel(0, 0)[0] < 5);",
                "        // Both pixels must retain their intensities through every quarter turn.\n"
                "        let dark = if size.0 == 2 {\n"
                "            (1 - bright.0, 0)\n"
                "        } else {\n"
                "            (0, 1 - bright.1)\n"
                "        };\n"
                "        assert!(decoded.get_pixel(dark.0, dark.1)[0] < 5);", path)


def transformed(original: dict[str, str], target: str) -> dict[str, str]:
    if target not in TARGETS:
        raise PreparationError("only the two pinned native tags are supported")
    patcher = load_source_patcher()
    edits = dict(original)
    edits[patcher.PARAMS] = camera_params(patcher.patch_params(original[patcher.PARAMS], target))
    registry = patcher.patch_registry(original[patcher.REGISTRY])
    marker = "    // ── [pad] ────────────────────────────────────────────────────────────────"
    edits[patcher.REGISTRY] = once(registry, marker,
        '    entry("media.camera_rotate", Kind::OptionalInteger,\n'
        '        "Clockwise camera correction: 0, 90, 180, 270; unset follows board, restart mediad only"),\n' + marker,
        patcher.REGISTRY)
    edits[patcher.BUS] = patcher.patch_bus(original[patcher.BUS])
    edits[patcher.IMU] = patcher.patch_imu(original[patcher.IMU])
    edits[patcher.MAIN] = patcher.patch_main(original[patcher.MAIN], target)
    edits["robotd-params/src/edit.rs"] = once(original["robotd-params/src/edit.rs"],
        '"pad.x" | "pad.y" | "pad.lb" | "pad.rb"',
        '"pad.x" | "pad.y" | "pad.lb" | "pad.rb" | "bus.calibration_path"',
        "robotd-params/src/edit.rs")
    edits["mediad/src/lib.rs"] = once(original["mediad/src/lib.rs"], "pub mod camera;\n",
        "pub mod camera;\n/// Persistent camera correction and runtime provenance.\npub mod camera_rotation;\n",
        "mediad/src/lib.rs")
    edits["mediad/src/main.rs"] = camera_main(original["mediad/src/main.rs"])
    edits["mediad/src/web.rs"] = camera_web_tests(original["mediad/src/web.rs"])
    template = (ROOT / "templates/camera_rotation.rs.in").read_text(encoding="utf-8")
    if template.count('"__TARGET__"') != 1:
        raise PreparationError("camera module target template has changed")
    edits[NEW_MODULE] = template.replace('"__TARGET__"', json.dumps(target))
    return edits


def git(repo: Path, *args: str, input: str | None = None) -> str:
    result = subprocess.run(["git", *args], cwd=repo, input=input,
                            text=True, capture_output=True, check=False)
    if result.returncode:
        raise PreparationError("git " + args[0] + " failed: " + result.stderr.strip())
    return result.stdout


def make_patch(original: dict[str, str], edits: dict[str, str]) -> str:
    parts = []
    for relative in PATHS:
        old, new = original.get(relative, ""), edits[relative]
        if old == new:
            raise PreparationError(f"expected source change missing: {relative}")
        parts.append(f"diff --git a/{relative} b/{relative}\n")
        if relative == NEW_MODULE:
            parts.append("new file mode 100644\n")
        parts.extend(difflib.unified_diff(old.splitlines(keepends=True), new.splitlines(keepends=True),
            fromfile="/dev/null" if relative == NEW_MODULE else "a/"+relative,
            tofile="b/"+relative))
    return "".join(parts)


def prepare(repo: Path, target: str, *, apply: bool = False) -> tuple[dict, str]:
    if target not in TARGETS:
        raise PreparationError("unsupported target")
    repo = repo.resolve(strict=True)
    if not (repo / ".git").exists():
        raise PreparationError("pass an existing native Git checkout")
    head = git(repo, "rev-parse", "HEAD").strip()
    tag = git(repo, "rev-parse", "--verify", target+"^{commit}").strip()
    if head != TARGETS[target] or tag != TARGETS[target]:
        raise PreparationError("HEAD and selected tag must match the pinned official commit exactly")
    dirty = git(repo, "status", "--porcelain", "--untracked-files=all", "--", *PATHS)
    if dirty:
        raise PreparationError("patch target files already contain changes; preserve them in another checkout")
    original = {}
    for relative in PATHS:
        path = repo / relative
        if path.is_symlink() or path.resolve().is_relative_to(repo) is False:
            raise PreparationError("source symlinks are not accepted")
        if relative == NEW_MODULE:
            if path.exists():
                raise PreparationError("camera module already exists; refusing to overwrite it")
            continue
        original[relative] = path.read_text(encoding="utf-8")
        if original[relative] != git(repo, "show", "HEAD:"+relative):
            raise PreparationError("source differs from the official Git object: "+relative)
    patch = make_patch(original, transformed(original, target))
    # `git apply --check` checks all files before any mutation. No shell, hooks,
    # network, build, daemon restart or package installation is involved here.
    git(repo, "apply", "--check", "--whitespace=error-all", "-", input=patch)
    if apply:
        git(repo, "apply", "--whitespace=error-all", "-", input=patch)
    plan = {"target_tag": target, "native_commit": head, "tools_commit": TOOLS_COMMIT,
            "repository": str(repo), "files": list(PATHS), "applied": apply,
            "patch_sha256": hashlib.sha256(patch.encode()).hexdigest(),
            "rust_compiled": False, "hardware_tested": False,
            "next_step": "Review git diff, run cargo fmt and native tests/build on the target build host; nothing was installed or restarted."}
    return plan, patch
