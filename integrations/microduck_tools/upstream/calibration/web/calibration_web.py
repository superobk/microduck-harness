#!/usr/bin/env python3
"""Local-only Microduck calibration dashboard, stdlib Python 3.11+.

No direct bus writes, torque commands, or daemon restarts. Configuration file writes
require CSRF, explicit confirmation, supported daemon and deliberate action.
Run over SSH port forwarding, NEVER bind directly to a public network interface.
"""
from __future__ import annotations
import argparse
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'common'))
import duck_calibrate as cal

PREVIEW_TEMPLATE=Path(__file__).with_name('index.html')
MAX_POST=48_000


def make_default(target):
    return {'schema':2,'target_tag':target,'joint_zero_rad':[0.0]*15,
        'joint_direction':[1]*15,
        'imu_mount_wxyz':list(cal.MOUNT_DEFAULT['zero3']),
        'imu_module':'bus_imu200','gyro_profile':'st_500dps',
        'gyro_raw_counts_per_dps':cal.GYRO_PROFILES['st_500dps']}


def state_delta(a,b):
    if a is None or b is None:
        return ['missing_saved_or_active']
    changes=[]
    for key in ('gyro_raw_counts_per_dps','imu_mount_wxyz','joint_zero_rad','joint_direction'):
        x,y=a.get(key),b.get(key)
        if isinstance(x,(tuple,list)) and isinstance(y,(tuple,list)):
            mismatch=(len(x)!=len(y) or any(abs(float(q)-float(z))>1e-8 for q,z in zip(x,y)))
        elif isinstance(x,(float,int)) and isinstance(y,(float,int)):
            mismatch=abs(x-y)>1e-7
        else:mismatch=x!=y
        if mismatch:changes.append(key)
    return changes


def read_robotd_pid():
    try:
        p=subprocess.run(['systemctl','show','robotd','-p','MainPID','--value'],
                    capture_output=True,text=True,timeout=1.5)
        if p.returncode==0:
            n=int(p.stdout.strip())
            return n if n>0 else None
    except (OSError,subprocess.SubprocessError,ValueError):pass
    return None


def valid_active_file(data, *, demo=False):
    if not isinstance(data,dict) or data.get('source')!='robotd:DynamixelIo:live' or data.get('bus_verified') is not True:
        return False,'invalid snapshot provenance'
    if demo:return False,'DEMO: offline example, never verified as live'
    try:
        pid=int(data['pid'])
        if pid<=1:return False,'invalid process ID'
        cmdline=Path(f'/proc/{pid}/cmdline').read_bytes().replace(b'\x00',b' ')
        if b'robotd' not in cmdline:return False,'reported daemon is no longer running'
        main=read_robotd_pid()
        if main is not None and main!=pid:return False,'runtime PID does not match active systemd robotd'
    except (ValueError,OSError,KeyError):return False,'runtime daemon process not verified'
    return True,'live daemon runtime snapshot verified'


class Monitor:
    """Read-only stream: actual gyro values already converted to rad/s by robotd."""
    def __init__(self,robotctl='robotctl',enable=True):
        self.last={}
        self.lock=threading.Lock()
        self.robotctl=robotctl
        self.enable=enable
        self.stop_event=threading.Event()
        self.proc=None
        if enable:
            threading.Thread(target=self._loop,name='cal-web-telemetry',daemon=True).start()

    def _loop(self):
        while not self.stop_event.is_set():
            proc=None
            try:
                proc=subprocess.Popen([self.robotctl,'monitor','--hz','5','--json'],
                    stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True,bufsize=1)
                self.proc=proc
                for raw in proc.stdout:
                    try:obj=json.loads(raw)
                    except json.JSONDecodeError:continue
                    if obj.get('method')!='robot.state':continue
                    s=obj.get('params') or {}
                    if not isinstance(s,dict):continue
                    imu=s.get('imu') or {}
                    gyro=imu.get('gyro') if isinstance(imu,dict) else None
                    if gyro is not None and (not isinstance(gyro,list) or len(gyro)!=3):gyro=None
                    sample={'time':time.time(),'policy':s.get('policy'),
                         'gravity':(s.get('safety') or {}).get('gravity'),
                         'joints':s.get('joints'), 'gyro_rad_s':gyro,
                         'loop_hz':(s.get('loop') or {}).get('hz'),
                         'bus_tick':s.get('t')}
                    with self.lock:self.last=sample
            except (OSError,ValueError):pass
            finally:
                if proc is not None and proc.poll() is None:
                    proc.terminate()
                    try:proc.wait(timeout=1)
                    except subprocess.TimeoutExpired:proc.kill()
            self.proc=None
            self.stop_event.wait(2)

    def close(self):
        self.stop_event.set()
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()

    def sample(self):
        with self.lock:out=dict(self.last)
        if out and time.time()-out.get('time',0)>4:
            out['stale']=True
        return out


class Context:
    def __init__(self,args):
        self.target=args.target
        self.config=Path(args.config)
        self.dest=Path(args.dest)
        self.runtime=Path(args.runtime)
        self.demo=args.demo
        self.allow_inactive=args.allow_inactive
        self.csrf=secrets.token_urlsafe(32)
        self.monitor=Monitor(args.robotctl,enable=not args.no_telemetry and not args.demo)
        self.lock=threading.RLock()
    def configured(self):
        try:
            data=tomllib.loads(self.config.read_text('utf-8'))
            ptr=(data.get('bus') or {}).get('calibration_path')
            if ptr is not None and not isinstance(ptr,str):raise ValueError('calibration_path must be a string')
            return ptr,None
        except (OSError,tomllib.TOMLDecodeError,ValueError) as e:return None,str(e)
    def saved(self):
        ptr,err=self.configured()
        path=Path(ptr) if ptr else self.dest
        if path.exists():
            try:return cal.parse_calibration(path,self.target),str(path),err,bool(ptr)
            except (OSError,cal.CalibrationError) as e:return None,str(path),str(e),bool(ptr)
        return None,str(path),err,bool(ptr)
    def runtime_read(self):
        try:
            obj=json.loads(self.runtime.read_text('utf-8'))
            ok,reason=valid_active_file(obj,demo=self.demo)
            if obj.get('target_tag')!=self.target:
                ok=False;reason='active daemon tag differs from dashboard target'
            return obj,ok,reason
        except (OSError,json.JSONDecodeError) as e:
            return None,False,f'no verified runtime snapshot: {e}'
    def status(self):
        saved,sp,serr,installed=self.saved()
        runtime,verified,reason=self.runtime_read()
        normalized=None
        if saved:
            normalized={**saved,**cal.effective_gyro(saved)}
        if runtime and verified:
            diffs=state_delta(normalized,runtime)
        else:diffs=[]
        telem=self.monitor.sample()
        if self.demo and not telem:
            telem={'stale':True,'policy':'SIMULATION','gravity':[0,0,-1],
                   'gyro_rad_s':[0,0,0],'joints':[0]*15,'loop_hz':50}
        return {'target':self.target,'configured_file':str(self.config),
                'install_dest':str(self.dest),'configured':installed,
                'saved_path':sp,'saved_error':serr,'saved':normalized,
                'runtime':runtime,'runtime_verified':verified,
                'runtime_reason':reason,'differences':diffs,
                'pending_restart':bool(diffs) if normalized and verified else None,
                'telemetry':telem,'can_save':verified or self.allow_inactive,
                'allow_inactive':self.allow_inactive,'demo':self.demo,
                'profiles':cal.GYRO_PROFILES,
                'joint_names':cal.JOINT_NAMES,'joint_ids':cal.JOINT_IDS}
    def validate_input(self,body):
        if not isinstance(body,dict) or not isinstance(body.get('calibration'),dict):
            raise cal.CalibrationError('missing calibration object')
        values=body['calibration']
        if values.get('target_tag')!=self.target:raise cal.CalibrationError('target_tag does not match selected version')
        cal.check_calibration(values,self.target)
        if values['schema'] !=2:
            raise cal.CalibrationError('web editor writes schema 2 only')
        return values
    def save(self,body):
        values=self.validate_input(body)
        if not (self.runtime_read()[1] or self.allow_inactive):
            raise cal.CalibrationError('no verified matching patched robotd; cannot write without --allow-inactive')
        g=cal.effective_gyro(values)
        if g['gyro_profile']!='st_500dps' and body.get('confirm_hardware') is not True:
            raise cal.CalibrationError('confirm firmware gyro raw value scale before saving custom profile')
        if body.get('confirm_safe') is not True:
            raise cal.CalibrationError('confirm robot is supported and will not be restarted automatically')
        # Use same atomic install + backup path as CLI, no subprocess to mutate config.
        with self.lock, tempfile.TemporaryDirectory(prefix='duck-cal-web-') as td:
            source=Path(td)/'generated.toml'
            source.write_text(cal.render_toml(values),'utf-8')
            a=argparse.Namespace(generated=str(source),target=self.target,
                 config=str(self.config),dest=str(self.dest),apply=True,
                 confirm_firmware_scale=body.get('confirm_hardware') is True)
            cal.cmd_install(a)
        return {'ok':True,'message':'Written and backed up; restart robotd safely outside this dashboard to apply.'}


def new_handler(ctx:Context):
    class Handler(BaseHTTPRequestHandler):
        server_version='DuckCalibration/2.0'
        def log_message(self,fmt,*args):
            sys.stderr.write('CAL-WEB '+fmt%args+'\n')
        def _host_ok(self):
            host=self.headers.get('Host','').split(':')[0].strip('[]').lower()
            return host in ('localhost','127.0.0.1','::1')
        def _reply(self,status,obj):
            data=json.dumps(obj,ensure_ascii=False,allow_nan=False).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type','application/json; charset=utf-8')
            self.send_header('Content-Length',str(len(data)))
            self.send_header('Cache-Control','no-store')
            self.send_header('X-Content-Type-Options','nosniff')
            self.send_header('Content-Security-Policy',"default-src 'none'; frame-ancestors 'none'")
            self.end_headers();self.wfile.write(data)
        def do_GET(self):
            if not self._host_ok():return self._reply(403,{'error':'Host not allowed; use localhost / SSH tunnel'})
            path=urlsplit(self.path).path
            if path=='/api/status':return self._reply(200,ctx.status())
            if path=='/api/export':
                saved,_,err,_=ctx.saved()
                if not saved:return self._reply(404,{'error':err or 'no saved calibration'})
                raw=cal.render_toml(saved).encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type','application/toml; charset=utf-8')
                self.send_header('Content-Disposition','attachment; filename="robot-calibration.toml"')
                self.send_header('Content-Length',str(len(raw)))
                self.send_header('Cache-Control','no-store')
                self.end_headers();self.wfile.write(raw);return
            if path in ('/','/index.html'):
                raw=PREVIEW_TEMPLATE.read_text(encoding='utf-8').replace('__CALIBRATION_CSRF__',ctx.csrf)
                data=raw.encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type','text/html; charset=utf-8')
                self.send_header('Content-Length',str(len(data)))
                self.send_header('Cache-Control','no-store')
                self.send_header('X-Frame-Options','DENY')
                self.send_header('X-Content-Type-Options','nosniff')
                self.send_header('Referrer-Policy','no-referrer')
                self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'")
                self.end_headers();self.wfile.write(data);return
            return self._reply(404,{'error':'not found'})
        def do_POST(self):
            if not self._host_ok():return self._reply(403,{'error':'invalid Host'})
            origin=self.headers.get('Origin')
            if origin:
                parsed=urlsplit(origin)
                if parsed.scheme!='http' or parsed.hostname not in ('localhost','127.0.0.1','::1'):
                    return self._reply(403,{'error':'Origin not allowed'})
            if self.headers.get('X-Calibration-CSRF')!=ctx.csrf:
                return self._reply(403,{'error':'invalid CSRF token'})
            if self.headers.get('Content-Type','').split(';')[0].strip()!='application/json':
                return self._reply(415,{'error':'application/json required'})
            try:
                n=int(self.headers.get('Content-Length','0'))
                if not 0<n<=MAX_POST:return self._reply(413,{'error':'invalid request length'})
                payload=json.loads(self.rfile.read(n))
                path=urlsplit(self.path).path
                if path=='/api/preview':
                    cfg=ctx.validate_input(payload)
                    return self._reply(200,{'ok':True,'toml':cal.render_toml(cfg),'conversion':cal.effective_gyro(cfg),
                                            'differences':state_delta({**cfg,**cal.effective_gyro(cfg)},
                                                                     ctx.runtime_read()[0])})
                if path=='/api/save':return self._reply(200,ctx.save(payload))
                return self._reply(404,{'error':'not found'})
            except (cal.CalibrationError,ValueError,TypeError,json.JSONDecodeError) as e:
                return self._reply(400,{'error':str(e)})
            except (OSError,subprocess.SubprocessError) as e:
                return self._reply(500,{'error':str(e)})
    return Handler


def parser():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--target',required=True,choices=cal.TARGETS)
    p.add_argument('--listen',default='127.0.0.1')
    p.add_argument('--port',type=int,default=8765)
    p.add_argument('--config',default='/etc/robot/robotd.toml')
    p.add_argument('--dest',default='/etc/robot/robot-calibration.toml')
    p.add_argument('--runtime',default='/run/robotd/calibration-effective.json')
    p.add_argument('--robotctl',default='robotctl')
    p.add_argument('--no-telemetry',action='store_true')
    p.add_argument('--allow-inactive',action='store_true',help='explicit dev-only file editing without verified live daemon')
    p.add_argument('--demo',action='store_true',help='visually show simulated telemetry, never claim real runtime active')
    return p


def main():
    args=parser().parse_args()
    if args.listen not in ('127.0.0.1','::1','localhost'):
        raise SystemExit('Refusing to bind public interface. Use --listen 127.0.0.1 and SSH -L.')
    if args.demo and not args.allow_inactive:
        print('DEMO displays offline visuals and will not write until --allow-inactive.',file=sys.stderr)
    ctx=Context(args)
    server=ThreadingHTTPServer((args.listen,args.port),new_handler(ctx))
    print(f'Microduck Calibration Web: http://127.0.0.1:{args.port}/',flush=True)
    print('No automatic robotd restart or motor write. To open remotely use an SSH -L port tunnel.',flush=True)
    try:server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:pass
    finally:
        ctx.monitor.close()
        server.server_close()


if __name__=='__main__':main()
