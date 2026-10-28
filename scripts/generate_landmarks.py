#!/usr/bin/env python3
"""Generate exact vector print masters and placement instructions from one map.

Install the optional print dependencies: reportlab, pypdf, opencv-contrib-python-headless.
The distributed subset font is used only by these fixed-text documents (SIL OFL).
No generated/painted approximation of ArUco bits is accepted.
"""
from __future__ import annotations
import argparse
import json
import math
from pathlib import Path

import cv2
import numpy as np
from PIL import Image
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from reportlab.platypus import Paragraph, Table, TableStyle

ROOT = Path(__file__).resolve().parents[1]
INK = colors.HexColor('#15352b')
MUTED = colors.HexColor('#5a6c65')
GREEN = colors.HexColor('#247b54')
PALE = colors.HexColor('#edf5ee')
RULE = colors.HexColor('#d5e1d8')


def write(c, x, y, value, size=10, color=colors.black, align='left'):
    c.setFillColor(color)
    c.setFont('GuideSans', size)
    fn = {'left': c.drawString, 'center': c.drawCentredString, 'right': c.drawRightString}[align]
    fn(x*mm, y*mm, str(value))


def paragraph(c, value, x, top, width=174, size=10.3, leading=16, color=INK):
    style = ParagraphStyle('body', fontName='GuideSans', fontSize=size, leading=leading,
                           textColor=color, wordWrap='CJK', spaceAfter=0)
    p = Paragraph(value, style)
    _, height = p.wrap(width*mm, 300*mm)
    p.drawOn(c, x*mm, top*mm-height)
    return top-height/mm


def arrow(c, x0, y0, x1, y1, color=GREEN, width=1.3):
    c.setStrokeColor(color); c.setFillColor(color); c.setLineWidth(width)
    c.line(x0*mm,y0*mm,x1*mm,y1*mm)
    a=math.atan2(y1-y0,x1-x0); length=2.8
    p=c.beginPath(); p.moveTo(x1*mm,y1*mm)
    for shift in (-.5,.5):
        p.lineTo((x1-length*math.cos(a+shift))*mm,(y1-length*math.sin(a+shift))*mm)
    p.close(); c.drawPath(p,stroke=0,fill=1)


def section(c, label, top):
    write(c,18,top,label,14,INK)
    return top-9


def header(c, number, title, subtitle):
    write(c,18,282,'MICRODUCK / ARUCO LAB',9,GREEN)
    write(c,192,282,'v0.2.0 · 2026-10-28',8.4,MUTED,'right')
    write(c,18,266,title,23,INK)
    paragraph(c,subtitle,18,256,size=10.3,leading=16,color=MUTED)
    c.setStrokeColor(RULE); c.line(18*mm,18*mm,192*mm,18*mm)
    write(c,18,11,'完整 15 舵机 · robotd 0.16.3 / 0.17.8 · 同一 Wi-Fi',8,MUTED)
    write(c,192,11,f'{number} / 5',8,MUTED,'right')


def table(c, rows, top, widths, size=9.3):
    style=ParagraphStyle('cell',fontName='GuideSans',fontSize=size,leading=14,wordWrap='CJK',textColor=INK)
    content=[[Paragraph(str(item),style) for item in row] for row in rows]
    t=Table(content,colWidths=[w*mm for w in widths],hAlign='LEFT')
    t.setStyle(TableStyle([
        ('BACKGROUND',(0,0),(-1,0),PALE),('VALIGN',(0,0),(-1,-1),'TOP'),
        ('TOPPADDING',(0,0),(-1,-1),7),('BOTTOMPADDING',(0,0),(-1,-1),7),
        ('LINEBELOW',(0,0),(-1,-1),.4,RULE),
        ('LEFTPADDING',(0,0),(-1,-1),7),('RIGHTPADDING',(0,0),(-1,-1),7),
    ]))
    _,h=t.wrap(174*mm,300*mm);t.drawOn(c,18*mm,top*mm-h)
    return top-h/mm


def make_assets(manifest, directory):
    directory.mkdir(parents=True,exist_ok=True)
    dictionary=cv2.aruco.getPredefinedDictionary(getattr(cv2.aruco,manifest['dictionary']))
    matrices={}
    for text_id in manifest['markers']:
        marker_id=int(text_id)
        bits=cv2.aruco.generateImageMarker(dictionary,marker_id,6,borderBits=1)
        matrices[text_id]=bits
        # 200 mm overall, 20 mm quiet zone on each side, 160 mm black square.
        raster=np.full((2400,2400),255,np.uint8)
        raster[240:2160,240:2160]=cv2.resize(bits,(1920,1920),interpolation=cv2.INTER_NEAREST)
        Image.fromarray(raster).save(directory/f'marker_{marker_id}_160mm.png',dpi=(304.8,304.8))
        rects=['<rect width="200" height="200" fill="white"/>']
        for row in range(6):
            for col in range(6):
                if bits[row,col]==0:
                    rects.append(f'<rect x="{20+col*160/6:.10f}" y="{20+row*160/6:.10f}" width="{160/6:.10f}" height="{160/6:.10f}" fill="black"/>')
        (directory/f'marker_{marker_id}_160mm.svg').write_text(
            '<svg xmlns="http://www.w3.org/2000/svg" width="200mm" height="200mm" viewBox="0 0 200 200">'
            +''.join(rects)+'</svg>\n',encoding='utf-8')
    return matrices


def print_pdf(manifest, matrices, destination):
    c=canvas.Canvas(str(destination),pagesize=A4,pageCompression=1)
    c.setTitle('Microduck ArUco 160mm A4 Print Masters + Camera Checkerboard')
    c.setAuthor('Microduck Harness')
    c.setViewerPreference('PrintScaling','None')
    for text_id,marker in manifest['markers'].items():
        idx=int(text_id); matrix=matrices[text_id]
        xyz=[marker['T_world_marker'][i][3] for i in range(3)]
        write(c,25,282,f'ArUco ID {idx}',20)
        write(c,185,283,'DICT_4X4_50',11,align='right')
        write(c,25,274,f'marker_{idx} · 黑方块 160 mm · 原尺寸 100% 打印',10)
        write(c,185,266,'TOP / 此边朝上',9,align='right')
        arrow(c,105,263,105,270,color=colors.black,width=1)
        # Black square x25..185, y80..240 mm. Quiet zone x5..205,y60..260.
        c.setFillColor(colors.black)
        for row in range(6):
            for col in range(6):
                if matrix[row,col]==0:
                    c.rect((25+col*160/6)*mm,(80+(5-row)*160/6)*mm,
                           (160/6)*mm,(160/6)*mm,stroke=0,fill=1)
        c.setStrokeColor(colors.black);c.setLineWidth(.55)
        c.line(25*mm,54*mm,185*mm,54*mm)
        for x in (25,185):c.line(x*mm,52*mm,x*mm,56*mm)
        write(c,105,47,'160.0 mm：测量最外层黑边，不含白边',9,align='center')
        c.setLineWidth(1.0);c.line(55*mm,32*mm,155*mm,32*mm)
        for x in (55,155):c.line(x*mm,29*mm,x*mm,35*mm)
        write(c,105,23,'校验线 100.0 mm · 禁止适应页面 / 镜像 / 多页合一',8.6,align='center')
        write(c,15,11,f'中心坐标 ({xyz[0]:+.2f}, {xyz[1]:+.2f}, {xyz[2]:.2f}) m · 正面朝场地中心',8)
        write(c,195,11,f'{idx+1} / 5',8,align='right')
        c.showPage()
    write(c,15,281,'相机标定棋盘',21)
    write(c,15,270,'9 列 × 6 行方格 · 内部角点 8 × 5 · 每格 20.0 mm',10.5)
    paragraph(c,'这一页只用于相机内参标定，正式运行时移出场地。请保持平整，不要缩放或镜像打印。',15,258,width=180,size=10)
    c.setFillColor(colors.black)
    for row in range(6):
        for col in range(9):
            if (row+col)%2==0:
                c.rect((15+col*20)*mm,(100+(5-row)*20)*mm,20*mm,20*mm,stroke=0,fill=1)
    c.setStrokeColor(colors.black);c.setLineWidth(.55)
    c.line(15*mm,88*mm,195*mm,88*mm)
    for x in (15,195):c.line(x*mm,86*mm,x*mm,90*mm)
    write(c,105,80,'图案总宽 180.0 mm / 总高 120.0 mm',9,align='center')
    paragraph(c,'从不同位置、距离和倾角拍摄完整棋盘。使用原始相机分辨率与像素方向，不裁切、不缩放；至少 12 张不同有效视图，建议采集 20 至 30 张。',18,63,size=10)
    c.setLineWidth(1);c.line(55*mm,29*mm,155*mm,29*mm)
    for x in (55,155):c.line(x*mm,26*mm,x*mm,32*mm)
    write(c,105,20,'100.0 mm 校验线 · 100% / Actual size',8.8,align='center')
    write(c,195,11,'5 / 5',8,align='right')
    c.save()


def guide_pdf(manifest,destination):
    c=canvas.Canvas(str(destination),pagesize=A4,pageCompression=1)
    c.setTitle('Microduck ArUco Placement and First Run Guide')
    c.setAuthor('Microduck Harness')
    header(c,1,'先把坐标放对','四张地标、一个地面原点。第一项任务先面对 ID 0，完成寻找与靠近，再逐步扩大活动范围。')
    # Top view: scale40mm/m, marker centres ±48mm, nominalzone ±40, inset±32.
    cx,cy,scale=105,177,40
    c.setFillColor(PALE);c.rect((cx-40)*mm,(cy-40)*mm,80*mm,80*mm,stroke=0,fill=1)
    c.setStrokeColor(RULE);c.setLineWidth(.8);c.rect((cx-40)*mm,(cy-40)*mm,80*mm,80*mm,stroke=1,fill=0)
    c.setDash(3,3);c.setStrokeColor(GREEN);c.rect((cx-32)*mm,(cy-32)*mm,64*mm,64*mm,stroke=1,fill=0);c.setDash()
    for idx,(dx,dy) in enumerate([(48,0),(0,48),(-48,0),(0,-48)]):
        x,y=cx+dx,cy+dy
        c.setStrokeColor(INK);c.setLineWidth(3)
        if dx:c.line(x*mm,(y-9)*mm,x*mm,(y+9)*mm)
        else:c.line((x-9)*mm,y*mm,(x+9)*mm,y*mm)
        arrow(c,x-dx*.04,y-dy*.04,x-dx*.22,y-dy*.22,INK,.9)
    write(c,169,178,'ID 0 / +X',10.5,INK,'center')
    write(c,105,236,'ID 1 / +Y',10.5,INK,'center')
    write(c,41,178,'ID 2 / -X',10.5,INK,'center')
    write(c,105,115,'ID 3 / -Y',10.5,INK,'center')
    arrow(c,cx,cy,cx+25,cy);arrow(c,cx,cy,cx,cy+25)
    c.setFillColor(INK);c.circle(cx*mm,cy*mm,1.7*mm,stroke=0,fill=1)
    write(c,cx-4,cy-8,'O (0,0)',8,INK,'right')
    write(c,cx+25,cy-6,'机器人起始朝向',8,MUTED,'center')
    write(c,105,101,'俯视示意；粗短线代表竖直面板，箭头表示正面朝向。',8.8,MUTED,'center')
    y=table(c,[['地标','中心坐标 x / y / z（m）','打印面朝向'],
        ['ID 0 / marker_0','(+1.20, 0, 0.20)','朝 -X'],
        ['ID 1 / marker_1','(0, +1.20, 0.20)','朝 -Y'],
        ['ID 2 / marker_2','(-1.20, 0, 0.20)','朝 +X'],
        ['ID 3 / marker_3','(0, -1.20, 0.20)','朝 +Y']],94,[42,87,45],8.8)
    paragraph(c,'地面坐标 +Z 朝上。浅色方框为 2 m × 2 m 工作区；虚线为扣除 0.20 m 边界余量后的机身参考点范围（x、y 均在 ±0.80 m 内）。',18,y-6,size=9,leading=13)
    c.showPage()

    header(c,2,'打印、量尺、立起来','使用配套打印文件的第 1 至 4 页。全部测量以最外层黑色正方形为准，不以 A4 纸边或内部编码区为准。')
    y=section(c,'01  打印设置',236)
    y=paragraph(c,'A4 纵向，单面，黑白，原尺寸 100% / Actual size。关闭“适应页面”“缩小过大页面”“无边距放大”“多页合一”和镜像。保持四张 ID 各打印一份，避免同一 ID 在多个位置重复出现。',18,y)
    y=table(c,[['必须复核的量','目标值','判定'],['黑色方块横边、竖边','160.0 mm','建议误差不超过 0.5 mm；超出则重印；横竖非等比不能用 size_m 补偿'],['每张底部校验线','100.0 mm','检验打印缩放；不要只相信驱动显示的 100%'],['黑边之外纯白区','每边至少 20 mm','不放文字、胶带、花纹或其他物体']],y-7,[60,35,79],9)
    y=section(c,'02  安装高度与朝向',y-12)
    y=paragraph(c,'贴在平整、哑光、刚性的竖直背板上，TOP 朝上，正面朝原点。黑色方块中心距地面 200 mm；保留整张 A4 时，纸底边应离地约 40 mm。不要把本布局的地标平铺在地上。',18,y)
    # Side mounting elevation.
    bx,by=133,39
    c.setStrokeColor(RULE);c.setFillColor(colors.white);c.rect(bx*mm,by*mm,36*mm,50.9*mm,fill=1,stroke=1)
    c.setFillColor(INK);c.rect((bx+4.3)*mm,(by+13.7)*mm,27.4*mm,27.4*mm,fill=1,stroke=0)
    c.setStrokeColor(MUTED);c.line(117*mm,32*mm,187*mm,32*mm)
    arrow(c,180,32,180,66.4,MUTED,.8)
    write(c,183,52,'200 mm',8,MUTED)
    c.setStrokeColor(GREEN);c.setDash(2,2);c.line(124*mm,66.4*mm,180*mm,66.4*mm);c.setDash()
    paragraph(c,'侧视高度示意<br/>标记中心 = 0.20 m<br/>纸底边 ≈ 0.04 m<br/><br/>先摆正再测位置；不要为了“让画面正”随意倾斜面板。',18,81,width=90,size=9.5)
    c.showPage()

    header(c,3,'让相机有真实尺度','ArUco 可以先识别 ID；可靠的距离与世界位姿还需要真实相机内参、畸变参数和相机到机身的变换。')
    y=section(c,'01  使用第 5 页标定棋盘',236)
    y=paragraph(c,'棋盘为 9 × 6 个方格，每格 20.0 mm，内部角点为 8 × 5。固定相机分辨率、镜头焦距与像素方向，保持棋盘平整。让棋盘出现在画面中央、四角和边缘，并改变距离及倾角。建议采集 20 至 30 张不同视图。',18,y)
    y=section(c,'02  只读采集与计算',y-11)
    y=paragraph(c,'工程 scripts/calibrate_camera.py 提供只读采集及离线标定。采集时每按一次 Enter 保存一张原始尺寸图，不驱动机器人；离线计算至少需要 12 张有效且不同的视图。输出 K、D、image_size、整体及逐图重投影误差，保存到 data/camera-calibration.json。',18,y)
    y=paragraph(c,'使用 --help 查看 capture 与 calibrate 的具体参数。命令不会自动覆盖你的视觉配置，也不会因为样本数量够了就宣称标定质量合格。',18,y-4,size=9.5,color=MUTED)
    y=section(c,'03  将实测参数填入视觉配置',y-11)
    y=table(c,[['配置字段','填入内容'],['dictionary','DICT_4X4_50'],['markers / size_m','0.16；若打印实际尺寸不同，应填实测米值'],['markers / T_world_marker','配套地图已提供四张竖直板的完整位姿'],['camera_matrix / distortion','本机标定得到的 K / D，不能照抄示例数值'],['image_size','与标定、运行完全相同的原始宽度与高度'],['use_robot_camera_fk','默认 true；使用 robotd 提供的动态相机 FK']],y,[63,111],9)
    paragraph(c,'0.16.3 与 0.17.8 都经过接口核对，但实际相机安装仍需核验。若镜头方向、安装件或模型外参不同，必须标定相应外参；仅内参正确还不足以得到正确机身位置。',18,y-8,size=9.5)
    c.showPage()

    header(c,4,'同一 Wi-Fi 的启动顺序','电脑运行 nanoMuse、Gateway、视觉和模型服务；ZERO 3W 运行原生 robotd 与执行器。无需让大模型进程直接访问电机总线。')
    y=table(c,[['服务','部署位置 / 默认端口','连接'],['robotd + executor','ZERO 3W / 8766','推荐 SSH 转发至电脑 localhost；也支持带 token 的局域网直连'],['Vision','电脑 / 8767','只取 ZERO 的相机、状态、ToF'],['Gateway + MCP','电脑 / 8765','nanoMuse 连接本机 /mcp'],['AgentJev / GLM','电脑或已有模型服务器','地址、模型 ID、认证可随时修改']],236,[43,55,76],9.1)
    y=section(c,'01  版本与连接',y-12)
    y=paragraph(c,'ZERO 设置 EXECUTOR_ROBOTD_VERSION=0.16.3 或 0.17.8。先保持 EXECUTOR_ALLOW_MOTION=0，仅检查实际版本、15 关节、相机与 ToF。同一 Wi-Fi 不保证设备互通；若连接失败，检查访客网络、AP 隔离、防火墙与 DHCP 地址变化。',18,y)
    y=section(c,'02  配置模型',y-11)
    y=paragraph(c,'为 Gateway 设置操作令牌后，在工作台的模型设置中填写 GLM Base URL、模型 ID、认证、超时与 JSON mode；AgentJev 地址和阈值也可调整。修改会保存，正在执行的任务保留原配置，下一项任务采用新配置。',18,y)
    y=section(c,'03  先找，再靠近',y-11)
    y=paragraph(c,'把机器人放在原点附近，正对 ID 0。先只读确认 marker_0、位姿、朝向和距离。原生策略按已有流程健康就绪后，保持执行器禁用运动，确认已可见的 marker_0；寻找任务仍会请求停止行走，并非只读接口。通过后再启用执行器运动，执行“靠近 marker_0 并停下”。示例到达距离为机身参考点到标记 0.55 m。',18,y)
    paragraph(c,'可直接使用 config/aruco-lab.example.json 配合 config/aruco-gateway.example.json。只填写真实相机参数和连接信息，不修改地标矩阵来掩盖摆放错误。完整命令见工程 docs/wifi-deployment.md。',18,y-7,size=9.4,color=MUTED)
    c.showPage()

    header(c,5,'确认坐标，再开始探索','四张板提供一个可测量的初始场地。其覆盖取决于视场角、朝向和遮挡，不能保证任意位置或转向中持续看到地标。')
    y=section(c,'01  只读位置核验',236)
    y=table(c,[['实际位置 / 动作','观测应当表现为'],['原点附近，面向 ID 0','x≈0，y≈0，yaw≈0；目标 bearing 接近0'],['沿 +X 移动约 0.20 m','x增加约0.20m，y基本不变'],['沿 +Y 移动约 0.20 m','y增加约0.20m，x基本不变'],['原地左转','yaw按正方向增加；目标方位随视角变化']],y,[79,95],9.3)
    y=paragraph(c,'以上是静态支撑条件下的测量核验，不要求直接手推已上电、正在驱动的机器人。位置偏差阈值应由实测确定；重投影误差小也不能证明地图高度、尺寸或世界朝向正确。',18,y-7,size=9.5)
    y=section(c,'02  常见现象与修正',y-12)
    y=table(c,[['现象','先检查'],['ID识别不到','字典/ID、白边、反光、模糊、遮挡、打印缩放'],['距离整体偏大或偏小','黑方块实测尺寸、相机内参、运行分辨率'],['x/y反向或机身朝向不对','TOP、面板正反、地图旋转、相机FK'],['转身或遮挡后任务停止','地标离开视野、定位过期；不要关闭时效检查'],['有图像但无法行走','实际版本、策略状态、新鲜ToF、头部朝向、运动是否启用']],y,[62,112],9)
    y=paragraph(c,'若要扩大自主探索，应增加可测量的新地标或接入连续定位；每张 ID 只对应一个固定位置。移动、旋转或改变任一面板高度后，需要同步更新地图位姿，并重新做只读核验。',18,y-8,size=9.5)
    paragraph(c,'依据：OpenCV ArUco Detection / Pose Estimation 文档；官方 Microduck 固定版本协议。数学约定与完整矩阵见 docs/aruco-placement.md。打印件经数字尺寸与识别检查，实际纸张仍必须量尺验证。',18,35,size=8.2,leading=12,color=MUTED)
    c.save()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--map',type=Path,default=ROOT/'config/aruco-landmarks.json')
    parser.add_argument('--font',type=Path,default=ROOT/'assets/fonts/MicroduckGuideSans.ttf')
    parser.add_argument('--out',type=Path,default=ROOT/'output/pdf')
    args=parser.parse_args()
    manifest=json.loads(args.map.read_text())
    assert manifest['print']['black_square_mm']==160 and manifest['print']['border_bits']==1
    pdfmetrics.registerFont(TTFont('GuideSans',str(args.font)))
    args.out.mkdir(parents=True,exist_ok=True)
    matrices=make_assets(manifest,ROOT/'assets/aruco')
    print_pdf(manifest,matrices,args.out/'microduck-aruco-print-pack.pdf')
    guide_pdf(manifest,args.out/'microduck-aruco-placement-guide.pdf')
    print(json.dumps({'print_pack':str(args.out/'microduck-aruco-print-pack.pdf'),
                      'guide':str(args.out/'microduck-aruco-placement-guide.pdf'),
                      'dictionary':manifest['dictionary'],'ids':list(manifest['markers']),
                      'black_square_mm':160,'pages':[5,5]},ensure_ascii=False))


if __name__=='__main__':
    main()
