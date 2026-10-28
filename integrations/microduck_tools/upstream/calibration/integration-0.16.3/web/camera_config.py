"""Camera-only configuration. All service mutations are confined to mediad."""
import json
import os
from pathlib import Path
import re
import subprocess
import tomllib
import urllib.request

import duck_calibrate as cal

ANGLES = (0, 90, 180, 270)


def angle(value):
    if value is not None and (type(value) is not int or value not in ANGLES):
        raise cal.CalibrationError('camera_rotate must be null, 0, 90, 180 or 270')
    return value


def edit(text, value):
    """Only change a simple [media].camera_rotate; reject ambiguous/dotted layouts."""
    angle(value)
    original = tomllib.loads(text)
    lines = text.splitlines(keepends=True)
    start = next((n for n, line in enumerate(lines)
                  if re.match(r'^\s*\[media\]\s*(?:#.*)?$', line)), None)
    if start is None:
        if 'media' in original:
            raise cal.CalibrationError('nonstandard [media] TOML layout; review it manually')
        if value is not None:
            lines += ['\n[media]\n', f'camera_rotate = {value}\n']
    else:
        end = next((n for n in range(start + 1, len(lines))
                    if re.match(r'^\s*\[', lines[n])), len(lines))
        matches = [n for n in range(start + 1, end)
                   if re.match(r'^\s*camera_rotate\s*=', lines[n])]
        if matches:
            n = matches[0]
            if value is None:
                lines.pop(n)
            else:
                lines[n] = f'camera_rotate = {value}\n'
        elif value is not None:
            if end > 0 and not lines[end - 1].endswith('\n'):
                lines[end - 1] += '\n'
            lines.insert(end, f'camera_rotate = {value}\n')
    result = ''.join(lines)
    parsed = tomllib.loads(result)
    expected = dict(original)
    media = dict(original.get('media', {}))
    if value is None:
        media.pop('camera_rotate', None)
    else:
        media['camera_rotate'] = value
    if media or 'media' in original:
        expected['media'] = media
    if parsed != expected:
        raise cal.CalibrationError('camera edit would change another config field')
    return result


def verify_runtime(data, config, target):
    if not isinstance(data, dict) or data.get('source') != 'mediad:camera:live':
        return False, 'no camera runtime receipt from patched mediad'
    if data.get('target_tag') != target or data.get('pipeline_started') is not True:
        return False, 'camera runtime target or pipeline does not match'
    try:
        angle(data['rotation_degrees'])
        if data['rotation_degrees'] is None:
            raise ValueError('missing runtime angle')
        pid = data['pid']
        if type(pid) is not int or pid <= 1:
            raise ValueError('invalid mediad PID')
        result = subprocess.run(['systemctl', 'show', 'mediad', '-p', 'MainPID', '--value'],
                                capture_output=True, text=True, timeout=2, check=True)
        if int(result.stdout.strip()) != pid:
            raise ValueError('camera PID differs from systemd mediad')
        if Path(os.readlink(f'/proc/{pid}/exe')).name != 'mediad':
            raise ValueError('camera process is not mediad')
        if Path(data['config_path']).resolve() != config.resolve():
            raise ValueError('mediad uses a different config file')
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError,
            cal.CalibrationError) as exc:
        return False, str(exc)
    return True, 'running mediad PID and camera settings verified'


class Camera:
    def __init__(self, config, runtime, target, demo, enable_apply, web_port):
        self.config = Path(config)
        self.runtime = Path(runtime)
        self.target = target
        self.demo = demo
        self.enable_apply = enable_apply
        self.web_port = web_port

    def status(self):
        saved = None
        expected = None
        error = None
        try:
            config = tomllib.loads(self.config.read_text('utf-8'))
            saved = angle(config.get('media', {}).get('camera_rotate'))
            board = config.get('board', {}).get('version', 'zero3')
            if board not in ('zero3', 'beta'):
                raise cal.CalibrationError('unknown board default; select an explicit angle after review')
            expected = saved if saved is not None else (90 if board == 'zero3' else 0)
        except (OSError, ValueError, AttributeError, cal.CalibrationError) as exc:
            error = str(exc)
        runtime = None
        verified = False
        reason = 'DEMO: no real camera connected'
        if not self.demo:
            try:
                runtime = json.loads(self.runtime.read_text('utf-8'))
                verified, reason = verify_runtime(runtime, self.config, self.target)
            except (OSError, ValueError) as exc:
                reason = str(exc)
        override = bool(verified and (runtime.get('rotation_source') == 'command_line'
                                     or runtime.get('flip_in_pipeline') is True))
        writable = verified and not override and error is None and self.target == 'daemon-v0.16.3'
        return {'saved_rotation': saved, 'expected_rotation': expected, 'saved_error': error,
                'runtime': runtime if verified else None, 'runtime_verified': verified,
                'runtime_reason': reason, 'override': override, 'can_save': writable,
                'can_apply': writable and self.enable_apply and not self.demo,
                'apply_enabled': self.enable_apply,
                'pending_restart': expected != runtime['rotation_degrees'] if verified and error is None else None}

    def preview(self, body):
        if not isinstance(body, dict) or 'rotation' not in body:
            raise cal.CalibrationError('missing camera rotation')
        if self.target != 'daemon-v0.16.3':
            raise cal.CalibrationError('camera extension is validated for daemon-v0.16.3 only')
        value = angle(body['rotation'])
        text = self.config.read_text('utf-8')
        updated = edit(text, value)
        snippet = '# follows board camera default' if value is None else f'[media]\ncamera_rotate = {value}\n'
        return {'ok': True, 'rotation': value, 'snippet': snippet, 'config': updated}

    def save(self, body):
        if body.get('confirm_save') is not True:
            raise cal.CalibrationError('confirm camera configuration save')
        state = self.status()
        if not state['can_save']:
            raise cal.CalibrationError('camera save requires verified patched mediad without CLI rotation overrides')
        if self.config.is_symlink():
            raise cal.CalibrationError('symlink config destinations are not supported')
        preview = self.preview(body)
        if preview['config'] == self.config.read_text('utf-8'):
            return {'ok': True, 'changed': False, 'message': 'camera configuration already saved; no restart'}
        backup = cal.make_backup(self.config)
        cal.atomic_write(self.config, preview['config'].encode('utf-8'), self.config.stat().st_mode & 0o777)
        return {'ok': True, 'changed': True, 'backup': str(backup),
                'message': 'camera configuration saved; mediad has not been restarted'}

    def apply(self, body):
        if body.get('confirm_video_interrupt') is not True:
            raise cal.CalibrationError('confirm video and media sessions will briefly disconnect')
        if not self.status()['can_apply']:
            raise cal.CalibrationError('camera apply disabled or live camera runtime not verified')
        subprocess.run(['systemctl', 'restart', 'mediad'], check=True, timeout=20,
                       capture_output=True, text=True)
        return {'ok': True, 'restart_requested': True, 'runtime_verified': False,
                'message': 'mediad restarted; wait for a fresh runtime receipt and verify the picture'}

    def frame(self):
        if not self.status()['runtime_verified']:
            raise cal.CalibrationError('no verified live camera; no preview available')
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        url = f'http://127.0.0.1:{self.web_port}/frame'
        with opener.open(url, timeout=4) as response:
            raw = response.read(12 * 1024 * 1024 + 1)
            if len(raw) > 12 * 1024 * 1024 or not raw.startswith(b'\x89PNG\r\n\x1a\n'):
                raise cal.CalibrationError('camera preview is not a bounded PNG')
        return raw
