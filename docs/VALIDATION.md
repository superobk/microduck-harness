# v0.3.0 验证记录

记录日期：2026-10-28（北京时间）。目标：完整 15 舵机 XL330 Microduck，官方 `daemon-v0.16.3`（API 41）与 `daemon-v0.17.8`（API 44）通信协议。电脑与 ZERO 的部署方案按同一 Wi-Fi 编写。

## 实际完成的验证

最终执行：

```text
python -m pytest -q --junitxml=/tmp/microduck-harness-v0.3-tests.xml
351 passed, 1 warning in 12.83s
```

| 测试文件 | 数量 | 验证范围 |
|---|---:|---|
| `test_decisions.py` | 43 | AgentJev 原生 API、严格分布校验、GLM Chat Completions、超时、无重试、凭证不回显 |
| `test_guard.py` | 45 | 数据时效、未知位姿/测距、区域、有限速度、动作形状、NaN、服务 URL |
| `test_engine.py` | 11 | 任务完成证据、取消模型等待、单所有者、幂等、重启、过期决定、错误回执、按类别寻找 |
| `test_executor.py` | 15 | 实际 fake Unix socket、原生消息、状态订阅、相机二进制、epoch、期限、停止、ToF 故障 |
| `test_vision.py` | 9 | 真实 OpenCV ArUco 检测与 PnP、坐标、测距、认证、旧帧、跨主机时钟偏移 |
| `test_contracts.py` | 1 | 板端归一化 → HTTP → 视觉 → LiveBackend → 执行器的字段/单位/时间/坐标契约 |
| `test_gateway.py` | 8 | 真实 loopback HTTP、官方 MCP 客户端、初始化/列工具/调用/查询/取消、认证和 CLI |
| `test_firmware_compatibility.py` | 64 | 两版 hello/API/订阅/运动连接、完整15关节、构建变化、缺失健康字段及合成ToF拒绝 |
| `test_model_config.py` | 31 | ASGI读写、认证、脱敏、任务版本固定、端点与环境凭据绑定、原子持久化失败恢复 |
| `test_aruco_layout.py` | 6 | 四方位合成图像的真实检测/PnP、地图朝向、尺寸与缺内参阻断 |
| `test_camera_calibration.py` | 3 | 16个合成视角的真实棋盘求解、重复帧拒绝、原始图像方向与过期检查 |
| `test_print_assets.py` | 12 | 最终PDF的A4/矢量尺寸、实际150dpi识别、静区及8×5棋盘角点 |
| `test_deployment.py` | 17 | 两版×SSH/LAN配置一致性、可执行生成脚本、测量字段合并、令牌分离/权限/保留、Git忽略及原生两键备份合并 |
| `test_tools_bridge.py` | 15 | 65文件完整性、双版本Web契约、真实本地HTTP、CSRF/Origin、保存/应用与mediad重启边界 |
| `test_plugins.py` | 71 | 校准/相机真实契约、当前PID、旧回执、保存待生效、动作generation/期限、低权限systemd身份及真实跨UID内核限制 |
| **合计** | **351** | 0失败、0错误、0跳过；软件测试，非硬件成功率 |

另行运行保留的microduck-tools上游测试：`python -m unittest discover -s integrations/microduck_tools/upstream/calibration/tests -v`，**35项全部通过，耗时5.617秒**。这35项与上表分开记录，不重复计入351。

完整测试需要允许本地Unix socket与loopback HTTP。受限沙箱最初阻止创建测试socket；在获准运行本地测试进程后完成上述最终结果，没有连接机器人、外部模型或摄像头。唯一pytest warning来自真实跨UID合成测试在有线程的进程中调用`fork()`的Python弃用提示；该项本次实际通过而非skip，未把警告描述成设备错误或硬件验收。

MCP 测试启动真实 Uvicorn 服务并使用官方 MCP SDK 的 StreamableHTTP 客户端，没有用直接调用 Python 函数替代协议。测试同时校验工具的 JSON 文本内容与 `structuredContent` 一致。缺少 bearer 返回 401，跨来源调用返回 403，REST 任务冲突返回 409，MCP 业务冲突返回工具错误。

视觉测试生成实际 ArUco 图像，再通过 OpenCV 检测、PnP 与 SE(3) 变换恢复预设机器人位置。它验证数学与软件接口，不能代替用户相机的物理标定。

跨模块测试使用原生形状的完整 15 关节状态、测量头部、scalar-first 四元数、UYVY、ToF 毫米和原生时间，确认各层的接口可以连接。外部假 daemon 是测试设备，没有访问真实舵机。

模型配置测试在任务的假模型请求阻塞期间保存新设置，确认当前任务继续使用原客户端、模型 ID、密钥与超时，下一任务采用新版本。独立审查发现的环境凭据绕过来源绑定和配置 rename 后目录 fsync 失败窗口均已修正并有回归覆盖。保存不自动发起模型请求，也不隐式取消机器人任务。

两份 robotd fixture 标注为依据固定上游源码生成的合成数据，未冒充两版设备实录。自定义构建元数据与 revision 被记录和提示，版本/API及实际测量字段仍严格核验。

## 打印交付验收

本版沿用v0.2的同一套打印文件，地标ID、字典、尺寸和摆放几何没有变更。下列原有逐页目视检查记录仍适用于这些相同字节的资产；本版351项回归再次包含最终PDF光栅检测与几何检查。v0.3插件部署以本仓库统一指南为准。

`output/pdf/microduck-aruco-print-pack.pdf` 与 `microduck-aruco-placement-guide.pdf` 各5页，合计10页，全部经 Poppler 渲染后逐页目视检查；文案修订涉及的页面再次渲染检查。无文字叠压、截断或丢失字形。

- 前4页 PDF 矢量黑边总尺寸160×160mm；单个码的边界为页面底部坐标 x=25..185mm、y=80..240mm，数字容差0.001mm。
- 150dpi实际PDF光栅经OpenCV检测，四页分别仅返回ID0/1/2/3，并核对角点、160mm边长及20mm白色净空。
- 第5页矢量棋盘为9×6方格，每格20mm；图案180×120mm，实际检测出8×5内部角点。
- 四板地图的世界变换通过合成投影→像素检测→PnP→世界位姿恢复验证；检测启用了SUBPIX细化。
- 原始模板的K/D刻意保留null，离线检查实际返回exit2（未就绪）；未捏造用户标定。

以上是数字文件与软件几何验证。没有操作实际打印机，未测量纸张、板位、用户相机或真实定位误差。必须按指南实测黑边、标尺、中心高度与地图方向。

## 前一版已运行的目标演示（v0.1记录）

以下是v0.1已有的本地CLI完整目标记录，作为历史演示保留；本次v0.3的最新验证为上面的351项回归，不把旧记录改写为新实机运行：

```text
duck-harness gateway --host 127.0.0.1 --port 8875
HARNESS_MODE=demo
DECISION_MODE=demo
```

通过真实 HTTP 提交“靠近纸团并停下”，轮询得到：

```json
{
  "status": "succeeded",
  "verified_success": true,
  "step": 14,
  "reason": "target confirmed on distinct fresh frames",
  "evidence_frames": 3,
  "target_range_m": 0.3268322816,
  "source": "kinematic-demo",
  "stop_command_accepted": true,
  "physical_stop_verified": false
}
```

`step` 是任务循环计数，包含完成前的重复观测，不能解读为 14 次真实电机动作。所有位置、相机观测和目标都是二维运动学测试数据，未运行 AgentJev / GLM 权重，也没有原生步态动力学。真实停止状态没有伪造为已验证。

## 安装与静态检查

- v0.3.0在已有test/vision/print依赖的独立Python3.12.14环境中重新以editable wheel安装成功。
- `duck-harness --help` 正常，包含 gateway / executor / vision 三个入口。
- `duck-deploy validate`使用实际示例配置正常运行，明确报告API、实测K/D和占位主机/账户未就绪。
- 工作台根页面ASGI/HTTP检查成功；含模型及原生插件面板的JavaScript通过Node `--check`，56个DOM ID无重复。
- `scripts/setup_agentjev.sh` 通过 `bash -n`。
- `scripts/smoke_mcp.py` 在真实子进程中验证只读检查及显式模拟任务启动/取消。
- `requirements.lock` 为本次验证环境的精确依赖记录。
- 统一指南68个Bash代码块通过`bash -n`；5份部署入口文档的相对文件链接全部存在。语法检查不代表执行了设备命令。
- 候选发布文件通过敏感文件名与高置信私钥/token签名检查；真实令牌、本机配置、采集、数据库、构建产物和模型权重未纳入提交。

## 原生插件与普通账户部署验证

`microduck-tools`固定提交`2b93cb3afc32fa343cc612ee060e94c5bdd7b01d`的65个文件逐字节一致，全部包含在发布资产中；运行适配位于快照外部。

| 官方目标 | 固定源码提交 | 实际完成的检查 |
|---|---|---|
| daemon-v0.16.3 / API41 | `0b2b82f56ac1e32d69018837ac1cd7d4c687f5a0` | HEAD/tag核对、干净目标文件、dry-run无修改、完整patch apply、reverse-check及`git diff --check` |
| daemon-v0.17.8 / API44 | `942e58e3aa6dbb28e7037203fff11866122c7567` | 同上；相机部分为本工程明确移植 |

机器记录与补丁SHA256在`integrations/microduck_tools/SOURCE_VALIDATION.json`。当前环境没有cargo/rustc/Docker，**没有编译Rust或运行原生Rust测试**，也没有安装到ZERO。这些步骤已完整列入统一指南，不能用源码应用通过替代。

发布审查发现并修复普通账户不能读取其他UID的`/proc/PID/exe`的问题。修复只在明确EACCES/EPERM时接受严格的官方systemd直接启动契约，保持实际PID、进程名、开始时间、回执时间和动作generation检查。真实Linux测试以临时`robotd`命名的sleep进程和降为UID65534的读者触发内核拒绝，systemd响应为明确标注的测试fixture；不冒充真实daemon验收。

插件检查超时预算为250ms，停止通道独立；执行期限以原始观测/票据的绝对deadline计算，排队或插件检查耗时不会刷新可移动时间。保存未生效、PID变化、修改相机像素路径及动作期间generation改变等情况均有回归覆盖。

## 尚未完成的验证

| 项目 | 当前状态 |
|---|---|
| 用户的 GLM5.3-Flash 服务 | 未收到实际 endpoint / 模型 ID / 认证配置；仅验证兼容 API 契约 |
| AgentJev GPU 推理 | 未下载权重或运行 GPU；上游源代码与下载版本固定，客户端已测 |
| 用户的 nanoMuse Desktop | 已提供按上游接口核实的接入步骤；未操作用户客户端 |
| ZERO 3W / XL330 真机 | 未收到设备连接信息，未部署或执行任何真实动作 |
| 两版原生Rust插件 | 源码补丁实测应用通过；本环境未编译、未运行Rust测试、未安装到设备 |
| 用户相机 / ArUco 地图 / ToF | 已给打印件与标定工具；尚未获得实体测量和真实内参 |
| 15 舵机动力学仿真 | 此包的 demo 不是 MuJoCo / mjlab / Isaac Lab |
| 网页浏览器视觉验收 | 本地浏览器运行文件未能下载，独立云浏览器无法访问测试端口；没有宣称截图或点击验收通过 |
| 抓取、投放、整屋导航 | v0.3仍未实现，接口没有假称支持 |

## 已修复的具体问题

- 原生 `robot.state` 是订阅通知，修正了请求方式。
- 取消与断线不取消板端 watchdog；动作与停止用 epoch 和一次性状态票据隔离。
- 模型输出后重新取观测，不延用推理前的运动条件。
- 失败执行回执和未获确认的停止不能继续被当作成功。
- 被动只读 Gateway 退出时不向其他控制者发送停止。
- 相机 / FK 配对保留原始板端时钟，TTL 使用电脑侧保守年龄，避免混用时钟域。
- 类别级 `find` 可验证不同帧中的 YOLO 检测框；`approach` 仍需要稳定身份与实际距离。
- 动作 `wait` 必须真正零速度，禁止借动作名称绕过检查。

下一轮应在已标定、平整受控区域完成真实模型 → 模拟设备，再完成只读真机 → 标记寻找 → 标记靠近 → 有界探索，分别记录任务成功、时间、模型回退、停机原因和传感数据。没有测量之前不对真实性能作保证。
