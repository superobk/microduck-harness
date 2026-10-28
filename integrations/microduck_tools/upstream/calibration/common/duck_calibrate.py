#!/usr/bin/env python3
"""Microduck official-runtime calibration: READ-only capture, solve, validate and deploy TOML.

No DYNAMIXEL EEPROM writes, no motor torque/position writes, no policy commands.
Requires Python 3.11+ (tomllib), on board or with captured JSONL transferred to host.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import re
import selectors
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import tomllib
from pathlib import Path

JOINT_NAMES = (
    'left_hip_yaw', 'left_hip_roll', 'left_hip_pitch', 'left_knee', 'left_ankle',
    'neck_pitch', 'head_pitch', 'head_yaw', 'head_roll', 'mouth',
    'right_hip_yaw', 'right_hip_roll', 'right_hip_pitch', 'right_knee', 'right_ankle',
)
JOINT_IDS = (20, 21, 22, 23, 24, 30, 31, 32, 33, 34, 10, 11, 12, 13, 14)
IMU_STEPS = ('still', 'fwd', 'back', 'left', 'right')
TARGETS = ('daemon-v0.16.3', 'daemon-v0.17.8')
S = 2 ** -0.5
MOUNT_DEFAULT = {'zero3': (S, 0., S, 0.), 'beta': (1., 0., 0., 0.)}
MIN_SAMPLES = 15
MAX_ZERO_RAD = 0.35
DEG = math.pi / 180.0
# ST hardware sensitivity values: mdps/LSB from ST LSM6DSV16X datasheet.
# The custom 16.4 count/dps ratio is NOT a ST standard FS and must be
# confirmed from the actual imu_to_dxl MCU firmware/contract.
GYRO_PROFILES = {
    'st_125dps': 1.0 / 0.004375,
    'st_250dps': 1.0 / 0.00875,
    'st_500dps': 1.0 / 0.0175,
    'st_1000dps': 1.0 / 0.035,
    'st_2000dps': 1.0 / 0.070,
    'st_4000dps': 1.0 / 0.140,
    'custom_raw_div_16_4': 16.4,
}
DEFAULT_GYRO_PROFILE = 'st_500dps'
SUPPORTED_IMU_MODULE = 'bus_imu200'
CUSTOM_GYRO_PROFILES = {'custom_raw_div_16_4', 'manual'}

def gyro_scale_counts(profile, manual=None):
    if profile == 'manual':
        if not finite(manual) or not 5 <= manual <= 300:
            raise CalibrationError('manual gyro raw counts per degree/s must be 5..300')
        return float(manual)
    if profile not in GYRO_PROFILES:
        raise CalibrationError(f'unknown gyro profile {profile!r}')
    return GYRO_PROFILES[profile]


class CalibrationError(Exception):
    pass


def finite(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def vec(x, n, name):
    if not isinstance(x, (list, tuple)) or len(x) != n or any(not finite(y) for y in x):
        raise CalibrationError(f'{name}: expected {n} finite numeric values')
    return tuple(float(y) for y in x)


def dot(a, b):
    return sum(x*y for x, y in zip(a, b))


def norm(a):
    return math.sqrt(dot(a, a))


def scale(a, s):
    return tuple(x*s for x in a)


def add(a, b):
    return tuple(x+y for x, y in zip(a, b))


def sub(a, b):
    return tuple(x-y for x, y in zip(a, b))


def unit(a):
    n = norm(a)
    if n < 1e-8:
        raise CalibrationError('degenerate vector; tilt/axis cannot be resolved')
    return scale(a, 1/n)


def cross(a, b):
    return (a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0])


def angle(a, b):
    return math.degrees(math.acos(max(-1.0, min(1.0, dot(unit(a), unit(b))))))


def project_horizontal(v, up):
    return sub(v, scale(up, dot(v, up)))


def qnorm(q):
    q = unit(vec(q, 4, 'quaternion'))
    return q if q[0] >= 0 else tuple(-c for c in q)


def qmul(a, b):
    w,x,y,z = a
    W,X,Y,Z = b
    return (w*W-x*X-y*Y-z*Z,
            w*X+x*W+y*Z-z*Y,
            w*Y-x*Z+y*W+z*X,
            w*Z+x*Y-y*X+z*W)


def qfrom_matrix(r):
    """Right-handed proper rotation matrix, rows = new axes in old coordinates."""
    t = sum(r[i][i] for i in range(3))
    if t > 0:
        s = math.sqrt(t + 1)*2
        q = (0.25*s, (r[2][1]-r[1][2])/s, (r[0][2]-r[2][0])/s, (r[1][0]-r[0][1])/s)
    elif r[0][0] > r[1][1] and r[0][0] > r[2][2]:
        s = math.sqrt(1+r[0][0]-r[1][1]-r[2][2])*2
        q = ((r[2][1]-r[1][2])/s, .25*s, (r[0][1]+r[1][0])/s, (r[0][2]+r[2][0])/s)
    elif r[1][1] > r[2][2]:
        s = math.sqrt(1+r[1][1]-r[0][0]-r[2][2])*2
        q = ((r[0][2]-r[2][0])/s, (r[0][1]+r[1][0])/s, .25*s, (r[1][2]+r[2][1])/s)
    else:
        s = math.sqrt(1+r[2][2]-r[0][0]-r[1][1])*2
        q = ((r[1][0]-r[0][1])/s, (r[0][2]+r[2][0])/s, (r[1][2]+r[2][1])/s, .25*s)
    return qnorm(q)


def get_robot_frame(line):
    """Official robotctl monitor --json outputs JSON-RPC method robot.state."""
    try:
        o = json.loads(line)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(o, dict) or o.get('method') != 'robot.state':
        return None
    s = o.get('params')
    if not isinstance(s, dict):
        return None
    g = (s.get('safety') or {}).get('gravity')
    j = s.get('joints')
    if g is None or j is None:
        return None
    try:
        g = vec(g, 3, 'gravity'); j = vec(j, 15, 'joints')
    except CalibrationError:
        return None
    return {'gravity': g, 'joints': j, 't': s.get('t'), 'policy': s.get('policy')}


def read_jsonl(path):
    path = Path(path)
    if not path.is_file():
        raise CalibrationError(f'missing capture {path}')
    samples = [x for line in path.read_text(encoding='utf-8').splitlines()
               if (x := get_robot_frame(line)) is not None]
    if len(samples) < MIN_SAMPLES:
        raise CalibrationError(f'{path}: only {len(samples)} valid robot.state frames, need {MIN_SAMPLES}')
    timestamps = [s['t'] for s in samples if finite(s['t'])]
    if timestamps and len(set(timestamps)) < MIN_SAMPLES:
        raise CalibrationError(f'{path}: too few distinct robot timestamps; stale stream?')
    return samples


def mean_unit(vectors):
    return unit(tuple(statistics.mean(a[i] for a in vectors) for i in range(3)))


def steady_gravity(samples, label):
    good = []
    for s in samples:
        g = s['gravity']
        if 0.7 <= norm(g) <= 1.3:
            good.append(unit(g))
    if len(good) < MIN_SAMPLES:
        raise CalibrationError(f'{label}: fewer than {MIN_SAMPLES} valid gravity vectors')
    center = mean_unit(good)
    deviation = sorted(angle(g, center) for g in good)
    p90 = deviation[math.ceil(.9*len(deviation))-1]
    if p90 > 1.5:
        raise CalibrationError(f'{label}: gravity not steady (p90={p90:.2f}°, max 1.5°)')
    return center


def solve_imu(captures, old_mount, allow_stand_tilt=False, allow_large_mount=False):
    old_mount = qnorm(old_mount)
    means = {k: steady_gravity(read_jsonl(Path(captures)/'imu'/f'{k}.jsonl'), k)
             for k in IMU_STEPS}
    g0 = means['still']
    for k in IMU_STEPS[1:]:
        deg = angle(g0, means[k])
        if not (10.0 <= deg <= 35.0):
            raise CalibrationError(f'{k}: tilt {deg:.2f}° not in 10°–35°')
    up = scale(g0, -1.0)
    fore = project_horizontal(sub(means['fwd'], means['back']), up)
    left = project_horizontal(sub(means['left'], means['right']), up)
    x = unit(fore)
    left_meas = unit(left)
    orth = abs(90-angle(x, left_meas))
    if orth > 12.0:
        raise CalibrationError(f'front and left not orthogonal (error {orth:.2f}°, max 12°)')
    y = unit(cross(up, x))
    if dot(y, left_meas) <= 0 or angle(y, left_meas) > 12.0:
        raise CalibrationError('left/right samples reversed or not a right-handed body frame')
    x = unit(cross(y, up))
    # x/y/up are old-coordinate vectors for target body axes. Matrix converts OLD body to target body.
    qc = qfrom_matrix((x, y, up))
    stand_error = angle(g0, (0., 0., -1.))
    correction_deg = 2*math.degrees(math.acos(max(-1., min(1., abs(qc[0])))))
    if stand_error > 5.0 and not allow_stand_tilt:
        raise CalibrationError(f'standing tilt {stand_error:.1f}° > 5°: fix legs or use --accept-stand-tilt')
    if correction_deg > 30.0 and not allow_large_mount:
        raise CalibrationError(f'IMU mount correction {correction_deg:.1f}° > 30°: check module or use --accept-large-mount')
    mount = qnorm(qmul(qc, old_mount))
    return mount, {'orthogonality_error_deg': round(orth,3),
                   'stand_tilt_deg': round(stand_error,3),
                   'correction_deg': round(correction_deg,3),
                   'tilts_deg': {k: round(angle(g0,means[k]),2) for k in IMU_STEPS[1:]},
                   'samples': {k: len(read_jsonl(Path(captures)/'imu'/f'{k}.jsonl')) for k in IMU_STEPS}}


def fit_joints(captures, current_calibration=None):
    """Solve ABSOLUTE hardware-frame corrections even if robotd already uses a mapping."""
    previous_zero = (current_calibration or {}).get('joint_zero_rad', [0.]*15)
    previous_dir = (current_calibration or {}).get('joint_direction', [1]*15)
    zeros = []; directions = []; quality=[]
    for i, name in enumerate(JOINT_NAMES):
        zero_frames = read_jsonl(Path(captures)/'joints'/f'zero_{name}.jsonl')
        pos_frames = read_jsonl(Path(captures)/'joints'/f'positive_{name}.jsonl')
        # The state stream is robot coordinates. Undo the EXISTING mapping first.
        zero_v = [previous_zero[i] + previous_dir[i]*s['joints'][i] for s in zero_frames]
        pos_v = [previous_zero[i] + previous_dir[i]*s['joints'][i] for s in pos_frames]
        z = statistics.median(zero_v)
        p = statistics.median(pos_v)
        zm = max(abs(v-z) for v in zero_v)
        pm = max(abs(v-p) for v in pos_v)
        if max(zm,pm) > 1.5 * DEG:
            raise CalibrationError(f'{name}: servo was moving during capture (max drift >1.5°)')
        if abs(z) > MAX_ZERO_RAD:
            raise CalibrationError(f'{name}: mechanical zero offset {math.degrees(z):.1f}° exceeds 20°; reassemble the horn')
        delta = p-z
        if not 3*DEG <= abs(delta) <= 25*DEG:
            raise CalibrationError(f'{name}: positive-reference movement {math.degrees(delta):.1f}° must be 3°–25°')
        zeros.append(z)
        directions.append(1 if delta > 0 else -1)
        quality.append({'joint': name,'id':JOINT_IDS[i], 'zero_deg': round(math.degrees(z),2),
                        'positive_delta_deg': round(math.degrees(delta),2),
                        'direction': directions[-1]})
    return zeros, directions, quality


def check_calibration(cal, expected_tag=None):
    common = {'schema','target_tag','joint_zero_rad','joint_direction','imu_mount_wxyz'}
    extended = {'imu_module','gyro_profile','gyro_raw_counts_per_dps'}
    if not isinstance(cal, dict):
        raise CalibrationError('calibration must be a mapping')
    schema = cal.get('schema')
    if type(schema) is not int or schema not in (1, 2):
        raise CalibrationError('schema must be integer 1 (legacy) or 2 (gyro profiles)')
    required = common if schema == 1 else common | extended
    if set(cal) != required:
        raise CalibrationError(f'calibration schema {schema} requires keys {sorted(required)}, got {sorted(cal)}')
    if cal['target_tag'] not in TARGETS or expected_tag and cal['target_tag'] != expected_tag:
        raise CalibrationError(f'wrong target_tag: {cal["target_tag"]!r}')
    zs = vec(cal['joint_zero_rad'], 15, 'joint_zero_rad')
    ds = cal['joint_direction']
    if not isinstance(ds,(list,tuple)) or len(ds) != 15 or any(type(d) is not int or d not in (-1,1) for d in ds):
        raise CalibrationError('joint_direction must be fifteen +1/-1 integers')
    if any(abs(z) > MAX_ZERO_RAD for z in zs):
        raise CalibrationError('software joint correction too large (max 0.35rad)')
    q = vec(cal['imu_mount_wxyz'],4,'imu_mount_wxyz')
    if abs(dot(q,q)-1.) > 1e-4:
        raise CalibrationError('imu_mount_wxyz must have unit norm')
    if schema == 2:
        if cal['imu_module'] != SUPPORTED_IMU_MODULE:
            raise CalibrationError('only bus_imu200 raw gyro is supported in official robotd; I2C IMUs need dedicated decoder')
        profile=cal['gyro_profile']
        counts=cal['gyro_raw_counts_per_dps']
        if not isinstance(profile,str) or not finite(counts) or not 5 <= counts <= 300:
            raise CalibrationError('invalid gyro profile/counts (finite 5..300 required)')
        if profile != 'manual':
            expected=gyro_scale_counts(profile)
            if abs(float(counts)-expected) > 1e-5:
                raise CalibrationError(f'gyro_profile {profile}: expected raw_counts_per_dps={expected}, got {counts}')
    return cal


def effective_gyro(cal):
    check_calibration(cal)
    if cal['schema'] == 1:
        p,c=DEFAULT_GYRO_PROFILE,GYRO_PROFILES[DEFAULT_GYRO_PROFILE]
    else:
        p,c=cal['gyro_profile'],float(cal['gyro_raw_counts_per_dps'])
    return {'imu_module':SUPPORTED_IMU_MODULE,'gyro_profile':p,
            'gyro_raw_counts_per_dps':c,
            'gyro_deg_per_raw_count':1.0/c,
            'gyro_rad_per_raw_count':math.pi/(180.0*c)}


def render_toml(cal):
    check_calibration(cal)
    def floats(nums):
        return '[ '+', '.join(f'{x:.12f}' for x in nums)+' ]'
    txt=('# Generated by duck_calibrate.py; servo EEPROM and Homing Offset are untouched.\n'
         f'schema = {cal["schema"]}\n'
         f'target_tag = "{cal["target_tag"]}"\n'
         f'joint_zero_rad = {floats(cal["joint_zero_rad"])}\n'
         f'joint_direction = [ {", ".join(str(x) for x in cal["joint_direction"])} ]\n'
         f'imu_mount_wxyz = {floats(cal["imu_mount_wxyz"])}\n')
    if cal['schema'] == 2:
        txt+=(f'imu_module = "{cal["imu_module"]}"\n'
              f'gyro_profile = "{cal["gyro_profile"]}"\n'
              f'gyro_raw_counts_per_dps = {cal["gyro_raw_counts_per_dps"]:.12f}\n')
    return txt


def parse_calibration(path, expected_tag=None):
    try:
        cal = tomllib.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise CalibrationError(f'cannot read TOML {path}: {e}') from e
    return check_calibration(cal, expected_tag)


def replace_bus_key(config_text, key, value):
    """Conservatively change one simple [bus] key; preserve all other config lines/comments."""
    try:
        old = tomllib.loads(config_text) if config_text.strip() else {}
    except tomllib.TOMLDecodeError as e:
        raise CalibrationError(f'robotd config currently malformed: {e}') from e
    bus = old.get('bus',{})
    if not isinstance(bus,dict):
        raise CalibrationError('[bus] must be a table')
    if 'calibration_path' in bus and key != 'calibration_path':
        raise CalibrationError('conflicting existing calibration_path')
    lines = config_text.splitlines(keepends=True)
    start = next((i for i,l in enumerate(lines) if re.match(r'^\s*\[bus\]\s*(?:#.*)?$',l)), None)
    if start is None:
        # Do not overwrite a dotted [bus] representation with an unrelated table.
        if 'bus' in old:
            raise CalibrationError('nonstandard [bus] TOML layout; edit calibration_path manually')
        if value is not None:
            lines += [('\n' if lines and not lines[-1].endswith('\n') else ''),
                      '\n[bus]\n',f'{key} = {json.dumps(value)}\n']
    else:
        end = next((i for i in range(start+1,len(lines)) if re.match(r'^\s*\[',lines[i])),len(lines))
        matches = [i for i in range(start+1,end)
                   if re.match(r'^\s*'+re.escape(key)+r'\s*=',lines[i])]
        if len(matches)>1:
            raise CalibrationError('duplicate calibration_path assignments')
        if matches:
            if value is None:
                lines.pop(matches[0])
            else:
                lines[matches[0]] = f'{key} = {json.dumps(value)}\n'
        elif value is not None:
            lines.insert(end,f'{key} = {json.dumps(value)}\n')
    changed=''.join(lines)
    try:
        result = tomllib.loads(changed)
    except tomllib.TOMLDecodeError as e:
        raise CalibrationError(f'edited TOML invalid: {e}') from e
    if value is not None and result.get('bus',{}).get(key) != value:
        raise CalibrationError('calibration_path was not written correctly')
    if value is None and key in result.get('bus',{}):
        raise CalibrationError('calibration_path was not removed')
    return changed


def atomic_write(dst, data, mode):
    dst = Path(dst)
    if dst.is_symlink():
        raise CalibrationError(f'symlink destination forbidden: {dst}')
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(prefix='.microduck-cal-',dir=dst.parent,delete=False) as f:
            tmp = Path(f.name)
            f.write(data)
            f.flush(); os.fsync(f.fileno())
        os.chmod(tmp,mode)
        os.replace(tmp,dst)
        try:
            fd = os.open(dst.parent, os.O_RDONLY)
            try: os.fsync(fd)
            finally: os.close(fd)
        except OSError:
            pass
    finally:
        if tmp is not None and tmp.exists(): tmp.unlink()


def make_backup(path):
    path = Path(path)
    if not path.exists(): return None
    if path.is_symlink(): raise CalibrationError(f'symlink not supported: {path}')
    stamp=dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    backup=path.with_name(path.name+f'.backup-{stamp}-{time.time_ns()%1000000:06d}')
    shutil.copy2(path,backup)
    return backup


def cmd_record(args):
    if not args.confirm_supported:
        raise CalibrationError('recording physical robot requires --confirm-supported (robot securely supported and motion disabled)')
    if args.seconds < 1.5 or args.seconds > 30:
        raise CalibrationError('seconds must be 1.5–30')
    output=Path(args.out)
    if output.exists() and not args.overwrite:
        raise CalibrationError(f'{output} already exists; pass --overwrite to replace a capture')
    output.parent.mkdir(parents=True, exist_ok=True)
    command=[args.robotctl,'monitor','--hz',str(args.hz),'--json']
    print('READ ONLY:', ' '.join(command), file=sys.stderr)
    proc=subprocess.Popen(command,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True,bufsize=1)
    count=0
    sel=selectors.DefaultSelector()
    sel.register(proc.stdout,selectors.EVENT_READ)
    deadline=time.monotonic()+args.seconds
    try:
        with output.open('w',encoding='utf-8') as f:
            while time.monotonic()<deadline:
                if proc.poll() is not None: break
                for key,_ in sel.select(timeout=min(.5,max(0,deadline-time.monotonic()))):
                    line=key.fileobj.readline()
                    if not line: continue
                    if get_robot_frame(line):
                        f.write(line); count+=1
        if count < MIN_SAMPLES:
            output.unlink(missing_ok=True)
            raise CalibrationError(f'only {count} usable state frames; need {MIN_SAMPLES}')
    finally:
        sel.close()
        if proc.poll() is None:
            proc.terminate()
            try: proc.wait(timeout=3)
            except subprocess.TimeoutExpired: proc.kill(); proc.wait()
    print(f'captured {count} frames -> {output}')


def cmd_solve(args):
    previous = parse_calibration(args.current_calibration) if args.current_calibration else None
    old_mount = qnorm(previous['imu_mount_wxyz']) if previous else MOUNT_DEFAULT[args.board]
    if args.imu_mode == 'measured':
        q,iq=solve_imu(args.captures,old_mount,args.accept_stand_tilt,args.accept_large_mount)
    else:
        q,iq=old_mount,{'note':'IMU mount preserved; not recalibrated'}
    if args.joint_mode == 'measured':
        zeros,dirs,jq=fit_joints(args.captures, previous)
    elif args.joint_mode == 'keep':
        zeros = list(previous['joint_zero_rad']) if previous else [0.]*15
        dirs = list(previous['joint_direction']) if previous else [1]*15
        jq=[{'note':'existing calibration preserved' if previous else 'official identity mapping preserved'}]
    else:
        if not args.confirm_reset_joints:
            raise CalibrationError('resetting all joints requires --confirm-reset-joints')
        zeros,dirs,jq=[0.]*15,[1]*15,[{'note':'explicitly reset all software joint offsets to identity'}]
    if args.gyro_profile == 'keep':
        if previous:
            p=effective_gyro(previous)
            profile,counts=p['gyro_profile'],p['gyro_raw_counts_per_dps']
        else:
            profile,counts=DEFAULT_GYRO_PROFILE,GYRO_PROFILES[DEFAULT_GYRO_PROFILE]
    else:
        profile=args.gyro_profile
        counts=gyro_scale_counts(profile,args.gyro_raw_counts_per_dps)
    if profile != DEFAULT_GYRO_PROFILE and not args.confirm_firmware_scale:
        raise CalibrationError('non-factory gyro scaling requires --confirm-firmware-scale after verifying firmware output')
    cal={'schema':2,'target_tag':args.target,'joint_zero_rad':zeros,
         'joint_direction':dirs,'imu_mount_wxyz':q,
         'imu_module':SUPPORTED_IMU_MODULE,'gyro_profile':profile,
         'gyro_raw_counts_per_dps':counts}
    content=render_toml(cal)
    out=Path(args.out)
    if out.exists() and not args.overwrite:
        raise CalibrationError(f'{out} already exists; pass --overwrite to replace generated calibration')
    out.parent.mkdir(parents=True,exist_ok=True)
    out.write_text(content,encoding='utf-8')
    quality={'target_tag':args.target,'imu':iq,'joints':jq,'gyro':effective_gyro(cal),
             'note':'SENSOR HARDWARE FS is NOT modified; verify board firmware raw gyro scale before use'}
    Path(str(out)+'.quality.json').write_text(json.dumps(quality,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
    print(f'generated {out}; quality: {out}.quality.json')
    print('IMU mount:',[round(x,9) for x in q]); print('JOINT directions:',dirs)
    print('Raw gyro counts per degree/s:', counts, '=> radians per raw count:', math.pi/(180.0*counts))
    print('No servo EEPROM or motor motion commands were performed.')


def cmd_gyro(args):
    """Generate standalone v2 config for a new gyro profile without requiring IMU tilt capture."""
    previous = parse_calibration(args.current_calibration,args.target) if args.current_calibration else None
    profile=args.gyro_profile
    counts=gyro_scale_counts(profile,args.gyro_raw_counts_per_dps)
    if profile != DEFAULT_GYRO_PROFILE and not args.confirm_firmware_scale:
        raise CalibrationError('non-default gyro profile requires --confirm-firmware-scale after checking MCU raw output')
    base=previous or {'joint_zero_rad':[0.0]*15,
                      'joint_direction':[1]*15,
                      'imu_mount_wxyz':list(MOUNT_DEFAULT[args.board])}
    updated={'schema':2,'target_tag':args.target,
             'joint_zero_rad':list(base['joint_zero_rad']),
             'joint_direction':list(base['joint_direction']),
             'imu_mount_wxyz':list(base['imu_mount_wxyz']),
             'imu_module':SUPPORTED_IMU_MODULE,
             'gyro_profile':profile,'gyro_raw_counts_per_dps':counts}
    out=Path(args.out)
    if out.exists() and not args.overwrite:
        raise CalibrationError(f'{out} exists; use --overwrite')
    out.parent.mkdir(parents=True,exist_ok=True)
    atomic_write(out,render_toml(updated).encode(),0o644)
    print(f'generated {out} ({args.target}) profile {profile}: raw / {counts:.9f} °/s, then ×π/180 -> rad/s')
    print('No IMU FS registers, servo registers or robotd service state were modified.')


def cmd_check(args):
    cal=parse_calibration(args.calibration,args.target)
    print(json.dumps({'target':cal['target_tag'],
                      'max_zero_deg':round(max(abs(v) for v in cal['joint_zero_rad'])/DEG,3),
                      'reversed_joints':[JOINT_NAMES[i] for i,d in enumerate(cal['joint_direction']) if d==-1],
                      'imu_mount_wxyz':cal['imu_mount_wxyz'], 'gyro':effective_gyro(cal)},indent=2))


def cmd_install(args):
    if Path(args.generated).name.startswith('demo-'):
        raise CalibrationError('refusing to install bundled synthetic demo calibration')
    cal=parse_calibration(args.generated,args.target)
    profile=effective_gyro(cal)['gyro_profile']
    if args.apply and profile != DEFAULT_GYRO_PROFILE and not getattr(args,'confirm_firmware_scale',False):
        raise CalibrationError('cannot install custom gyro profile without --confirm-firmware-scale')
    config=Path(args.config)
    dest=Path(args.dest)
    if not config.is_file():
        raise CalibrationError(f'missing robotd config {config} (do not create a replacement config blindly)')
    if not dest.is_absolute():
        raise CalibrationError('--dest must be absolute')
    if dest.resolve()==config.resolve():
        raise CalibrationError('calibration and main config must be different files')
    if config.is_symlink() or dest.is_symlink():
        raise CalibrationError('symlink destinations not accepted')
    current=config.read_text(encoding='utf-8')
    modified=replace_bus_key(current,'calibration_path',str(dest))
    if not args.apply:
        print('DRY RUN: would create',dest,'and update [bus].calibration_path in',config)
        print('target:',cal['target_tag'],'; previous path:',(tomllib.loads(current).get('bus') or {}).get('calibration_path','(none)'))
        return
    old_c=make_backup(config)
    old_d=make_backup(dest)
    try:
        atomic_write(dest,render_toml(cal).encode('utf-8'),mode=0o644)
        atomic_write(config,modified.encode('utf-8'),mode=config.stat().st_mode & 0o777)
    except Exception:
        # Keep last-known-good main config if the final write did not succeed.
        if old_c:
            atomic_write(config,old_c.read_bytes(),mode=old_c.stat().st_mode & 0o777)
        if old_d:
            atomic_write(dest,old_d.read_bytes(),mode=old_d.stat().st_mode & 0o777)
        raise
    print('installed calibration:',dest)
    print('main config backed up:',old_c)
    print('previous calibration backed up:',old_d)
    print('NOT RESTARTED. Verify robot is limp/supported, then restart robotd deliberately.')


def cmd_remove(args):
    config=Path(args.config)
    current=config.read_text(encoding='utf-8')
    edited=replace_bus_key(current,'calibration_path',None)
    if not args.apply:
        print('DRY RUN: would remove bus.calibration_path from',config); return
    backup=make_backup(config)
    atomic_write(config,edited.encode('utf-8'),mode=config.stat().st_mode & 0o777)
    print('removed calibration pointer, backup:',backup)
    print('NOT RESTARTED. Restart only when robot is secure and relaxed.')


def parser():
    ap=argparse.ArgumentParser(description=__doc__)
    sub=ap.add_subparsers(dest='cmd',required=True)
    p=sub.add_parser('record',help='capture robotctl monitor --json frames, no motion commands')
    p.add_argument('--out',required=True)
    p.add_argument('--seconds',type=float,default=3.0)
    p.add_argument('--hz',type=int,default=10,choices=range(5,21),metavar='5..20')
    p.add_argument('--robotctl',default='robotctl')
    p.add_argument('--confirm-supported',action='store_true')
    p.add_argument('--overwrite',action='store_true')
    p.set_defaults(fn=cmd_record)
    p=sub.add_parser('solve',help='calculate IMU and software joint conversion, emit TOML')
    p.add_argument('--captures',required=True,type=Path)
    p.add_argument('--target',required=True,choices=TARGETS)
    p.add_argument('--out',required=True,type=Path)
    p.add_argument('--board',choices=('zero3','beta'),default='zero3')
    p.add_argument('--current-calibration',type=Path)
    p.add_argument('--joint-mode',choices=('keep','measured','reset'),default='keep')
    p.add_argument('--confirm-reset-joints',action='store_true')
    p.add_argument('--imu-mode',choices=('measured','keep'),default='measured')
    p.add_argument('--gyro-profile',choices=['keep',*GYRO_PROFILES,'manual'],default='keep')
    p.add_argument('--gyro-raw-counts-per-dps',type=float)
    p.add_argument('--confirm-firmware-scale',action='store_true')
    p.add_argument('--accept-stand-tilt',action='store_true')
    p.add_argument('--accept-large-mount',action='store_true')
    p.add_argument('--overwrite',action='store_true')
    p.set_defaults(fn=cmd_solve)
    p=sub.add_parser('gyro',help='generate config from verified MCU gyro profile; no capture needed')
    p.add_argument('--target',required=True,choices=TARGETS)
    p.add_argument('--gyro-profile',required=True,choices=[*GYRO_PROFILES,'manual'])
    p.add_argument('--gyro-raw-counts-per-dps',type=float)
    p.add_argument('--current-calibration',type=Path)
    p.add_argument('--board',choices=('zero3','beta'),default='zero3')
    p.add_argument('--out',required=True)
    p.add_argument('--confirm-firmware-scale',action='store_true')
    p.add_argument('--overwrite',action='store_true')
    p.set_defaults(fn=cmd_gyro)
    p=sub.add_parser('check',help='validate generated TOML against pinned schema')
    p.add_argument('--calibration',required=True)
    p.add_argument('--target',choices=TARGETS)
    p.set_defaults(fn=cmd_check)
    p=sub.add_parser('install',help='atomically install TOML and point robotd.toml to it')
    p.add_argument('--confirm-firmware-scale',action='store_true')
    p.add_argument('--generated',required=True)
    p.add_argument('--target',required=True,choices=TARGETS)
    p.add_argument('--config',default='/etc/robot/robotd.toml')
    p.add_argument('--dest',default='/etc/robot/robot-calibration.toml')
    p.add_argument('--apply',action='store_true',help='actually write after default dry-run')
    p.set_defaults(fn=cmd_install)
    p=sub.add_parser('remove',help='undo config pointer without changing physical servo')
    p.add_argument('--config',default='/etc/robot/robotd.toml')
    p.add_argument('--apply',action='store_true')
    p.set_defaults(fn=cmd_remove)
    return ap


def main():
    ap=parser(); a=ap.parse_args()
    try:
        a.fn(a)
    except (CalibrationError,OSError,ValueError,subprocess.SubprocessError) as e:
        ap.error(str(e))

if __name__=='__main__': main()
