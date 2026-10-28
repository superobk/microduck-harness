"""Schema, gyro conversion, and local web security tests (no hardware)."""
import argparse
import http.client
import importlib.util
import json
import math
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
def import_module(name,path):
    spec=importlib.util.spec_from_file_location(name,path)
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
m=import_module('duck_calibrate_v2',ROOT/'common'/'duck_calibrate.py')
w=import_module('calibration_web_v2',ROOT/'web'/'calibration_web.py')


class GyroProfileTests(unittest.TestCase):
    def template(self,tag='daemon-v0.16.3',profile='custom_raw_div_16_4'):
        return {'schema':2,'target_tag':tag,'joint_zero_rad':[0.]*15,
               'joint_direction':[1]*15,'imu_mount_wxyz':[1.,0.,0.,0.],
               'imu_module':'bus_imu200','gyro_profile':profile,
               'gyro_raw_counts_per_dps':m.gyro_scale_counts(profile)}
    def test_degree_vs_radian_conversion(self):
        for tag in m.TARGETS:
            c=self.template(tag)
            s=m.effective_gyro(c)
            self.assertAlmostEqual(164/s['gyro_raw_counts_per_dps'],10,places=10)
            self.assertAlmostEqual(164*s['gyro_rad_per_raw_count'],math.radians(10),places=10)
            self.assertAlmostEqual(164*m.effective_gyro(self.template(tag,'st_500dps'))['gyro_deg_per_raw_count'],2.87)
    def test_roundtrip_new_schema_and_legacy(self):
        for tag in m.TARGETS:
            with tempfile.TemporaryDirectory() as td:
                c=self.template(tag)
                p=Path(td)/'cal.toml'
                p.write_text(m.render_toml(c))
                self.assertEqual(m.parse_calibration(p,tag)['gyro_raw_counts_per_dps'],16.4)
                old={k:v for k,v in c.items() if k not in ('imu_module','gyro_profile','gyro_raw_counts_per_dps')}
                old['schema']=1
                p.write_text(m.render_toml(old))
                self.assertAlmostEqual(m.effective_gyro(m.parse_calibration(p,tag))['gyro_raw_counts_per_dps'],1/0.0175)
    def test_bad_profile_ratio_and_source_rejected(self):
        c=self.template();c['gyro_raw_counts_per_dps']=14.2857
        with self.assertRaises(m.CalibrationError):m.check_calibration(c)
        c=self.template();c['imu_module']='i2c_bno085'
        with self.assertRaises(m.CalibrationError):m.check_calibration(c)
    def test_non_default_requires_confirm_on_apply(self):
        with tempfile.TemporaryDirectory() as td:
            d=Path(td);cfg=d/'robotd.toml';cfg.write_text('[bus]\nport="/dev/ttyS2"\n')
            g=d/'gen.toml';g.write_text(m.render_toml(self.template()))
            a=argparse.Namespace(generated=str(g),target='daemon-v0.16.3',config=str(cfg),dest=str(d/'cal.toml'),apply=True,confirm_firmware_scale=False)
            with self.assertRaises(m.CalibrationError):m.cmd_install(a)
            self.assertNotIn('calibration_path',cfg.read_text())


class WebTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.d=Path(self.temp.name)
        self.cfg=self.d/'robotd.toml'
        self.cfg.write_text('[board]\nversion = "zero3"\n[bus]\nport = "/dev/ttyS2"\n')
        self.tag='daemon-v0.16.3'
        self.dest=self.d/'cal.toml'
        self.run=self.d/'cal-runtime.json'
        self.args=argparse.Namespace(target=self.tag,config=str(self.cfg),dest=str(self.dest),runtime=str(self.run),robotctl='robotctl',no_telemetry=True,demo=False,allow_inactive=False)
        self.ctx=w.Context(self.args)
        self.app=w.ThreadingHTTPServer(('127.0.0.1',0),w.new_handler(self.ctx))
        self.worker=threading.Thread(target=self.app.serve_forever,daemon=True)
        self.worker.start()
    def tearDown(self):
        self.app.shutdown();self.app.server_close();self.worker.join(timeout=3)
        self.temp.cleanup()
    def req(self,method,path,body=None,csrf=None,host='127.0.0.1'):
        conn=http.client.HTTPConnection('127.0.0.1',self.app.server_address[1],timeout=4)
        headers={'Host':host,'Content-Type':'application/json'}
        if csrf:headers['X-Calibration-CSRF']=csrf
        conn.request(method,path,json.dumps(body) if body is not None else None,headers)
        res=conn.getresponse();raw=res.read();conn.close()
        return res.status,json.loads(raw) if path!='/index.html' and path!='/' else raw.decode()
    def template(self):return w.make_default(self.tag)
    def test_unauthenticated_write_cannot_change_config(self):
        p={'calibration':self.template(),'confirm_safe':True,'confirm_hardware':True}
        code,_=self.req('POST','/api/save',p)
        self.assertEqual(code,403)
        self.assertNotIn('calibration_path',self.cfg.read_text())
        code,_=self.req('GET','/api/status',host='evil.test')
        self.assertEqual(code,403)
    def test_runtime_snapshot_only_trusted_if_matching_process(self):
        active={**self.template(),'source':'robotd:DynamixelIo:live',
                'bus_verified':True,'pid':123456,'gyro_rad_per_raw_count':math.pi/3150}
        self.run.write_text(json.dumps(active))
        s=self.ctx.status()
        self.assertFalse(s['runtime_verified'])
        with patch.object(w,'valid_active_file',return_value=(True,'mocked matched PID')):
            s=self.ctx.status();self.assertTrue(s['runtime_verified'])
    def test_save_requires_active_version_and_has_backup(self):
        obj=self.template();obj['gyro_profile']='custom_raw_div_16_4';obj['gyro_raw_counts_per_dps']=16.4
        payload={'calibration':obj,'confirm_safe':True,'confirm_hardware':True}
        code,res=self.req('POST','/api/save',payload,self.ctx.csrf)
        self.assertEqual(code,400)
        self.assertIn('no verified',res['error'])
        with patch.object(w,'valid_active_file',return_value=(True,'test')):
            runtime={**obj,'source':'robotd:DynamixelIo:live','bus_verified':True,'pid':2}
            runtime['gyro_raw_counts_per_dps']=1/0.0175
            self.run.write_text(json.dumps(runtime))
            code,_=self.req('POST','/api/save',payload,self.ctx.csrf)
            self.assertEqual(code,200)
            self.assertTrue(self.dest.exists())
            code,s=self.req('GET','/api/status')
            self.assertTrue(s['pending_restart'])
            self.assertIn('gyro_raw_counts_per_dps',s['differences'])
    def test_default_preview_no_motor_commands(self):
        code,res=self.req('POST','/api/preview',{'calibration':self.template()},self.ctx.csrf)
        self.assertEqual(code,200)
        self.assertIn('gyro_profile = "st_500dps"',res['toml'])
        self.assertFalse(self.dest.exists())

if __name__=='__main__':unittest.main()
