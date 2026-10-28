#!/usr/bin/env python3
"""Create FAKE robot.state JSONL for offline smoke tests. Never apply simulated calibration to a robot."""
import argparse
import json
import math
from pathlib import Path

JOINTS=('left_hip_yaw','left_hip_roll','left_hip_pitch','left_knee','left_ankle',
        'neck_pitch','head_pitch','head_yaw','head_roll','mouth',
        'right_hip_yaw','right_hip_roll','right_hip_pitch','right_knee','right_ankle')

def make(path, g, joint_values):
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('w',encoding='utf-8') as f:
        for i in range(30):
            obj={'jsonrpc':'2.0','method':'robot.state',
                 'params':{'t':round(i*.1,3),'joints':joint_values,
                           'safety':{'gravity':g},'policy':'held'}}
            f.write(json.dumps(obj,separators=(',',':'))+'\n')


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--out',type=Path,required=True)
    a=ap.parse_args(); root=a.out
    t=math.radians(15);x=math.sin(t);z=-math.cos(t)
    dirs={'still':(0,0,-1),'fwd':(x,0,z),'back':(-x,0,z),
          'left':(0,x,z),'right':(0,-x,z)}
    for name,g in dirs.items():make(root/'imu'/f'{name}.jsonl',g,[0.]*15)
    for i,n in enumerate(JOINTS):
        a0=[0.]*15;a0[i]=.03
        a1=a0[:];a1[i]=.13 if i%2==0 else -.07
        make(root/'joints'/f'zero_{n}.jsonl',(0,0,-1),a0)
        make(root/'joints'/f'positive_{n}.jsonl',(0,0,-1),a1)
    print('SIMULATION FILES ONLY:',root,'; never install these generated values on hardware')

if __name__=='__main__':main()
