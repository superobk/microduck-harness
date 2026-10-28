# 视觉与定位：先有可验证的观测，再让模型选动作

视觉服务运行在电脑。它提供两种可切换的输入路径：`external` 接收已有的 SLAM／定位／检测程序输出；`camera` 直接读取 ZERO 3W 执行器的相机、原生机器人状态和 ToF，运行 OpenCV ArUco 标记检测及已知地标定位。两种模式使用同一份观测契约，harness 不需要随视觉实现重写。

首版的实用真机目标是已标定区域中的 ArUco 地标。交付打印包使用 `marker:0` 到 `marker:3`，对应目标标签 `marker_0` 到 `marker_3`，先验证 `marker_0`。完整尺寸、四面放置、相机棋盘标定和静态检查见 [ArUco 打印与放置指南](aruco-placement.md)。普通 YOLO 检测框可以用于“寻找并报告”，但本项目没有把框的宽度或最小 ToF 距离当作物体的真实距离，因此不会用这类未知距离直接执行靠近。

## 启动

在项目虚拟环境中安装视觉依赖，再设置独立令牌：

```bash
pip install -e '.[vision]'
export VISION_TOKEN='替换成随机长字符串'
export VISION_CONFIG="$PWD/config/vision.local.json"
cp -n config/vision.example.json config/vision.local.json
python -m microduck_harness.vision --host 127.0.0.1 --port 8767
```

示例配置的默认模式为 `external`，相机内参、外参和地图没有填入伪造数值。需要使用相机路径时，完成后面的标定，再把 `mode` 改成 `camera`。`GET /health` 查看模式、最新画面年龄和错误；有令牌的 `GET /observe` 返回结构化数据，`GET /image` 返回同一帧的 JPEG。没有接到真实画面时 `/image` 返回 404。

采用本次四块打印地标时，从 `config/aruco-lab.example.json` 创建工作配置。该文件已经设置 `mode="camera"` 和 160 mm 标记的完整地图；仍需填入实测内参以及实际 ZERO 地址。保留已有工作配置，不要用模板覆盖你的标定结果。

## 相机路径

在电脑上把板端执行器映射到本地，或把配置中的三个 URL 改为实际可达地址。令牌只从 `executor_token_env` 指定的环境变量读取。

```json
{
  "mode": "camera",
  "camera_url": "http://127.0.0.1:8766/frame",
  "state_url": "http://127.0.0.1:8766/state",
  "tof_url": "http://127.0.0.1:8766/tof",
  "executor_token_env": "EXECUTOR_TOKEN"
}
```

执行器的 `/frame` 从 mediad 读取原始 UYVY。`X-Frame-Captured-At` 是原始采集时刻，`X-Frame-Age-S` 是板端计算的帧年龄。视觉服务取更保守的年龄并计入传输时间；重试请求不会使旧图像重新变新。相机与 FK 的配对使用两者共同的板端时间，输出观测的 TTL 则使用保守换算后的电脑时间，避免把两个时钟直接相减。两台机器仍应使用正常的时间同步，板端状态与相机的配对偏差默认必须在 250 ms 以内。网络中断或处理失败时，旧观测保留原始时间，Gateway 会因过期而停止下一段动作。

### 相机内参与像素方向

`camera_matrix` 使用 OpenCV 的 3×3 内参矩阵，`distortion` 填入标定得到的畸变参数，`image_size` 必须与实际推理图像完全一致。必须针对同一镜头、焦距、分辨率和像素方向标定。

打印包第 5 页提供 20 mm 方格、9×6 格（8×5 内角点）的标定棋盘。`scripts/calibrate_camera.py capture` 仅通过 `/frame` 手动逐张采集原生 PNG；`calibrate` 从至少 12 个不同的有效视角估计 K/D，生成独立 JSON 并报告逐图重投影 RMS。它不修改运行配置；审核后明确复制 `camera_matrix`、`distortion`、`image_size` 三项。完整命令及采集姿态见 [标定步骤](aruco-placement.md#5-用第-5-页棋盘获得真实相机内参)。

默认 `rotate_upright=false`：推理保持 mediad 原始像素方向，这与直接使用原生 OpenCV 相机坐标系的 FK 配对。`X-Frame-Rotate` 仍然保留，不会根据画面“看着像倒置”去猜相机外参。若希望旋转成正向像素，设置 `rotate_upright=true`，同时设置 `use_robot_camera_fk=false`，并提供旋转后坐标系对应的标定外参及固定头部位置。代码会拒绝把旋转后的像素直接配给未经相应变换的原生 FK。

### 相机到躯干

优先使用原生 `robot.state.frames.camera` 中的 `pos` 与 `quat`。它是由实际头部关节计算的相机到躯干变换，四元数顺序为 `[w,x,y,z]`；相机坐标 `+x` 向右、`+y` 向下、`+z` 沿光轴，躯干坐标 `+x` 向前、`+y` 向左、`+z` 向上。

已审计的 robotd **0.16.3 / API 41** 与 **0.17.8 / API 44** 均提供该字段，并通过 `/state.firmware` 显示实际握手身份。若定制装配需要固定头部外参回退，必须提供 `T_body_camera`（4×4，米）与 `fixed_head_rad`（`neck_pitch, head_pitch, head_yaw, head_roll`）。代码使用实际 `joints[5:9]` 比对固定头部位置，不使用仅表示期望值的 `head` 命令字段。偏差超过 `head_tolerance_rad` 或缺少测量时，定位无效。

### 已知地标

把标记固定在不会移动的位置，测量黑色外边框边长及其在工作区中的完整姿态。配置格式：

```json
{
  "dictionary": "DICT_4X4_50",
  "markers": {
    "7": {
      "label": "home",
      "size_m": 0.08,
      "T_world_marker": [[1,0,0,0],[0,1,0,0],[0,0,1,0],[0,0,0,1]]
    }
  }
}
```

这段单位变换仅演示 JSON 结构，表示把世界原点和标记中心坐标系重合。它不是你的场地标定值。实际地标坐标必须与 Gateway 工作区边界使用同一坐标系。OpenCV 标记坐标原点在中心，`x` 向右、`y` 向上、`z` 指向标记正面；不要只填一个平面 `x/y` 而遗漏墙面标记的旋转。

定位计算为：

```text
T_world_body = T_world_marker × inverse(T_camera_marker) × inverse(T_body_camera)
```

检测使用 `CORNER_REFINE_SUBPIX` 亚像素细化后再执行 PnP。代码检查重投影误差，并比较同帧多个地标给出的结果；出现明显分歧时不发布有效位姿。单个平面标记仍存在遮挡、反光与姿态歧义，尤其小像素标记近乎正面时，低重投影误差也不等于低真实位置误差。首版应在能持续看见可靠地标的平整小区域使用；它没有提供全场 SLAM、回环修正或无地标时的长期定位。离开可定位范围后会暂停执行，而不是把最后的位置持续当成当前位置。

### ToF 前向距离

使用原生 `tof.stream` 的状态码：5、9 且距离为正数时才有有效距离；255 表示测量范围内没有目标，其他状态表示不可用。为了不把“未测到”误判为空旷，首版要求中央 4×4 区域都有有效返回，且与 `frames.tof` 的时间差在 250 ms 内、传感器面向躯干前方。随后投影到躯干坐标，取保守的前向距离。

此门控保留地面返回，可能导致保守停下；它没有把未测到的区域填成最大量程，也不把最近的返回绑定为纸团距离。它不能检测所有低矮物、悬崖、透明表面或侧后方障碍，更不构成完整地图。需要大范围自由探索时，应通过 `external` 路径接入有完整运动区域覆盖和有效定位的感知系统。

### 自有 YOLO 权重

安装 `ultralytics`，把 `yolo_weights` 指向已经存在的本地文件，再设置 `yolo_device`。服务不会自动下载权重。类别名称来自你的权重；通用模型是否认识纸团必须实际验证。YOLO 返回的 `range_m` 为 `null`，可用于寻找目标，若要自动靠近，应由有标定深度或地面几何的外部视觉程序补足目标距离。

## 外部感知契约

`POST /observations` 需要 `Authorization: Bearer <VISION_TOKEN>`，可用于接入你已有的 SLAM、深度相机或 Isaac 仿真桥接程序。示例字段：

```json
{
  "frame_id": "sensor-frame-123",
  "captured_at": 1793130000.0,
  "source": "calibrated-perception",
  "pose": {"x": 0.1, "y": 0.2, "yaw": 0.0},
  "pose_valid": true,
  "clearance_m": 0.8,
  "detections": [
    {"id": "marker:7", "label": "home", "confidence": 0.99, "bearing_rad": 0.12, "range_m": 0.7}
  ],
  "notes": []
}
```

示例时间和数值仅说明格式。发送者必须使用对应原始测量的当前采集时间，不能在重复传输时写 `now()`。同一来源的重复 `frame_id`、倒序时间、明显未来时间、非有限数值都会拒绝。`bearing_rad` 为躯干坐标下左正右负；目标距离为躯干原点到目标的水平距离；`clearance_m` 是当前移动前方的保守可用空间，不是目标距离。未知字段应显式为 `null` 或 `pose_valid=false`。这个端点是受信任的传感器适配边界，不能将未经验证的大模型猜测作为测量发布。

## 已完成的验证与依据

自动测试实际生成四个方向的 ArUco 图像，通过 OpenCV 检测、PnP、世界变换恢复已知机器人位置；另以 16 个合成棋盘视角执行真实角点检测和内参标定，验证重复视角拒绝及原生 PNG 方向/尺寸保持。同时覆盖错误头部姿态、过期观测、未知 ToF 状态、重复帧、鉴权与模式互斥。这验证了软件几何与接口，不等于已完成你的相机标定或真机感知验证。

- [OpenCV ArUco 检测与位姿估计](https://docs.opencv.org/4.x/d5/dae/tutorial_aruco_detection.html)
- [OpenCV 相机标定](https://docs.opencv.org/4.x/dc/dbb/tutorial_py_calibration.html)
- [Microduck 0.16.3 / API 41 原生状态、相机与 ToF 协议](https://github.com/pollen-robotics/microduck/blob/daemon-v0.16.3/duck-ipc-proto/src/lib.rs)
- [Microduck 0.17.8 / API 44 原生状态、相机与 ToF 协议](https://github.com/pollen-robotics/microduck/blob/daemon-v0.17.8/duck-ipc-proto/src/lib.rs)
- [Microduck 0.16.3 ToF 状态码语义](https://github.com/pollen-robotics/microduck/blob/daemon-v0.16.3/tof/src/lib.rs) / [0.17.8](https://github.com/pollen-robotics/microduck/blob/daemon-v0.17.8/tof/src/lib.rs)
- [Microduck 0.16.3 ToF 坐标与光束几何](https://github.com/pollen-robotics/microduck/blob/daemon-v0.16.3/kinematics/src/tof.rs) / [0.17.8](https://github.com/pollen-robotics/microduck/blob/daemon-v0.17.8/kinematics/src/tof.rs)
- [双版本协议审计与软件测试范围](robotd.md#软件验证与源码依据)
