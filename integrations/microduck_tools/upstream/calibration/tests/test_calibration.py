"""Offline tests: no servo writes. Run python3 -m unittest discover -s tests -v."""
import importlib.util
import json
import math
import tempfile
import unittest
from pathlib import Path

BASE=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('duck_calibrate',BASE/'common'/'duck_calibrate.py')
m=importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def frame(g,j=None,**kw):
    if j is None:j=[0.]*15
    return json.dumps({'jsonrpc':'2.0','method':'robot.state',
       'params': {'safety':{'gravity':list(g)},'joints':list(j),'policy':'held','t':kw.get('t',1.2)}})+'\n'


def write_log(path, g, j=None,n=35):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(''.join(frame(g,j,t=i*.1) for i in range(n)),encoding='utf-8')


class TestSolver(unittest.TestCase):
    def setUp(self):
        self.t=tempfile.TemporaryDirectory()
        self.root=Path(self.t.name)
    def tearDown(self): self.t.cleanup()
    def imu_data(self, mapper=lambda g:g):
        a=math.radians(15)
        s,c=math.sin(a),math.cos(a)
        poses={'still':(0,0,-1),'fwd':(s,0,-c),'back':(-s,0,-c),
               'left':(0,s,-c),'right':(0,-s,-c)}
        for k,g in poses.items():write_log(self.root/'imu'/f'{k}.jsonl',mapper(g))

    def test_imu_identity(self):
        self.imu_data()
        q,quality=m.solve_imu(self.root,m.MOUNT_DEFAULT['zero3'])
        for a,b in zip(q,m.MOUNT_DEFAULT['zero3']):self.assertAlmostEqual(a,b,places=9)
        self.assertAlmostEqual(quality['stand_tilt_deg'],0,places=6)

    def test_imu_corrects_yaw_90(self):
        # Input is old body frame rotated -90°; solve old->true, as in physical install correction.
        self.imu_data(lambda g:(g[1],-g[0],g[2]))
        q,_=m.solve_imu(self.root,(1,0,0,0),allow_large_mount=True)
        expected=(2**-0.5,0,0,2**-0.5)
        for a,b in zip(q,expected):self.assertAlmostEqual(a,b,places=7)

    def test_imu_tilt_swaps_rejected(self):
        self.imu_data()
        bad=(0,math.sin(math.radians(15)),-math.cos(math.radians(15)))
        write_log(self.root/'imu'/'left.jsonl',m.scale(bad,-1))
        with self.assertRaises(m.CalibrationError):m.solve_imu(self.root,(1,0,0,0))

    def test_imu_frozen_capture_rejected(self):
        for k in m.IMU_STEPS:write_log(self.root/'imu'/f'{k}.jsonl',(0,0,-1))
        with self.assertRaises(m.CalibrationError):m.solve_imu(self.root,(1,0,0,0))

    def test_joint_zero_and_direction(self):
        for i,name in enumerate(m.JOINT_NAMES):
            z=[0.]*15;z[i]=0.04
            p=[0.]*15;p[i]=0.14 if i%2==0 else -0.06
            write_log(self.root/'joints'/f'zero_{name}.jsonl',(0,0,-1),z)
            write_log(self.root/'joints'/f'positive_{name}.jsonl',(0,0,-1),p)
        z,d,_=m.fit_joints(self.root)
        self.assertEqual(d,[1 if i%2==0 else -1 for i in range(15)])
        for x in z:self.assertAlmostEqual(x,.04)
        # Existing calibration must be inverted when captures are already corrected by robotd.
        old={'joint_zero_rad':[.02]*15,'joint_direction':[-1]*15}
        zz,dd,_=m.fit_joints(self.root,old)
        for x in zz:self.assertAlmostEqual(x,-.02)
        self.assertEqual(dd,[-x for x in d])

    def test_joint_excess_offset_rejected(self):
        for name in m.JOINT_NAMES:
            j=[.4]*15; pos=[.5]*15
            write_log(self.root/'joints'/f'zero_{name}.jsonl',(0,0,-1),j)
            write_log(self.root/'joints'/f'positive_{name}.jsonl',(0,0,-1),pos)
        with self.assertRaises(m.CalibrationError):m.fit_joints(self.root)

    def test_render_parse_and_atomic_install_remove(self):
        cal={'schema':1,'target_tag':'daemon-v0.17.8','joint_zero_rad':[.01]*15,
             'joint_direction':[1]*15,'imu_mount_wxyz':m.MOUNT_DEFAULT['zero3']}
        gen=self.root/'gen.toml';gen.write_text(m.render_toml(cal))
        self.assertEqual(m.parse_calibration(gen)['joint_zero_rad'][0],.01)
        cfg=self.root/'robotd.toml'; cfg.write_text('[board]\nversion = "zero3"\n[bus]\nport = "/dev/ttyS2"\nfast_sync_read = true\n[control]\nhz = 50\n')
        dest=self.root/'robot-calibration.toml'
        import argparse
        args=argparse.Namespace(generated=str(gen),target='daemon-v0.17.8',config=str(cfg),dest=str(dest),apply=False)
        m.cmd_install(args)
        self.assertNotIn('calibration_path',cfg.read_text())
        args.apply=True;m.cmd_install(args)
        self.assertIn('calibration_path',cfg.read_text())
        self.assertTrue(dest.exists())
        m.cmd_remove(argparse.Namespace(config=str(cfg),apply=True))
        self.assertNotIn('calibration_path',cfg.read_text())
        self.assertEqual(m.parse_calibration(dest)['target_tag'],'daemon-v0.17.8')

    def test_bad_quaternion_and_bad_target(self):
        cal={'schema':1,'target_tag':'daemon-v0.17.8','joint_zero_rad':[0.]*15,
             'joint_direction':[1]*15,'imu_mount_wxyz':[3.,0.,0.,0.]}
        with self.assertRaises(m.CalibrationError):m.render_toml(cal)
        cal['imu_mount_wxyz']=[1.,0.,0.,0.]
        with self.assertRaises(m.CalibrationError):m.check_calibration(cal,'daemon-v0.16.3')

    def test_jsonrpc_filter(self):
        self.assertIsNone(m.get_robot_frame(json.dumps({'method':'robot.health','params':{}})))
        self.assertEqual(len(m.get_robot_frame(frame((0,0,-1)))['joints']),15)

    def test_bus_config_editor_retains_other_sections(self):
        cfg='[board]\nversion="zero3"\n\n[bus]\nport="/dev/ttyS2"\n\n[policy]\nenabled=false\n'
        s=m.replace_bus_key(cfg,'calibration_path','/etc/robot/robot-calibration.toml')
        self.assertEqual(m.tomllib.loads(s)['bus']['calibration_path'],'/etc/robot/robot-calibration.toml')
        s2=m.replace_bus_key(s,'calibration_path',None)
        self.assertNotIn('calibration_path',s2)

if __name__=='__main__':unittest.main()
