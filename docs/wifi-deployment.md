# 同一 Wi-Fi：电脑与 ZERO 3W 的首轮部署

适用于完整 15 舵机 Microduck，原生 robotd **0.16.3 / API 41** 或 **0.17.8 / API 44**。电脑运行视觉、Gateway、nanoMuse；ZERO 只运行已有 robotd 与本工程执行器。AgentJev 通常运行在电脑的独立 GPU 环境，GLM 可以是电脑或其他已有服务器上的兼容 API。

## 1. 选择连接方式

| 项目 | 推荐：SSH 转发 | 可选：局域网直连 |
|---|---|---|
| ZERO 执行器监听 | `127.0.0.1:8766` | ZERO 的实际局域网地址或 `0.0.0.0:8766` |
| 电脑访问执行器 | `http://127.0.0.1:8766` | `http://ZERO_IP:8766`，将 ZERO_IP 换成实际地址 |
| 传输 | SSH 加密通道 | HTTP + Bearer token；仅用于受控 Wi-Fi，跨不可信网络应使用 SSH 或 TLS |
| 相机配置 | 配套示例可直接保留 URL | 修改 camera/state/tof 三个 URL |
| nanoMuse → Gateway | 两种方式都连电脑的 `http://127.0.0.1:8765/mcp` | 同左 |

同一 SSID 不一定允许互通。电脑和 ZERO 应在能互相访问的子网，关闭该网络对这两台设备的 AP / 访客隔离；按实际防火墙规则放行选定的 SSH 或执行器端口。建议给 ZERO 设置 DHCP 地址保留。不要把 `0.0.0.0` 当作访问地址，也不要把 Gateway 的 MCP Host 校验整体关闭。

两台设备保持正常系统时钟同步；传感器年龄仍使用原始捕获时间与单调时钟预算检查。网络抖动使数据超时会阻止动作，不能用延长过期阈值代替连接排查。

## 2. 安装与令牌

两台设备解压同一份工程，进入目录。ZERO：

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
```

电脑：

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[vision]'
```

用下面命令分别生成三个令牌：`EXECUTOR_TOKEN`、`VISION_TOKEN`、`GATEWAY_TOKEN`。它们是本工程服务的访问凭据，与 GLM API key 不同。设置到各进程使用的私有环境文件或终端，不要写进共享的示例 JSON。

```bash
python -c 'import secrets; print(secrets.token_urlsafe(32))'
```

`.env.example` 不会自动加载。各终端需要显式 export；systemd 则使用对应 `EnvironmentFile`。首轮保持各服务前台运行，便于观察状态。以下占位值需要换成实际值；令牌必须一致地配置在服务和其客户端。

## 3. ZERO：先启动只读执行器

确保原生 daemon 已按你的官方流程部署，本账户具有 robotd、mediad、tofd socket 的连接权限。不要额外打开串口。

```bash
export EXECUTOR_TOKEN='替换为生成的执行器令牌'
export EXECUTOR_ALLOW_MOTION=0
export EXECUTOR_ROBOTD_VERSION=0.17.8
duck-harness executor --host 127.0.0.1 --port 8766
```

运行 0.16.3 时把版本项改为 `0.16.3`；也可以使用 `auto`，只接受这两个版本。软件读取真实 `hello` 握手，不以环境变量冒充设备版本。`/state` 中 `firmware` 应给出实际版本、API、revision 和兼容判断。自定义构建 revision 会显示提示，仍需真实状态检查。

电脑另开终端建立通道，保持这个终端运行：

```bash
ssh -N -o ExitOnForwardFailure=yes \
  -o ServerAliveInterval=15 -o ServerAliveCountMax=3 \
  -L 127.0.0.1:8766:127.0.0.1:8766 YOUR_USER@YOUR_ZERO_HOST
```

在电脑设置同一个 `EXECUTOR_TOKEN` 后读取：

```bash
curl --fail --silent --show-error \
  -H "Authorization: Bearer $EXECUTOR_TOKEN" \
  http://127.0.0.1:8766/state
```

同时核验 `/frame` 和 `/tof`。刚启动时 `/health` 的版本可能尚未检查；先读取 `/state` 才会实际订阅。确认完整 15 个测量关节、真实策略、IMU、相机及新鲜 ToF。缺少数据时显示未就绪是正确行为。`sensor=fake/sim` 不用于 live 运动许可。

### 如果选择局域网直连

将 ZERO 的启动命令改为 `duck-harness executor --host 0.0.0.0 --port 8766`，并保持至少 24 字符的 `EXECUTOR_TOKEN`。电脑的 `EXECUTOR_URL` 改成 `http://ZERO_IP:8766`；相机 JSON 的 `camera_url`、`state_url`、`tof_url` 也都改为这个地址下的对应路径。其他步骤相同。只运行一个 executor 和一个 Gateway worker。

## 4. 打印、摆放、标定

打印 `output/pdf/microduck-aruco-print-pack.pdf` 的前四页，各一张。第五页为相机内参棋盘，标定后移出场地。不要从聊天截图打印图案。

按 [aruco-placement.md](aruco-placement.md) 摆放：四块竖直板分别位于 `(±1.20,0,0.20)`、`(0,±1.20,0.20)` m，正面朝原点，TOP 向上，黑方块 160 mm，白边至少 20 mm。配套 PDF 指南有俯视图与高度图。

电脑运行只读采集和标定：

```bash
python scripts/calibrate_camera.py capture \
  --executor-url http://127.0.0.1:8766 \
  --directory data/calibration-raw

python scripts/calibrate_camera.py calibrate \
  --images data/calibration-raw \
  --output data/camera-calibration.json
```

上面的采集命令使用 SSH 转发；如果选择局域网直连，将 `--executor-url` 改为 `http://ZERO_IP:8766`，其中 ZERO_IP 是实际地址。采集终端每次 Enter 保存一张，建议 20–30 张不同倾角、距离和画面位置，至少 12 张有效的不同视角。脚本只取原生图像，不驱动电机。输出不会覆盖旧文件；重新标定用新的输出文件名。

复制 `config/aruco-lab.example.json` 为 `config/aruco-lab.local.json`，把标定输出的 `camera_matrix`、`distortion`、`image_size` 填入。保留原始像素方向、动态 camera FK 和四块板地图；若实际安装不同，需要正确外参，不能用错误地图补偿。

```bash
python scripts/check_aruco_setup.py --config config/aruco-lab.local.json
```

模板未填 K/D 时此检查必须返回未就绪；检查通过也只证明配置自洽，仍需实际画面与坐标核验。

## 5. 电脑：启动视觉与 Gateway

视觉终端（同样先激活虚拟环境）：

```bash
export EXECUTOR_TOKEN='与ZERO相同的执行器令牌'
export VISION_TOKEN='替换为生成的视觉令牌'
duck-harness vision --config config/aruco-lab.local.json
```

Gateway 终端：

```bash
export EXECUTOR_TOKEN='与ZERO相同的执行器令牌'
export VISION_TOKEN='与视觉服务相同的令牌'
export GATEWAY_TOKEN='替换为生成的Gateway令牌'
duck-harness gateway --config config/aruco-gateway.example.json
```

打开 `http://127.0.0.1:8765`，输入 Gateway 令牌并连接。**没有 GLM API 时也能启动这个工作台并检查状态、配置模型；模型未就绪时新目标会被拒绝。** 当前机身位置必须与场地实际坐标相符。定位未知不是 `(0,0)`。

工作台模型设置支持 Base URL、准确模型 ID、无认证/环境变量/本地保存密钥、超时、JSON mode，以及 AgentJev 地址、置信度门槛、margin 与连续快速决策上限。模型 API key 不回显。修改在空闲时生效；已有任务固定使用启动时的配置，下一项任务使用最新保存配置。立即切换应先明确停止当前任务，再启动新任务。

模型配置保存在 `data/aruco-model-settings.json`，密钥另存权限为 0600 的凭据文件或从指定进程环境变量读取。已有保存配置优先于启动环境的模型默认项；使用网页或 `PATCH /api/model-config` 修改。工作区、限速、执行器地址等不属于模型可改设置。完整字段见 [model-config.md](model-config.md)。

GLM 使用兼容 Chat Completions 接口，Base URL 通常以 `/v1` 结尾；适配器也接受完整的 `/chat/completions` 地址。模型 ID 以你的服务器实际返回为准。先按 [agentjev.md](agentjev.md) 启动真实 AgentJev；若暂时只有 GLM，可在设置中选择 `glm`，以后再切回 `hybrid`。

## 6. 第一轮任务

把机身参考点放在原点附近，面对 ID 0。先只读查看 `marker_0`、世界位姿、yaw、目标 bearing、实际距离，验证坐标方向和尺度。原生策略/IMU尚未就绪时，目标即使可见也可能被阻止，这是需要排查的真实状态。

1. 原生策略按已有流程处于健康的 `stand` 或 `walk`，模型和视觉就绪后，保持 `EXECUTOR_ALLOW_MOTION=0`，提交“寻找 marker_0”。此时目标已在视野内，完成判定应来自至少 3 个不同的新鲜帧。任务仍会请求停止行走，不是纯只读接口；真正只读请使用状态/观测。若目标不在视野，先调整物理布置，不通过盲转找回定位。完全未启用或抱起的机器人可能被健康门控阻止。
2. 按已有真机流程验证站立与行走，处理手柄等其他运动控制者，保持头部在已验证的感知方向。将 ZERO 的 `EXECUTOR_ALLOW_MOTION=1` 后重启执行器，重新读取状态；服务不会自动上电或开启策略。
3. 提交“靠近 marker_0 并停下”。配套 Gateway 到达距离为机身参考点到板中心 **0.55 m**；板端 ToF门槛从传感器位置计，二者不能等同。
4. 完成后再尝试区域内探索。此地图名义区域 2 m × 2 m，扣除 0.20 m 边界余量，机身参考点限制在 x/y 的 ±0.80 m。四块板不保证连续 360° 视野；丢失位姿时停止。

这些步骤不意味着未知房间导航或全向障碍感知已实现。实际能力仍是有限动作下的寻找、靠近和有界探索。观测验证任务完成与确认物理停止是不同证据。

nanoMuse 添加 `http://127.0.0.1:8765/mcp` 并填写 Gateway token。其对话模型设置独立于本工程的 GLM 设置，见 [nanomuse.md](nanomuse.md)。系统服务模板使用前请填实际账户、绝对路径和本地配置路径，首轮优先前台核验。
