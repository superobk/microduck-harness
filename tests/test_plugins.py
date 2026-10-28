"""Final native configuration/receipt semantics, without any physical device."""
from __future__ import annotations

import asyncio
from dataclasses import replace
import errno
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace
import time

import httpx
import pytest
from fastapi import HTTPException

from microduck_harness.executor import ActionRequest, ExecutorService, ExecutorSettings, create_executor_app
from microduck_harness.plugins import (
    NativePluginInspector, PluginEvidenceError, PluginPaths, ProcessVerifier,
)
from test_executor import FakeRobot, request_for


def calibration_text(tag="daemon-v0.17.8", zero=0.0, schema=2):
    result = (f'schema = {schema}\ntarget_tag = "{tag}"\n'
              f'joint_zero_rad = {json.dumps([zero] + [0.0]*14)}\n'
              f'joint_direction = {json.dumps([1]*15)}\n'
              'imu_mount_wxyz = [1.0, 0.0, 0.0, 0.0]\n')
    if schema == 2:
        result += 'imu_module = "bus_imu200"\ngyro_profile = "custom_raw_div_16_4"\ngyro_raw_counts_per_dps = 16.4\n'
    return result


@pytest.fixture
def evidence(tmp_path):
    cal = tmp_path / "robot-calibration.toml"
    config = tmp_path / "robotd.toml"
    cr = tmp_path / "calibration-effective.json"
    mr = tmp_path / "camera-effective.json"
    cal.write_text(calibration_text())
    config.write_text(f'[board]\nversion="zero3"\n[bus]\ncalibration_path={json.dumps(str(cal))}\n[media]\ncamera_rotate=90\n')
    calibration = {"source": "robotd:DynamixelIo:live", "bus_verified": True, "target_tag": "daemon-v0.17.8",
                   "pid": 3001, "calibration_path": str(cal), "joint_zero_rad": [0.0]*15,
                   "joint_direction": [1]*15, "imu_mount_wxyz": [1.0, 0, 0, 0],
                   "imu_module": "bus_imu200", "gyro_raw_counts_per_dps": 16.4}
    camera = {"source": "mediad:camera:live", "target_tag": "daemon-v0.17.8", "pid": 3002,
              "pipeline_started": True, "config_path": str(config), "rotation_degrees": 90,
              "rotation_source": "configuration", "frame_rotation_degrees": 90,
              "flip_in_pipeline": False, "width": 1280, "height": 720}
    cr.write_text(json.dumps(calibration)); mr.write_text(json.dumps(camera))
    paths = PluginPaths(str(config), str(cr), str(mr))

    def process(name, pid, st):
        assert pid == {"robotd": 3001, "mediad": 3002}[name]
        return {"pid": pid, "start_ticks": 123, "executable": name, "receipt_mtime_ns": st.st_mtime_ns}

    return SimpleNamespace(cal=cal, config=config, cr=cr, mr=mr, calibration=calibration,
                           camera=camera, paths=paths, process=process)


def inspector(evidence, *, version="0.17.8", required=True):
    return NativePluginInspector(evidence.paths, required=required, robotd_version=version,
                                 process_verifier=evidence.process)


@pytest.mark.parametrize("version", ["0.16.3", "0.17.8"])
def test_saved_matches_actual_decoder_and_camera_for_both_tags(evidence, version):
    tag = "daemon-v" + version
    evidence.cal.write_text(calibration_text(tag))
    evidence.calibration["target_tag"] = tag
    evidence.camera["target_tag"] = tag
    evidence.cr.write_text(json.dumps(evidence.calibration))
    evidence.mr.write_text(json.dumps(evidence.camera))
    report = inspector(evidence, version=version).inspect()
    assert report["ready"] and report["status"] == "active"
    assert report["calibration"]["active"]["gyro_raw_counts_per_dps"] == 16.4
    assert report["camera"]["active"]["rotation_degrees"] == 90
    assert len(report["generation"]) == 64


def test_schema_one_uses_native_factory_gyro_scale(evidence):
    evidence.cal.write_text(calibration_text(schema=1))
    evidence.calibration["gyro_raw_counts_per_dps"] = 1 / .0175
    evidence.cr.write_text(json.dumps(evidence.calibration))
    report = inspector(evidence).inspect()
    assert report["ready"]
    assert report["calibration"]["saved"]["schema"] == 1
    assert report["calibration"]["active"]["gyro_raw_counts_per_dps"] == pytest.approx(57.14285714)


def test_configuration_saved_is_not_running_calibration(evidence):
    evidence.cal.write_text(calibration_text(zero=.1))
    report = inspector(evidence).inspect()
    assert not report["ready"] and report["status"] == "pending"
    assert report["calibration"]["saved"]["joint_zero_rad"][0] == .1
    assert report["calibration"]["active"]["joint_zero_rad"][0] == 0
    assert report["generation"] is None


def test_saved_rotation_is_pending_without_restarting_mediad(evidence):
    evidence.config.write_text(evidence.config.read_text().replace("camera_rotate=90", "camera_rotate=270"))
    report = inspector(evidence).inspect()
    assert not report["ready"] and report["camera"]["status"] == "pending"
    assert report["camera"]["active"]["rotation_degrees"] == 90


@pytest.mark.parametrize("field,value", [
    ("source", "simulated"), ("bus_verified", False), ("bus_verified", 1),
    ("target_tag", "daemon-v0.16.3"), ("joint_zero_rad", [0]*10),
    ("joint_direction", [True]*15), ("joint_direction", [0]*15),
    ("imu_mount_wxyz", [0, 0, 0, 0]), ("gyro_raw_counts_per_dps", math.nan),
    ("imu_module", "i2c"), ("calibration_path", "relative.toml"),
])
def test_invalid_live_calibration_never_becomes_active(evidence, field, value):
    evidence.calibration[field] = value
    evidence.cr.write_text(json.dumps(evidence.calibration))
    report = inspector(evidence).inspect()
    assert not report["ready"] and report["calibration"]["status"] != "active"
    assert report["calibration"]["reasons"]


@pytest.mark.parametrize("field,value", [
    ("source", "simulated"), ("pipeline_started", 1), ("target_tag", "daemon-v0.16.3"),
    ("flip_in_pipeline", True), ("flip_in_pipeline", None),
    ("rotation_degrees", 45), ("rotation_degrees", True), ("frame_rotation_degrees", 0),
    ("config_path", "/another/robotd.toml"), ("width", 0),
    ("height", True), ("rotation_source", "unknown"),
])
def test_invalid_live_camera_blocks_raw_pixel_contract(evidence, field, value):
    evidence.camera[field] = value
    evidence.mr.write_text(json.dumps(evidence.camera))
    report = inspector(evidence).inspect()
    assert not report["ready"] and report["camera"]["status"] != "active"


def test_command_line_camera_override_is_visible_and_blocks_unified_readiness(evidence):
    evidence.camera["rotation_source"] = "command_line"
    evidence.mr.write_text(json.dumps(evidence.camera))
    report = inspector(evidence).inspect()
    assert not report["ready"] and report["camera"]["status"] == "pending"
    assert "command-line" in report["camera"]["reasons"][0]


def test_auto_requires_hello_identity_and_mismatched_selected_version_is_rejected(evidence):
    auto = inspector(evidence, version="auto")
    assert not auto.inspect()["ready"]
    assert auto.inspect({"profile_version": "0.17.8"})["ready"]
    assert not inspector(evidence).inspect({"profile_version": "0.16.3"})["ready"]


def test_absent_receipt_is_saved_but_never_active(evidence):
    evidence.cr.unlink()
    report = inspector(evidence).inspect()
    assert report["calibration"]["status"] == "saved"
    assert not report["ready"]
    assert inspector(evidence, required=False).inspect()["status"] == "disabled"
    assert inspector(evidence, required=False).inspect()["ready"]


def test_error_does_not_echo_file_content_and_links_are_rejected(evidence):
    sentinel = "DO-NOT-ECHO-PRIVATE-FILE-CONTENT"
    evidence.cal.write_text(sentinel + " = [invalid TOML")
    assert sentinel not in json.dumps(inspector(evidence).inspect())
    evidence.cal.unlink()
    evidence.cal.symlink_to(evidence.cr)
    assert not inspector(evidence).inspect()["ready"]


@pytest.mark.parametrize("change", ["pid", "executable", "reused_pid", "future_receipt", "dead"])
def test_process_identity_rejects_old_or_wrong_receipt(monkeypatch, change):
    verifier = ProcessVerifier()
    verifier._systemd_pid = lambda name: 4002 if change == "pid" else 4001
    monkeypatch.setattr(os, "readlink", lambda path: "/usr/bin/other" if change == "executable" else "/usr/bin/robotd")
    fields = ["Z" if change == "dead" else "S"] + ["0"]*19
    fields[19] = "10010" if change == "reused_pid" else "9000"
    monkeypatch.setattr(Path, "read_text", lambda *a, **kw: "4001 (robotd) " + " ".join(fields))
    monkeypatch.setattr(os, "sysconf", lambda name: 100)
    monkeypatch.setattr(time, "clock_gettime", lambda clock: 100.0)
    monkeypatch.setattr(time, "time", lambda: 1000.0)
    stamp = 1002.0 if change == "future_receipt" else 1000.0
    with pytest.raises(PluginEvidenceError):
        verifier("robotd", 4001, SimpleNamespace(st_mtime=stamp, st_mtime_ns=int(stamp*1e9)))


def test_old_receipt_is_valid_for_its_still_running_process(monkeypatch):
    verifier = ProcessVerifier()
    verifier._systemd_pid = lambda name: 4001
    monkeypatch.setattr(os, "readlink", lambda path: "/usr/bin/robotd")
    fields = ["S"] + ["0"]*19; fields[19] = "1000"
    monkeypatch.setattr(Path, "read_text", lambda *a, **kw: "4001 (robotd) " + " ".join(fields))
    monkeypatch.setattr(os, "sysconf", lambda name: 100)
    monkeypatch.setattr(time, "clock_gettime", lambda clock: 500_000.0)
    monkeypatch.setattr(time, "time", lambda: 1_000_000.0)
    identity = verifier("robotd", 4001, SimpleNamespace(st_mtime=500_011, st_mtime_ns=500_011_000_000_000))
    assert identity["start_ticks"] == 1000  # No artificial seconds-long TTL for startup receipts.


def native_unit(name="robotd", pid=4001):
    executable = f"/opt/robot/daemon/current/bin/{name}"
    return {
        "MainPID": str(pid), "ExecMainPID": str(pid), "Type": "exec",
        "ActiveState": "active", "SubState": "running",
        "ExecStart": (f"{{ path={executable} ; argv[]={executable} --socket /run/{name}.sock ; "
                      f"ignore_errors=no ; start_time=[Wed 2026-10-28 00:00:00 UTC] ; "
                      f"stop_time=[n/a] ; pid={pid} ; code=(null) ; status=0/0 }}"),
    }


def denied_process_boundary(monkeypatch, *, name="robotd", denied_errno=errno.EACCES, unit=None):
    unit = native_unit(name) if unit is None else unit
    calls, proc_reads = [], []

    def show(command, **kwargs):
        calls.append(command)
        assert command[:3] == ["systemctl", "show", name + ".service"]
        assert kwargs == {"capture_output": True, "text": True, "timeout": .15, "check": True}
        return SimpleNamespace(stdout="\n".join(f"{key}={value}" for key, value in unit.items()) + "\n")

    def denied(path):
        assert path == "/proc/4001/exe"
        raise OSError(denied_errno, "cross-UID proc executable is restricted")

    def proc_stat(path, *args, **kwargs):
        assert str(path) == "/proc/4001/stat"
        proc_reads.append(str(path))
        fields = ["S"] + ["0"]*19
        fields[19] = "9000"
        return f"4001 ({name}) " + " ".join(fields)

    monkeypatch.setattr(subprocess, "run", show)
    monkeypatch.setattr(os, "readlink", denied)
    monkeypatch.setattr(Path, "read_text", proc_stat)
    monkeypatch.setattr(os, "sysconf", lambda name: 100)
    monkeypatch.setattr(time, "clock_gettime", lambda clock: 100.0)
    monkeypatch.setattr(time, "time", lambda: 1000.0)
    monkeypatch.setattr(time, "monotonic", lambda: 10.0)
    return calls, proc_reads


@pytest.mark.parametrize("name", ["robotd", "mediad"])
@pytest.mark.parametrize("denied_errno", [errno.EACCES, errno.EPERM])
def test_cross_uid_exe_denial_requires_complete_systemd_identity(monkeypatch, name, denied_errno):
    calls, proc_reads = denied_process_boundary(monkeypatch, name=name, denied_errno=denied_errno)
    verifier = ProcessVerifier()
    stamp = SimpleNamespace(st_mtime=1000.0, st_mtime_ns=1_000_000_000_000)
    identity = verifier(name, 4001, stamp)
    assert identity["executable_evidence"] == "systemd_exec"
    assert identity["executable"] == name and identity["start_ticks"] == 9000
    assert verifier(name, 4001, stamp) == identity
    assert len(calls) == 1  # One bounded property snapshot, not a new query per fallback.
    assert len(proc_reads) == 2  # A cached unit never replaces current kernel identity.


@pytest.mark.parametrize("change", [
    "main_pid", "exec_main_pid", "exec_pid", "shell", "argv0", "alternate_path",
    "multiple_commands", "duplicate_field", "ignore_errors", "inactive", "stopped",
    "forking", "missing_property",
])
def test_permission_fallback_rejects_untrusted_or_ambiguous_systemd_contract(monkeypatch, change):
    unit = native_unit()
    if change == "main_pid":
        unit["MainPID"] = "4002"
    elif change == "exec_main_pid":
        unit["ExecMainPID"] = "4002"
    elif change == "exec_pid":
        unit["ExecStart"] = unit["ExecStart"].replace("pid=4001", "pid=4002")
    elif change == "shell":
        unit["ExecStart"] = unit["ExecStart"].replace("path=/opt/robot/daemon/current/bin/robotd", "path=/bin/sh")
    elif change == "argv0":
        unit["ExecStart"] = unit["ExecStart"].replace("argv[]=/opt/robot/daemon/current/bin/robotd", "argv[]=/bin/sh")
    elif change == "alternate_path":
        unit["ExecStart"] = unit["ExecStart"].replace("/opt/robot/daemon/current/bin/robotd", "/tmp/robotd")
    elif change == "multiple_commands":
        unit["ExecStart"] += " ; " + unit["ExecStart"]
    elif change == "duplicate_field":
        unit["ExecStart"] = unit["ExecStart"].replace(" ; code=", " ; pid=4001 ; code=")
    elif change == "ignore_errors":
        unit["ExecStart"] = unit["ExecStart"].replace("ignore_errors=no", "ignore_errors=yes")
    elif change == "inactive":
        unit["ActiveState"] = "inactive"
    elif change == "stopped":
        unit["SubState"] = "dead"
    elif change == "forking":
        unit["Type"] = "forking"
    elif change == "missing_property":
        unit.pop("ExecMainPID")
    denied_process_boundary(monkeypatch, unit=unit)
    with pytest.raises(PluginEvidenceError):
        ProcessVerifier()("robotd", 4001, SimpleNamespace(st_mtime=1000, st_mtime_ns=1_000_000_000_000))


@pytest.mark.parametrize("reason", ["missing_exe", "wrong_exe", "wrong_comm", "reused_pid", "hidden_stat"])
def test_permission_fallback_does_not_bypass_kernel_evidence(monkeypatch, reason):
    denied_process_boundary(monkeypatch)
    if reason == "missing_exe":
        def gone(path):
            raise FileNotFoundError(errno.ENOENT, "process exited")
        monkeypatch.setattr(os, "readlink", gone)
    elif reason == "wrong_exe":
        monkeypatch.setattr(os, "readlink", lambda path: "/usr/bin/python3")
    else:
        def proc_stat(path, *args, **kwargs):
            if reason == "hidden_stat":
                raise PermissionError(errno.EACCES, "proc stat is hidden")
            fields = ["S"] + ["0"]*19
            fields[19] = "10010" if reason == "reused_pid" else "9000"
            return "4001 (" + ("python3" if reason == "wrong_comm" else "robotd") + ") " + " ".join(fields)
        monkeypatch.setattr(Path, "read_text", proc_stat)
    with pytest.raises(PluginEvidenceError):
        ProcessVerifier()("robotd", 4001, SimpleNamespace(st_mtime=1000, st_mtime_ns=1_000_000_000_000))


@pytest.mark.skipif(sys.platform != "linux" or not hasattr(os, "geteuid") or os.geteuid() != 0,
                    reason="real cross-UID /proc check requires a root-owned local fixture")
def test_real_cross_uid_proc_denial_with_synthetic_native_process(tmp_path):
    """Only a temporary sleep process and a privilege-dropping reader are run.

    systemd is a fixture; the kernel exe denial, comm, PID and start time are
    real. No native service or hardware is accessed and the parent keeps its UID.
    """
    binary = tmp_path / "robotd"
    shutil.copyfile("/bin/sleep", binary)
    binary.chmod(0o755)
    process = subprocess.Popen([str(binary), "10"])
    read_fd, write_fd = os.pipe()
    reader = None
    try:
        stamp = time.time()
        receipt = SimpleNamespace(st_mtime=stamp, st_mtime_ns=int(stamp * 1e9))
        reader = os.fork()
        if reader == 0:
            os.close(read_fd)
            try:
                os.setgroups([])
                os.setgid(65534)
                os.setuid(65534)
                try:
                    os.readlink(f"/proc/{process.pid}/exe")
                except OSError as exc:
                    assert exc.errno in {errno.EACCES, errno.EPERM}
                else:
                    raise AssertionError("kernel did not restrict the cross-UID executable")
                verifier = ProcessVerifier()
                verifier._systemd_properties = lambda name: native_unit(name, process.pid)
                identity = verifier("robotd", process.pid, receipt)
                result = {"ok": True, "identity": identity}
            except BaseException as exc:
                result = {"ok": False, "error": type(exc).__name__ + ": " + str(exc)}
            os.write(write_fd, json.dumps(result).encode())
            os.close(write_fd)
            os._exit(0)
        os.close(write_fd)
        write_fd = None
        result = json.loads(os.read(read_fd, 8192))
        _, status = os.waitpid(reader, 0)
        reader = None
        assert status == 0 and result["ok"], result
        assert result["identity"]["executable_evidence"] == "systemd_exec"
        assert result["identity"]["pid"] == process.pid
    finally:
        if write_fd is not None:
            os.close(write_fd)
        os.close(read_fd)
        if reader is not None:
            os.waitpid(reader, 0)
        process.terminate()
        process.wait(timeout=2)


def make_service(evidence, robot=None):
    cfg = ExecutorSettings(allow_motion=True, require_native_plugins=True, robotd_version="0.17.8",
                           native_config_path=str(evidence.config), calibration_runtime_path=str(evidence.cr),
                           camera_runtime_path=str(evidence.mr))
    service = ExecutorService(cfg, robot or FakeRobot())
    service.plugins.verify_process = evidence.process
    return service


@pytest.mark.asyncio
async def test_required_plugins_remove_ticket_and_stop_remains_independent(evidence):
    service = make_service(evidence)
    evidence.cr.unlink()
    state = await service.snapshot()
    assert state["healthy"] is False and state["state_token"] is None and state["state_token_ttl_s"] == 0
    assert state["plugins"]["status"] == "saved"
    stopped = await service.stop()
    assert stopped["stop"]["accepted"]
    assert [command[0] for command in service.robot.commands] == ["robot.stop"]


@pytest.mark.asyncio
async def test_configuration_change_after_ticket_blocks_before_movement(evidence):
    service = make_service(evidence)
    request = await request_for(service)
    evidence.cal.write_text(calibration_text(zero=.1))
    with pytest.raises(HTTPException) as error:
        await service.execute(request)
    assert error.value.status_code == 409
    assert not service.robot.commands


@pytest.mark.asyncio
async def test_new_native_generation_fences_old_ticket_even_if_values_match(evidence):
    service = make_service(evidence)
    request = await request_for(service)
    st = evidence.cr.stat()
    os.utime(evidence.cr, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
    with pytest.raises(HTTPException, match="changed after"):
        await service.execute(request)
    assert not service.robot.commands


@pytest.mark.asyncio
async def test_live_calibration_change_during_pulse_stops_next_refresh(evidence):
    class ChangingRobot(FakeRobot):
        async def call(self, method, params=None):
            result = await super().call(method, params)
            if method == "robot.move":
                evidence.cal.write_text(calibration_text(zero=.1))
            return result

    service = make_service(evidence, ChangingRobot())
    request = await request_for(service, duration_s=.25)
    result = await service.execute(request)
    assert result["status"] == "failed" and result["move_refreshes"] == 1
    assert "native_plugins_not_active" in result["error"]
    assert service.robot.commands[-1][0] == "robot.stop"


@pytest.mark.asyncio
async def test_active_plugin_pulse_preserves_native_values_and_finishes(evidence):
    service = make_service(evidence)
    request = await request_for(service, duration_s=.12)
    result = await service.execute(request)
    assert result["status"] == "completed" and result["move_refreshes"] >= 1
    moves = [params for method, params, _ in service.robot.commands if method == "robot.move"]
    assert all(params == {"vx": .04, "vy": 0.0, "vyaw": 0.0} for params in moves)
    assert result["verified_success"] is False


@pytest.mark.asyncio
async def test_plugin_preflight_time_is_charged_to_ticket_expiry(evidence):
    service = make_service(evidence)
    service.settings = replace(service.settings, max_observation_age_s=.05)
    request = await request_for(service, duration_s=.02)
    original = service.plugin_status

    async def slow_status(*args):
        await asyncio.sleep(.06)
        return await original(*args)

    service.plugin_status = slow_status
    with pytest.raises(HTTPException, match="state_token expired"):
        await service.execute(request)
    assert not service.robot.commands


@pytest.mark.asyncio
async def test_delayed_pulse_start_cannot_extend_the_perception_deadline(evidence):
    service = make_service(evidence)
    service.settings = replace(service.settings, max_observation_age_s=.08)
    request = await request_for(service, duration_s=.03)
    original = service._pulse

    async def delayed_pulse(*args):
        await asyncio.sleep(.09)
        return await original(*args)

    service._pulse = delayed_pulse
    result = await service.execute(request)
    assert result["status"] == "failed" and "before the pulse" in result["error"]
    assert [method for method, _, _ in service.robot.commands] == ["robot.stop"]


@pytest.mark.asyncio
async def test_stop_waiting_for_preflight_fences_the_old_epoch(evidence):
    service = make_service(evidence)
    request = await request_for(service, duration_s=.2)
    entered, release = asyncio.Event(), asyncio.Event()
    original = service.plugin_status

    async def pending_status(*args):
        entered.set()
        await release.wait()
        return await original(*args)

    service.plugin_status = pending_status
    executing = asyncio.create_task(service.execute(request))
    await entered.wait()
    stopping = asyncio.create_task(service.stop())
    await asyncio.sleep(0)
    release.set()
    stopped = await stopping
    result = await executing
    assert stopped["epoch"] > request.epoch
    assert result["status"] == "cancelled"
    assert service.active is None and not service.tickets
    with pytest.raises(HTTPException, match="stale control epoch"):
        await service.execute(request.model_copy(update={"request_id": "stale-new-id"}))
    assert service.robot.commands[-1][0] == "robot.stop"


@pytest.mark.asyncio
async def test_read_only_plugin_endpoint_is_authenticated_and_has_no_mutations(evidence):
    cfg = make_service(evidence).settings
    app = create_executor_app(replace(cfg, token="test-token", allow_motion=False), robot=FakeRobot())
    app.state.executor.plugins.verify_process = evidence.process
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://executor") as client:
        assert (await client.get("/plugins")).status_code == 401
        response = await client.get("/plugins", headers={"Authorization": "Bearer test-token"})
        assert response.status_code == 200 and response.json()["ready"]
        assert (await client.post("/plugins", headers={"Authorization": "Bearer test-token"}, json={})).status_code == 405
    assert not app.state.executor.robot.commands


def test_settings_environment_and_paths(monkeypatch):
    monkeypatch.setenv("EXECUTOR_REQUIRE_NATIVE_PLUGINS", "1")
    monkeypatch.setenv("NATIVE_CONFIG_PATH", "/etc/custom/robotd.toml")
    cfg = ExecutorSettings.from_env()
    assert cfg.require_native_plugins and cfg.native_config_path == "/etc/custom/robotd.toml"
    cfg.validate()
    with pytest.raises(ValueError, match="absolute"):
        replace(cfg, native_config_path="relative.toml").validate()
    with pytest.raises(ValueError, match="boolean"):
        replace(cfg, require_native_plugins="false").validate()
    monkeypatch.setenv("EXECUTOR_REQUIRE_NATIVE_PLUGINS", "treu")
    with pytest.raises(ValueError, match="0 or 1"):
        ExecutorSettings.from_env()
