"""Camera config and HTTP lifecycle, without accessing any real service or camera."""
import argparse
import http.client
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import tomllib
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / 'integration-0.16.3/web'
sys.path.insert(0, str(WEB))
spec = importlib.util.spec_from_file_location('unified_calibration_web', WEB / 'calibration_web.py')
web = importlib.util.module_from_spec(spec)
spec.loader.exec_module(web)
camera = web.camera_config


class EditTests(unittest.TestCase):
    def test_all_quarter_turns_and_reset_preserve_other_fields(self):
        original = '# robot\n[bus]\ncalibration_path="/etc/robot/cal.toml"\n[media]\nquality="720p30"\n[media.intrinsics]\nfx=123.0\n[policy]\nenabled=false\n'
        for value in camera.ANGLES:
            edited = camera.edit(original, value)
            parsed = tomllib.loads(edited)
            self.assertEqual(parsed['media']['camera_rotate'], value)
            self.assertEqual(parsed['media']['intrinsics']['fx'], 123.0)
            self.assertEqual(parsed['bus']['calibration_path'], '/etc/robot/cal.toml')
            reset = camera.edit(edited, None)
            self.assertEqual(tomllib.loads(reset), tomllib.loads(original))
            self.assertIn('# robot', reset)

    def test_wrong_types_angles_and_ambiguous_layout_are_refused(self):
        for value in (True, False, '90', 90.0, -90, 45, 360, [], {}):
            with self.subTest(value=value), self.assertRaises(web.cal.CalibrationError):
                camera.edit('[media]\n', value)
        with self.assertRaises(web.cal.CalibrationError):
            camera.edit('media.camera_rotate=90\n', 270)

    def test_new_table_and_no_trailing_newline_are_valid(self):
        for text in ('[bus]\nport="x"', '[media]\nquality="720p30"'):
            self.assertEqual(tomllib.loads(camera.edit(text, 270))['media']['camera_rotate'], 270)


class CameraHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.dir = Path(self.temp.name)
        self.config = self.dir / 'robotd.toml'
        self.config.write_text('[board]\nversion="zero3"\n[media]\nquality="720p30"\n[bus]\ncalibration_path="/keep/cal.toml"\n')
        self.runtime = self.dir / 'camera.json'
        self.live = {'source': 'mediad:camera:live', 'target_tag': 'daemon-v0.16.3',
                     'pid': 123, 'pipeline_started': True, 'config_path': str(self.config),
                     'rotation_degrees': 90, 'rotation_source': 'board_default',
                     'flip_in_pipeline': False, 'width': 1280, 'height': 720}
        self.runtime.write_text(json.dumps(self.live))
        args = argparse.Namespace(target='daemon-v0.16.3', config=str(self.config),
                                  dest=str(self.dir/'cal.toml'), runtime=str(self.dir/'robot.json'),
                                  demo=False, allow_inactive=False, robotctl='robotctl', no_telemetry=True,
                                  camera_runtime=str(self.runtime), camera_web_port=8080, enable_camera_apply=True)
        self.ctx = web.Context(args)
        self.server = web.ThreadingHTTPServer(('127.0.0.1', 0), web.new_handler(self.ctx))
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.worker.join(timeout=3)
        self.temp.cleanup()

    def request(self, route, body=None, csrf=True, method='POST', origin=None):
        headers = {'Content-Type': 'application/json'}
        if csrf: headers['X-Calibration-CSRF'] = self.ctx.csrf
        if origin: headers['Origin'] = origin
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_address[1], timeout=5)
        conn.request(method, route, json.dumps(body) if body is not None else None, headers)
        response = conn.getresponse(); data = response.read(); conn.close()
        return response.status, json.loads(data)

    def test_preview_without_camera_changes_no_files_or_services(self):
        before = self.config.read_bytes()
        with patch.object(camera.subprocess, 'run') as run:
            code, data = self.request('/api/camera/preview', {'rotation': 270})
            self.assertEqual(code, 200)
            self.assertEqual(data['snippet'], '[media]\ncamera_rotate = 270\n')
            self.assertNotIn('config', data)
            run.assert_not_called()
        self.assertEqual(before, self.config.read_bytes())

    def test_csrf_origin_and_payload_validation_block_mutations(self):
        before = self.config.read_bytes()
        with patch.object(camera.subprocess, 'run') as run:
            self.assertEqual(self.request('/api/camera/apply', {'confirm_video_interrupt': True}, csrf=False)[0], 403)
            self.assertEqual(self.request('/api/camera/save', {'rotation': 90}, origin='https://evil.test')[0], 403)
            self.assertEqual(self.request('/api/camera/preview', ['bad'])[0], 400)
            self.assertEqual(self.request('/api/camera/preview', {'rotation': True})[0], 400)
            run.assert_not_called()
        self.assertEqual(before, self.config.read_bytes())

    def test_save_backup_pending_apply_and_new_pid_receipt(self):
        with patch.object(camera, 'verify_runtime', return_value=(True, 'matched test PID')):
            code, result = self.request('/api/camera/save', {'rotation': 270, 'confirm_save': True})
            self.assertEqual(code, 200)
            self.assertTrue(Path(result['backup']).exists())
            self.assertEqual(tomllib.loads(self.config.read_text())['bus']['calibration_path'], '/keep/cal.toml')
            status = self.ctx.camera.status()
            self.assertTrue(status['pending_restart'])
            with patch.object(camera.subprocess, 'run') as run:
                code, result = self.request('/api/camera/apply', {'confirm_video_interrupt': True})
                self.assertEqual(code, 200)
                self.assertFalse(result['runtime_verified'])
                self.assertEqual(run.call_args.args[0], ['systemctl', 'restart', 'mediad'])
                self.assertEqual(run.call_count, 1)
            self.live.update(pid=124, rotation_degrees=270, rotation_source='configuration')
            self.runtime.write_text(json.dumps(self.live))
            self.assertFalse(self.ctx.camera.status()['pending_restart'])

    def test_stale_runtime_and_cli_override_block_save(self):
        with patch.object(camera, 'verify_runtime', return_value=(False, 'stale PID')):
            self.assertEqual(self.request('/api/camera/save', {'rotation': 90, 'confirm_save': True})[0], 400)
        self.live['rotation_source'] = 'command_line'
        self.runtime.write_text(json.dumps(self.live))
        with patch.object(camera, 'verify_runtime', return_value=(True, 'verified')):
            self.assertFalse(self.ctx.camera.status()['can_save'])
            self.assertEqual(self.request('/api/camera/save', {'rotation': 90, 'confirm_save': True})[0], 400)

    def test_apply_needs_explicit_opt_in_and_confirmation(self):
        with patch.object(camera, 'verify_runtime', return_value=(True, 'verified')), patch.object(camera.subprocess, 'run') as run:
            self.assertEqual(self.request('/api/camera/apply', {})[0], 400)
            self.ctx.camera.enable_apply = False
            self.assertEqual(self.request('/api/camera/apply', {'confirm_video_interrupt': True})[0], 400)
            self.ctx.camera.enable_apply = True; self.ctx.camera.demo = True
            self.assertEqual(self.request('/api/camera/apply', {'confirm_video_interrupt': True})[0], 400)
            run.assert_not_called()

    def test_failed_service_restart_is_not_reported_successful(self):
        with (patch.object(camera, 'verify_runtime', return_value=(True, 'verified')),
              patch.object(camera.subprocess, 'run', side_effect=subprocess.CalledProcessError(1, ['systemctl']))):
            self.assertEqual(self.request('/api/camera/apply', {'confirm_video_interrupt': True})[0], 500)

    def test_demo_and_missing_runtime_never_return_a_camera_frame(self):
        self.ctx.camera.demo = True
        self.assertEqual(self.request('/api/camera/frame', method='GET')[0], 503)


class RuntimeTests(unittest.TestCase):
    def test_real_verifier_checks_systemd_pid_executable_and_config(self):
        cfg = Path('/etc/robot/robotd.toml')
        data = {'source': 'mediad:camera:live', 'target_tag': 'daemon-v0.16.3',
                'pipeline_started': True, 'pid': 123, 'rotation_degrees': 90, 'config_path': str(cfg)}
        with (patch.object(camera.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '123\n', '')),
              patch.object(camera.os, 'readlink', return_value='/opt/robot/daemon/current/bin/mediad')):
            self.assertTrue(camera.verify_runtime(data, cfg, 'daemon-v0.16.3')[0])
            self.assertFalse(camera.verify_runtime({**data, 'pid': 124}, cfg, 'daemon-v0.16.3')[0])
            self.assertFalse(camera.verify_runtime(data, Path('/other/config'), 'daemon-v0.16.3')[0])


if __name__ == '__main__': unittest.main()
