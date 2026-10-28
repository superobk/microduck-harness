"""Actual pinned editors and local HTTP behavior; no robot or system service calls."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import http.client
import json
from pathlib import Path
import subprocess
import threading
import tomllib

import pytest

from integrations.microduck_tools import bridge, native


TAGS = tuple(native.TARGETS)


def context_for(tmp_path, target, *, demo=True, enable_apply=False):
    web = bridge.load_web(target)
    config = tmp_path / "robotd.toml"
    config.write_text('[board]\nversion="zero3"\n[media]\nquality="720p30"\n'
                      '[media.intrinsics]\nfx=500.0\n[policy]\nenabled=false\n')
    args = argparse.Namespace(target=target, config=str(config), dest=str(tmp_path / "calibration.toml"),
        runtime=str(tmp_path / "calibration-effective.json"), demo=demo, allow_inactive=False,
        no_telemetry=True, robotctl="robotctl", camera_runtime=str(tmp_path / "camera-effective.json"),
        camera_web_port=8080, enable_camera_apply=enable_apply)
    return web, web.Context(args)


def live_receipt(context, target, **changes):
    data = {"source": "mediad:camera:live", "target_tag": target,
            "pid": 12345, "pipeline_started": True, "config_path": str(context.config),
            "rotation_degrees": 90, "rotation_source": "board_default",
            "frame_rotation_degrees": 90, "flip_in_pipeline": False,
            "width": 1280, "height": 720}
    data.update(changes)
    context.camera.runtime.write_text(json.dumps(data))
    return data


def mock_native_process(monkeypatch, web):
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        if command == ["systemctl", "show", "mediad", "-p", "MainPID", "--value"]:
            return subprocess.CompletedProcess(command, 0, "12345\n", "")
        if command == ["systemctl", "restart", "mediad"]:
            return subprocess.CompletedProcess(command, 0, "", "")
        raise AssertionError("unexpected service operation")

    monkeypatch.setattr(web.camera_config.subprocess, "run", run)
    monkeypatch.setattr(web.camera_config.os, "readlink", lambda p: "/opt/robot/daemon/current/bin/mediad")
    return calls


@contextmanager
def local_http(web, context):
    server = web.ThreadingHTTPServer(("127.0.0.1", 0), web.new_handler(context))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        context.monitor.close()


def request(port, method, path, payload=None, *, csrf=None, origin=None):
    headers = {"Content-Type": "application/json"}
    if csrf is not None:
        headers["X-Calibration-CSRF"] = csrf
    if origin:
        headers["Origin"] = origin
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request(method, path, json.dumps(payload) if payload is not None else None, headers)
    response = conn.getresponse()
    data = response.read()
    conn.close()
    return response.status, data


def test_entire_vendor_snapshot_has_original_hashes():
    record = native.verify_upstream()
    assert record["commit"] == "2b93cb3afc32fa343cc612ee060e94c5bdd7b01d"
    assert len(record["files"]) == 65
    assert "calibration/common/duck_calibrate.py" in record["files"]


@pytest.mark.parametrize("target", TAGS)
def test_actual_camera_editor_preserves_config_and_never_rescales_pixels(tmp_path, target):
    web, context = context_for(tmp_path, target)
    original = context.config.read_text()
    for value in (None, 0, 90, 180, 270):
        result = context.camera.preview({"rotation": value})
        modified = tomllib.loads(result["config"])
        expected = tomllib.loads(original)
        if value is not None:
            expected["media"]["camera_rotate"] = value
        assert modified == expected
        assert context.config.read_text() == original
    assert not context.camera.status()["runtime_verified"]
    assert not context.camera.status()["can_save"]
    assert not context.camera.status()["can_apply"]
    context.monitor.close()


@pytest.mark.parametrize("target", TAGS)
def test_real_verifier_requires_actual_runtime_target_and_pid(tmp_path, target, monkeypatch):
    web, context = context_for(tmp_path, target, demo=False)
    mock_native_process(monkeypatch, web)
    live_receipt(context, target)
    state = context.camera.status()
    assert state["runtime_verified"] and state["can_save"]
    assert not state["can_apply"]
    other = TAGS[1] if target == TAGS[0] else TAGS[0]
    live_receipt(context, other)
    assert not context.camera.status()["can_save"]
    live_receipt(context, target, pid=54321)
    assert not context.camera.status()["can_save"]
    live_receipt(context, target, flip_in_pipeline=True)
    assert not context.camera.status()["can_save"]
    live_receipt(context, target, rotation_source="command_line")
    assert not context.camera.status()["can_save"]
    context.monitor.close()


@pytest.mark.parametrize("target", TAGS)
def test_saved_rotation_remains_pending_and_only_explicit_camera_apply_restarts(tmp_path, target, monkeypatch):
    web, context = context_for(tmp_path, target, demo=False)
    calls = mock_native_process(monkeypatch, web)
    live_receipt(context, target)
    original = context.config.read_bytes()
    with pytest.raises(web.cal.CalibrationError):
        context.camera.save({"rotation": 270})
    assert context.config.read_bytes() == original
    result = context.camera.save({"rotation": 270, "confirm_save": True})
    assert Path(result["backup"]).read_bytes() == original
    assert tomllib.loads(context.config.read_text())["media"]["intrinsics"]["fx"] == 500.0
    state = context.camera.status()
    assert state["pending_restart"] and state["runtime"]["rotation_degrees"] == 90
    with pytest.raises(web.cal.CalibrationError):
        context.camera.apply({"confirm_video_interrupt": True})
    assert ["systemctl", "restart", "mediad"] not in calls
    context.camera.enable_apply = True
    with pytest.raises(web.cal.CalibrationError):
        context.camera.apply({})
    result = context.camera.apply({"confirm_video_interrupt": True})
    assert result["restart_requested"] and not result["runtime_verified"]
    assert calls.count(["systemctl", "restart", "mediad"]) == 1
    assert all("robotd" not in command for command in calls)
    context.monitor.close()


@pytest.mark.parametrize("target", TAGS)
def test_real_http_demo_has_correct_target_port_and_csrf_gates(tmp_path, target):
    web, context = context_for(tmp_path, target)
    original = context.config.read_bytes()
    with local_http(web, context) as port:
        code, body = request(port, "GET", "/")
        assert code == 200
        page = body.decode()
        assert target in page and "8768" in page and "8765" not in page
        if target.endswith("0.17.8"):
            assert "0.16.3" not in page
        assert context.csrf in page
        code, body = request(port, "GET", "/api/status")
        status = json.loads(body)
        assert code == 200 and status["demo"] is True and status["target"] == target
        assert status["runtime_verified"] is False
        assert status["camera"]["runtime_verified"] is False
        assert request(port, "POST", "/api/camera/preview", {"rotation": 270})[0] == 403
        assert request(port, "POST", "/api/camera/preview", {"rotation": 270},
                       csrf=context.csrf, origin="https://untrusted.invalid")[0] == 403
        code, body = request(port, "POST", "/api/camera/preview", {"rotation": 270}, csrf=context.csrf)
        assert code == 200 and "270" in json.loads(body)["snippet"]
        assert "config" not in json.loads(body)
        assert request(port, "POST", "/api/camera/save", {"rotation": 270, "confirm_save": True},
                       csrf=context.csrf)[0] == 400
        assert context.config.read_bytes() == original


@pytest.mark.parametrize("target", TAGS)
def test_calibration_is_native_schema_and_keeps_all_fifteen_joints(target):
    calibration, _ = bridge.load_components()
    data = {"schema": 2, "target_tag": target, "joint_zero_rad": [0.0] * 15,
            "joint_direction": [1] * 15, "imu_mount_wxyz": [1.0, 0.0, 0.0, 0.0],
            "imu_module": "bus_imu200", "gyro_profile": "custom_raw_div_16_4",
            "gyro_raw_counts_per_dps": 16.4}
    assert tomllib.loads(calibration.render_toml(data)) == data
    assert calibration.effective_gyro(data)["gyro_raw_counts_per_dps"] == 16.4
    for bad in ({**data, "joint_direction": [1]*10}, {**data, "imu_module": "i2c"}):
        with pytest.raises(calibration.CalibrationError):
            calibration.check_calibration(bad, target)


def test_preparer_rejects_wrong_native_head_without_modifying_files(tmp_path):
    subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True)
    file = tmp_path / "example"
    file.write_text("keep this native source")
    subprocess.run(["git", "add", "example"], cwd=tmp_path, check=True)
    subprocess.run(["git", "-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid",
                    "commit", "--quiet", "-m", "synthetic fixture"], cwd=tmp_path, check=True)
    subprocess.run(["git", "tag", TAGS[0]], cwd=tmp_path, check=True)
    with pytest.raises(native.PreparationError, match="pinned official commit"):
        native.prepare(tmp_path, TAGS[0], apply=True)
    assert file.read_text() == "keep this native source"
    assert subprocess.check_output(["git", "status", "--porcelain"], cwd=tmp_path) == b""


def test_recorded_real_tag_source_patches_match_checksums():
    receipts = json.loads((native.ROOT / "SOURCE_VALIDATION.json").read_text())
    assert {row["target_tag"] for row in receipts} == set(TAGS)
    for receipt in receipts:
        patch = (native.ROOT / receipt["git_patch_file"]).read_bytes()
        assert hashlib.sha256(patch).hexdigest() == receipt["patch_sha256"]
        assert receipt["actual_git_apply_succeeded"] and receipt["reverse_check_succeeded"]
        assert receipt["dry_run_left_checkout_unchanged"] and receipt["git_diff_check_succeeded"]
        assert receipt["native_commit"] == native.TARGETS[receipt["target_tag"]]
        assert receipt["rust_compiled"] is False and receipt["hardware_tested"] is False


def test_launcher_refuses_nonlocal_bind_before_opening_services(monkeypatch):
    with pytest.raises(SystemExit) as error:
        bridge.main(["--target", TAGS[0], "--listen", "0.0.0.0"])
    assert error.value.code == 2


def test_unknown_target_cannot_become_an_adapted_camera():
    with pytest.raises(ValueError):
        bridge.load_web("daemon-v0.17.9")
