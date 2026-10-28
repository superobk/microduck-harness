from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tomllib

import pytest

from microduck_harness.config import Settings
from microduck_harness.deployment import (DeploymentError, create_secrets, load_profile, plan, render)
from microduck_harness.executor import ExecutorSettings

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def site(tmp_path):
    p = tmp_path / "site.toml"
    text = (ROOT / "config/deployment.example.toml").read_text()
    text = text.replace('workstation_root = ".."', 'workstation_root = "pc checkout"')
    text = text.replace('source = "aruco-lab.example.json"',
                        'source = ' + json.dumps(str(ROOT / "config/aruco-lab.example.json")))
    text = text.replace('YOUR_USER@YOUR_ZERO_HOST', 'duck@192.168.1.8').replace('YOUR_ZERO_IP', '192.168.1.8')
    text = text.replace('YOUR_PC_USER', 'pcuser').replace('YOUR_USER', 'duck')
    p.write_text(text)
    return p, tmp_path / "bundle"


@pytest.mark.parametrize("version", ["0.16.3", "0.17.8"])
@pytest.mark.parametrize("transport", ["ssh", "lan"])
def test_generated_services_share_version_network_and_real_plugin_contract(site, version, transport):
    profile, out = site
    profile.write_text(profile.read_text().replace('version = "0.17.8"', f'version = "{version}"')
                       .replace('transport = "ssh"', f'transport = "{transport}"'))
    result = render(profile, out)
    gateway = json.loads((out / "pc/gateway.json").read_text())
    vision = json.loads((out / "pc/vision.json").read_text())
    executor = json.loads((out / "zero/executor.json").read_text())
    Settings.model_validate({**gateway, "gateway_token": "g", "vision_token": "v", "executor_token": "e"})
    ExecutorSettings(**executor, token="s"*32).validate()
    expected = "127.0.0.1" if transport == "ssh" else "192.168.1.8"
    assert gateway["executor_url"] == f"http://{expected}:8766"
    assert vision["camera_url"] == gateway["executor_url"] + "/frame"
    assert vision["state_url"] == gateway["executor_url"] + "/state"
    assert vision["tof_url"] == gateway["executor_url"] + "/tof"
    assert executor["bind_host"] == ("127.0.0.1" if transport == "ssh" else "0.0.0.0")
    assert executor["robotd_version"] == version
    assert executor["require_native_plugins"] is True
    assert executor["allow_motion"] is False
    assert result["vision_calibrated_fields_present"] is False
    overlay = tomllib.loads((out / "zero/native-overlay.toml").read_text())
    assert overlay == {"bus": {"calibration_path": "/etc/robot/robot-calibration.toml"},
                       "media": {"camera_rotate": 90}}
    # An overlay is never an invented measured calibration or complete native config.
    assert "joint_zero_rad" not in (out / "zero/native-overlay.toml").read_text()
    assert gateway["tools_url"] == "http://127.0.0.1:8768"


def test_render_preserves_runtime_models_credentials_and_unmanaged_files(site):
    profile, out = site
    render(profile, out)
    create_secrets(out)
    secrets_before = (out / "pc/secrets.env").read_bytes()
    data = profile.parent / "pc checkout/data/live"
    data.mkdir(parents=True)
    saved_model = data / "model-settings.json"
    saved_model.write_text('{"operator":"kept"}')
    profile.write_text(profile.read_text().replace('model_id = "GLM5.3-Flash"', 'model_id = "new-initial-default"'))
    render(profile, out)
    assert (out / "pc/secrets.env").read_bytes() == secrets_before
    assert saved_model.read_text() == '{"operator":"kept"}'
    (out / "pc/gateway.json").write_text('{"local_edit":true}')
    with pytest.raises(DeploymentError, match="local edits"):
        render(profile, out)
    assert json.loads((out / "pc/gateway.json").read_text()) == {"local_edit": True}


def test_secret_split_permissions_and_no_accidental_rotation(site):
    profile, out = site
    render(profile, out)
    report = create_secrets(out)
    pc = (out / "pc/secrets.env").read_text()
    zero = (out / "zero/secrets.env").read_text()
    assert report["secret_values_returned"] is False
    assert "GATEWAY_TOKEN" not in zero and "GLM_API_KEY" not in zero and "VISION_TOKEN" not in zero
    line = next(line for line in pc.splitlines() if line.startswith("EXECUTOR_TOKEN="))
    assert zero.strip() == line and len(line.partition("=")[2]) >= 32
    assert line.partition("=")[2] not in json.dumps(report)
    for dest in (out / "pc/secrets.env", out / "zero/secrets.env"):
        assert dest.stat().st_mode & 0o777 == 0o600
    with pytest.raises(DeploymentError, match="already exist"):
        create_secrets(out)
    assert (out / "pc/secrets.env").read_text() == pc


def test_measured_camera_fields_are_explicitly_merged_without_changing_map(site):
    profile, out = site
    calibration = profile.parent / "camera.json"
    k = [[700, 0, 320], [0, 701, 240], [0, 0, 1]]
    calibration.write_text(json.dumps({"camera_matrix": k, "distortion": [0]*5,
                                       "image_size": [640, 480], "untrusted_extra": "not copied"}))
    profile.write_text(profile.read_text().replace('calibration_result = ""', 'calibration_result = "camera.json"'))
    files, report = plan(profile, out)
    vision = json.loads(files["pc/vision.json"])
    original = json.loads((ROOT / "config/aruco-lab.example.json").read_text())
    assert vision["camera_matrix"] == k and vision["markers"] == original["markers"]
    assert "untrusted_extra" not in vision
    assert report["vision_calibrated_fields_present"] is True


def test_custom_output_git_ignores_credentials_and_host_specific_configuration(site):
    profile, out = site
    subprocess.run(["git", "init", "--quiet", str(profile.parent)], check=True)
    render(profile, out)
    create_secrets(out)
    result = subprocess.run(["git", "status", "--porcelain", "--untracked-files=all"],
                            cwd=profile.parent, check=True, capture_output=True, text=True)
    assert "bundle/" not in result.stdout
    for relative in ("pc/secrets.env", "zero/secrets.env", "pc/gateway.json", "deployment.json"):
        assert subprocess.run(["git", "check-ignore", "--quiet", str(out / relative)],
                              cwd=profile.parent).returncode == 0


@pytest.mark.parametrize("old,new", [
    ('tools_port = 8768', 'tools_port = 8765'),
    ('version = "0.17.8"', 'version = "0.17.9"'),
    ('rotation = 90', 'rotation = 45'),
    ('ssh_target = "duck@192.168.1.8"', 'ssh_target = "-oProxyCommand=bad"'),
    ('speed_m_s = 0.06', 'speed_m_s = 1'),
    ('base_url = ""', 'base_url = "https://user:secret@example.com/v1"'),
    ('json_mode = true', 'json_mode = true\napi_key = "do-not-echo-this"'),
])
def test_profile_rejects_conflicts_unknown_versions_unsafe_values_and_secrets(site, old, new):
    profile, _ = site
    profile.write_text(profile.read_text().replace(old, new))
    with pytest.raises(DeploymentError) as err:
        load_profile(profile)
    assert "do-not-echo-this" not in str(err.value) and "secret@" not in str(err.value)


def test_generated_launch_scripts_pass_shell_syntax_and_execute_explicit_service(site):
    profile, out = site
    render(profile, out)
    create_secrets(out)
    for script in out.rglob("*.sh"):
        subprocess.run(["bash", "-n", str(script)], check=True)
    # Replace only the selected temporary installation's executable with a probe.
    # This executes the generated script's path/quoting/environment behavior,
    # without opening sockets or contacting any model/robot.
    probe = profile.parent / "pc checkout/.venv/bin/duck-harness"
    probe.parent.mkdir(parents=True)
    probe.write_text('#!' + sys.executable + '\nimport json,os,sys\nprint(json.dumps({"args":sys.argv[1:],"has_key":bool(os.environ.get("GATEWAY_TOKEN")),"cwd":os.getcwd()}))\n')
    probe.chmod(0o700)
    result = subprocess.run(["bash", str(out / "pc/start-gateway.sh")], check=True, capture_output=True, text=True)
    data = json.loads(result.stdout)
    assert data["has_key"] is True
    assert data["args"] == ["gateway", "--config", str(out / "pc/gateway.json"), "--host", "127.0.0.1", "--port", "8765"]
    assert data["cwd"].endswith("pc checkout")


def test_native_overlay_dry_run_and_apply_preserve_unrelated_config(tmp_path):
    spec = importlib.util.spec_from_file_location("apply_native_config", ROOT / "scripts/apply_native_config.py")
    native = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(native)
    cal, _ = native.load_components()
    measurement = tmp_path / "measured-test-only.toml"
    measurement.write_text(cal.render_toml({"schema": 2, "target_tag": "daemon-v0.17.8",
        "joint_zero_rad": [0.0]*15, "joint_direction": [1]*15,
        "imu_mount_wxyz": [1, 0, 0, 0], "imu_module": "bus_imu200",
        "gyro_profile": cal.DEFAULT_GYRO_PROFILE,
        "gyro_raw_counts_per_dps": cal.gyro_scale_counts(cal.DEFAULT_GYRO_PROFILE)}))
    config = tmp_path / "robotd.toml"
    original = '[board]\nversion="zero3"\n[bus]\nport="/dev/ttyS2"\n[idle]\nenabled=false\n'
    config.write_text(original)
    overlay = tmp_path / "overlay.toml"
    overlay.write_text('[bus]\ncalibration_path=' + json.dumps(str(measurement)) + '\n[media]\ncamera_rotate=90\n')
    args = ["--config", str(config), "--overlay", str(overlay), "--target", "daemon-v0.17.8"]
    assert native.main(args) == 0
    assert config.read_text() == original
    assert native.main(args + ["--apply"]) == 0
    result = tomllib.loads(config.read_text())
    assert result["bus"]["port"] == "/dev/ttyS2" and result["idle"]["enabled"] is False
    assert result["bus"]["calibration_path"] == str(measurement)
    assert result["media"]["camera_rotate"] == 90
    assert native.main(args + ["--apply"]) == 0  # already-matching merge is harmless
    overlay.write_text(overlay.read_text() + '\n[idle]\nenabled=true\n')
    with pytest.raises(ValueError, match="only set"):
        native.prepare(config, overlay, "daemon-v0.17.8")
