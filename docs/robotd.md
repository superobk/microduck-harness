# ZERO 3W 执行器与原生 robotd

本适配器以完整 **15 舵机 XL330 Microduck**、ZERO 3W 上的 **robotd 0.16.3 和 0.17.8** 为目标。两版均已逐项核对固定源码 tag 的实际字段和处理方法，并通过本地 Unix socket 协议测试。没有连接真机执行过动作；软件测试不能代替对应硬件、IMU 安装和策略的实测。

## 两个版本怎样选择与核验

| 本地选择 | 原生 `hello.daemon_version` | 必须匹配的 `api_version` | 本工程使用的协议 |
|---|---|---:|---|
| `EXECUTOR_ROBOTD_VERSION=0.16.3` | `0.16.3`，允许合法 `+build` 元数据 | 41 | 状态订阅、健康、15 关节、IMU、安全标志、相机/ToF FK、UYVY、ToF |
| `EXECUTOR_ROBOTD_VERSION=0.17.8` | `0.17.8`，允许合法 `+build` 元数据 | 44 | 同上；所用类型与 0.16.3 定义一致 |
| `EXECUTOR_ROBOTD_VERSION=auto` | 上述任一版本 | 按实际版本精确匹配 | 自动选择这两个已审计配置之一 |

`auto` 是默认值，只接受表中两个版本。它不会把未知版本、`null`、错误 API 编号或 prerelease 当成已兼容版本，也不会根据 API 编号猜测缺失的 daemon 版本。设置为一个明确版本时，实际连接到另一个版本会显示不匹配并拒绝新动作。切换板上的 robotd 后，修改这一环境项并重启执行器；或者保留 `auto`。

连接的第一条消息是实际的 `hello` RPC，读取运行进程返回的 `daemon_version`、`api_version` 和 `revision`。随后在同一连接上发送 `robot.subscribe`。每次将要发送 `robot.move` 时，新命令连接也先执行 `hello`，核对与状态订阅的身份一致，并重新检查状态有效性。发生版本/API 不匹配，或者状态订阅来自另一版本/源码 revision 时，不会发送该移动命令。`robot.stop` 保留独立可用，不被版本核验阻止。

`GET /state` 的 `firmware` 对象显示实际完整版本、匹配的 `profile_version`、API、revision、兼容判断及原因。`GET /health` 显示已知的最近身份；刚启动、还没有读取状态时，身份为 `not_yet_checked`，不会报告机器人健康正常。

源码审计使用的 tag 提交为：

| Tag | 固定提交 |
|---|---|
| `daemon-v0.16.3` | `0b2b82f56ac1e32d69018837ac1cd7d4c687f5a0` |
| `daemon-v0.17.8` | `942e58e3aa6dbb28e7037203fff11866122c7567` |

本地编译可能使用不同 revision，或返回 `null`。本工程保留并显示这种差异；只要声明的版本/API 正确，仍允许继续严格检查运行时字段，兼容性不会仅由官方提交 SHA 决定。带 `+imu-id200.local` 等合法构建元数据的版本会保留完整字符串并显示自定义构建提示。自报身份不是二进制认证，定制 IMU、坐标轴或策略仍需按实际安装验证。

两版原生 `robot.health` 都提供当前适配器要求的 `control_loop`、`bus`、`imu.ready` 和 `imu.consecutive_stale_blocks`；两版状态也都提供 `safety.picked_up`、测量关节、IMU 和动态传感器 FK。因此这里没有“旧版本字段缺失就默认安全”的分支。正常情况下按相同证据工作；字段缺失、错误类型、冻结或时间过期都明确拒绝运动。

## 真正使用的协议

`robot.state` **不是请求方法**。完成 `hello` 后，客户端发送 `robot.subscribe`，得到 `accepted` 回应，然后接收 `robot.state` 通知。`RobotdClient` 保持一个 20 Hz 订阅，同时读取 `robot.health`。运动请求只发送到 `/run/robotd.sock`，没有打开串口或 Dynamixel 总线。

| 本服务接口 | 原生接口 | 实际语义 |
|---|---|---|
| `GET /state` | `hello` + `robot.subscribe` + `robot.health` | 实际版本、测量状态、健康、执行器 epoch、短时 state_token |
| `POST /action` | 周期发送 `robot.move`，最后 `robot.stop` | 一个有截止时间的速度脉冲 |
| `POST /stop` | `robot.stop` | 清零行走速度，废弃旧 epoch 和票据 |
| `GET /frame` | `/run/mediad/media.sock` 的 `media.frame` | 原始 UYVY 字节与捕获时间、旋转元数据 |
| `GET /tof` | `/run/tofd/tof.sock` 的 `tof.stream` | 原始 `tof.frame` 距离与状态数组 |

这里没有 `robot.enable`、`robot.init`、`robot.relax`、`robot.do` 端点。启用控制器与启动电机仍由已有真机操作流程完成。`robot.stop` 不会中止已经启动的 `ground_pick` 等原生一次性技能，因此首版执行器只开放有界行走。

`enabled` 的归一化含义是当前 tick 的 `policy` 为 `walk` 或 `stand`，表示策略确实正在驱动，不是猜测电机是否通电。`healthy` 要求明确的健康回应、IMU 就绪及未冻结、最新控制循环、完整 15 关节、有效姿态以及未跌倒、未抱起。缺失值不会被补成“正常”。

## 在 ZERO 3W 启动

将工程放到板上的任意工作目录，创建 Python 3.11+ 虚拟环境并安装工程。执行器不需要 torch、CUDA 或 OpenCV。

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e .
export EXECUTOR_HOST=127.0.0.1
export EXECUTOR_TOKEN='replace-with-a-long-random-token'
export EXECUTOR_ALLOW_MOTION=0
export EXECUTOR_ROBOTD_VERSION=0.17.8
uvicorn microduck_harness.executor:create_executor_app --factory --host "$EXECUTOR_HOST" --port 8766 --workers 1
```

上例选择 0.17.8；使用 0.16.3 时改成 `EXECUTOR_ROBOTD_VERSION=0.16.3`。先读取 `/health`、`/state`、`/frame`、`/tof`，确认对应的是实际运行的版本、策略、15 个关节、新鲜 IMU 与 ToF。仅在已有站立/行走操作验证完成后，将 `EXECUTOR_ALLOW_MOTION=1` 并重启本服务。启动服务本身不会发送运动或上电命令。

```bash
curl -H "Authorization: Bearer $EXECUTOR_TOKEN" http://127.0.0.1:8766/state
```

电脑连接板上服务可用 SSH 转发：

```bash
ssh -N -L 8766:127.0.0.1:8766 user@MICRODUCK_HOST
```

电脑和 ZERO 3W 在同一 Wi-Fi 时可直接绑定局域网，将 `EXECUTOR_HOST` 设置为板上局域网地址或 `0.0.0.0`，并使用至少 24 字符的随机 `EXECUTOR_TOKEN`。服务启动时检查这个绑定配置。运行 uvicorn 时必须使用相同的 `EXECUTOR_HOST`，不要通过另一个 `--host` 值绕过启动配置。只运行 **一个 worker**，因为单所有者、epoch、票据与请求日志属于同一个进程。客户端使用 ZERO 的实际局域网 IP；`0.0.0.0` 是监听地址，不能作为电脑访问目的地址。

`deploy/microduck-executor.service` 是可安装模板。先替换 `User`、`Group` 和工程绝对路径，确保该账户原本就有三个 Unix socket 的连接权限；原生观测 socket 使用 `robot` 组，模板列出这个附加组，需与板上实际部署相符。不要为此开放舵机串口的其他控制者。环境文件路径为 `/etc/microduck-harness/executor.env`，里面设置主机、token、是否允许运动和 robotd 版本。模板默认只读。

| 环境变量 | 默认值 | 作用 |
|---|---|---|
| `EXECUTOR_ROBOTD_VERSION` | `auto` | 两个受支持版本的选择与握手核验 |
| `EXECUTOR_HOST` | `127.0.0.1` | 本机监听；同 Wi-Fi 直连可配置 `0.0.0.0` |
| `EXECUTOR_TOKEN` | 空 | 非 loopback 绑定必须至少 24 字符；客户端用 Bearer 认证 |
| `EXECUTOR_ALLOW_MOTION` | `0` | 只有 `1` 才允许有界动作；不自动启用电机 |
| `ROBOTD_SOCKET` | `/run/robotd.sock` | 原生机器人 Unix socket |
| `MEDIAD_SOCKET` | `/run/mediad/media.sock` | 原始相机帧 Unix socket |
| `TOFD_SOCKET` | `/run/tofd/tof.sock` | ToF Unix socket |

## 一个速度动作

Gateway 完成视觉和边界检查后，立即读取 `/state`，将返回的 `epoch` 和 `state_token` 传入动作请求：

```json
{
  "request_id": "goal-123-step-4",
  "epoch": 123456,
  "state_token": "use-the-fresh-token-returned-by-state",
  "vx": 0.04,
  "vy": 0.0,
  "vyaw": 0.0,
  "duration_s": 0.4,
  "observation_age_s": 0.15
}
```

单位是 m/s、rad/s、秒。epoch 是服务实际返回的随机初值，示例数字不能直接使用。`state_token` 在 ZERO 的单调时钟上只存活 1 秒并且只能消费一次，避免网络中滞留的旧指令后来开始运行。来自客户端的视觉年龄加上票据在板上经过的时间不得超过 1 秒。新 epoch 也会使停止前的票据全部失效。

同时仅允许一个动作。线速度上限 0.08 m/s、转速上限 0.5 rad/s、单脉冲上限 1 秒，默认每 50 ms 刷新一次速度。只支持单独向前移动或单独转向；后退、横移以及边移动边转向都被拒绝，这和首版 Gateway 的前方感知、轨迹检查范围一致。视觉数据的剩余有效时间也会缩短脉冲，不会因动作还没结束而续期旧观测。

执行期间继续检查 robotd 状态和板上最新 ToF；跌倒、IMU/总线异常、策略退出、超时、新进入的近距离障碍都会进入最终停止。ToF 与状态使用同板单调时钟配对。前进要求中心 4×4 测量全部有效且最近距离大于 `0.30 m + 剩余行程`；转向采用更保守的全 8×8 有效测量、最近距离大于 0.40 m。只接受原生有效状态 5/9，255“没有目标”仍然是未知，不算前方空旷。ToF 的动态 FK 光轴必须朝向机身前方 20° 范围。缺失 ToF、时间失配、数据过期或传感器转向其他方向，都会拒绝新运动。

这些是前方传感器的局部保护，不能证明身体两侧或后方没有障碍。转向也需要事先确认的平整、开阔工作区域。原生 robotd 的 deadman 仍是另一层保护，不会被无限重发旧目标绕过。

相同 `request_id` 与完全相同参数重试，只返回原动作的结果，不重新执行。相同 ID 不同参数被拒绝。网络错误后 Gateway 不自动重试运动。HTTP 客户端断开不会取消板上的截止时间和最终停止逻辑。

动作结果里的 `status=completed` 仅说明速度脉冲执行路径结束。`verified_success` 始终为 false，因为“找到目标/到达位置”必须由后续观测验证。`robot.stop` 收到 accepted 也仅证明意图被接收，所以 `verified_physical_stop` 同样不伪报为 true。若无法确认停止请求被接收，执行器锁定新动作直到本地排障和服务重启。

## 帧与时钟

`robot.state.t_ns` 和 `tof.frame.t_ns` 是板上同一个 `CLOCK_MONOTONIC`。执行器在板上检查年龄，避免直接拿电脑时钟相减。`observed_at` 是根据板上时刻和测量年龄换算的 Unix 时间，供显示及近似配对；跨主机精确配对应优先采用同源 monotonic 时间与传输预算。

`GET /frame` 返回 `application/octet-stream`，没有改变像素或旋转。响应头包括：

- `X-Frame-Captured-At`：mediad 原始 Unix 捕获时刻（秒）。
- `X-Frame-Id`：mediad 原始微秒捕获时刻。
- `X-Frame-Age-S`：板上计算的捕获年龄，电脑还需加上自身接收/推理经过的时间。
- `X-Frame-Width`、`X-Frame-Height`、`X-Frame-Format=UYVY`、`X-Frame-Rotate`。

`GET /tof` 保留毫米距离、每个像素的原始状态值及 sensor 时间，执行器不把无返回当成“前方无限空旷”，也不会把最近的 ToF 测量冒充纸团距离。

两版 tofd 都会在 `tof.stream` 回应中明确标记 `sensor="fake"` 或 `sensor="sim"` 的合成模式。live 执行器拒绝这两种来源，不把它们用于真实运动许可；本工程的 mock 模式仍可独立用于无硬件演练。

## 已知边界

首轮定位与行走实验需要头部持续朝向已标定的工作方向。官方配置的 `[idle] enabled = true` 会在空闲时加入头部扫视；可按原生配置流程关闭 idle，并关闭会移动头部的 chorale / theremin，重启 robotd 后再按原有真机流程启用。工程不会替你修改这些开关；若头部偏离，ToF 方向检查会拒绝运动。

原生 robotd 的连续意图仍采用最后写入者生效。执行器自己的单所有者锁不能阻止游戏手柄、netpadd、WebRTC 或其他本地进程同时发送行走指令。运行自主任务时，需要按板上的实际部署配置停用或仲裁这些并行运动入口；人工接管应先停止当前任务。这个工程没有擅自修改原生守护进程配置。

执行器没有桌边/楼梯识别，也不把健康 IMU 等同于可靠避障。区域、定位、目标距离由电脑侧视觉与 Gateway 检查，前方 ToF 由两侧独立检查。传感器覆盖不足时应停下来；从这版局部目标执行扩展到整屋自主导航，还需要地图、定位覆盖与轨迹验证。

## 软件验证与源码依据

`tests/fixtures/robotd_0.16.3.json` 和 `robotd_0.17.8.json` 是根据固定源码字段生成的**合成协议测试数据**，含 tag、提交和来源 URL，不是真机录制。参数化测试覆盖两版 hello→subscribe、健康、15 关节/FK、media 二进制尾部、ToF 流、版本/API 不匹配、构建变化、缺失健康字段和停止路径。其余执行器测试验证有界脉冲、断开后的最终停止、epoch/票据过期以及拒绝重放。

| 核对范围 | 0.16.3 | 0.17.8 |
|---|---|---|
| IPC 类型及 API 编号 | [duck-ipc-proto/src/lib.rs](https://github.com/pollen-robotics/microduck/blob/daemon-v0.16.3/duck-ipc-proto/src/lib.rs) | [duck-ipc-proto/src/lib.rs](https://github.com/pollen-robotics/microduck/blob/daemon-v0.17.8/duck-ipc-proto/src/lib.rs) |
| hello、健康、订阅及状态发布 | [robotd/src/main.rs](https://github.com/pollen-robotics/microduck/blob/daemon-v0.16.3/robotd/src/main.rs) | [robotd/src/main.rs](https://github.com/pollen-robotics/microduck/blob/daemon-v0.17.8/robotd/src/main.rs) |
| 相机 JSON 头与原始字节 | [mediad/src/frame.rs](https://github.com/pollen-robotics/microduck/blob/daemon-v0.16.3/mediad/src/frame.rs) | [mediad/src/frame.rs](https://github.com/pollen-robotics/microduck/blob/daemon-v0.17.8/mediad/src/frame.rs) |
| ToF 状态码与测量语义 | [tof/src/lib.rs](https://github.com/pollen-robotics/microduck/blob/daemon-v0.16.3/tof/src/lib.rs) | [tof/src/lib.rs](https://github.com/pollen-robotics/microduck/blob/daemon-v0.17.8/tof/src/lib.rs) |
| ToF 光轴及坐标 | [kinematics/src/tof.rs](https://github.com/pollen-robotics/microduck/blob/daemon-v0.16.3/kinematics/src/tof.rs) | [kinematics/src/tof.rs](https://github.com/pollen-robotics/microduck/blob/daemon-v0.17.8/kinematics/src/tof.rs) |
