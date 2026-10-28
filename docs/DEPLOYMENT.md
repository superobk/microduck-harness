# Microduck Harness 统一配置、原生部署与运行指南

版本：v0.3.0。目标：完整15舵机XL330 Microduck、ZERO 3W、robotd 0.16.3或0.17.8；电脑与ZERO处于同一可互通Wi-Fi。电脑运行nanoMuse、视觉、Gateway、AgentJev；自有GLM5.3-Flash通过可随时变更的兼容API接入。

本指南主路径使用原生Python进程、官方robotctl/更新器和显式SSH命令。所有生成脚本都可直接阅读。软件不会替你启用电机、恢复中断任务或把保存的配置冒充已生效。末尾附完整原生部署篇，包含两个版本的源码补丁、官方构建/签名/安装、真实校准采集、启用与回退命令。

## A. 配置从哪里来，谁负责生效

| 配置 | 唯一职责 / 修改入口 | 生效边界 |
|---|---|---|
| `config/deployment.local.toml` | 这次部署的版本、主机、端口、路径、插件指针、相机显示方向、视觉来源、初始模型值和工作区 | `duck-deploy render`生成两端文件；不会自动修改设备或重启服务 |
| `/etc/robot/robotd.toml` | 原生总线、策略、board、camera等设备设置 | 按原生流程重启对应daemon；保留原有其他键 |
| `/etc/robot/robot-calibration.toml` | 实测15关节零点/方向、IMU安装与原始gyro比例 | 由已打补丁的robotd在初始化总线时加载 |
| `/run/robotd/calibration-effective.json` | 正在运行的native decoder实际使用值 | 是运行回执，不是用户配置文件 |
| `/run/mediad/camera-effective.json` | 当前相机方向、原始图像及进程证据 | 是运行回执，不是用户配置文件 |
| `data/camera-calibration.json` | 实测相机K/D与原始分辨率 | 引用到部署TOML并重新render/restart vision |
| `data/live/model-settings.json`及凭据文件 | Gateway网页的运行时GLM/AgentJev设置 | 当前任务固定原版本；新保存配置供下一任务使用 |
| `build/deployment/pc/secrets.env` | 电脑的3个服务令牌和可选GLM key | 由服务启动脚本载入；权限0600 |
| `build/deployment/zero/secrets.env` | ZERO仅持有执行器令牌 | 不复制GLM key或Gateway/视觉令牌到板上 |

原生关节与gyro转换只执行一次：**robotd中的DynamixelIo负责转换，harness读取转换后的状态**。`camera_rotate`是显示元数据，ArUco始终使用原始UYVY、原始K/D和原生camera FK。统一配置不启用`--flip-in-pipeline`。

原始microduck-tools固定于提交`2b93cb3afc32fa343cc612ee060e94c5bdd7b01d`，完整源快照及哈希在`integrations/microduck_tools/`。0.17.8的相机部分由本工程移植，两个tag均有实际Git补丁应用记录；本次环境未编译Rust、未执行真机动作，现场构建和验收步骤仍需完成。

## B. 电脑：取得工程并建立环境

以下命令在电脑的Bash终端执行。Python要求3.11+。本指南默认电脑目录为`$HOME/microduck-harness`，ZERO目录为`/opt/microduck-harness`；已有同名目录时保留它，选择另一目录并同步填写配置。

```bash
git clone https://github.com/superobk/microduck-harness.git "$HOME/microduck-harness"
cd "$HOME/microduck-harness"
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[vision]'
cp config/deployment.example.toml config/deployment.local.toml
```

首次可用纯演示确认Python和工作台：

```bash
duck-harness gateway
```

打开`http://127.0.0.1:8765`，演示明确标为DEMO。完成后Ctrl-C关闭，腾出8765端口。演示数据默认在`data/`；统一live部署使用`data/live/`，不会把保存的demo决策模式混进真机配置。

## C. 电脑：填写一份部署TOML并生成两端文件

编辑`config/deployment.local.toml`。至少修改以下实际值：

| 字段 | 填写方式 |
|---|---|
| `robot.version` | `0.16.3`或`0.17.8`，与后面原生源码/安装目标一致 |
| `network.ssh_target` | 能SSH登录ZERO的`用户名@IP或主机名` |
| `network.zero_host` | 局域网直连时使用ZERO的真实IP；SSH模式不使用此地址 |
| `network.transport` | 默认`ssh`；也支持`lan` |
| `paths.workstation_root` | 默认`..`，相对于该TOML所在config目录 |
| `paths.zero_root` | 默认`/opt/microduck-harness`，ZERO源码安装路径 |
| `paths.workstation_user` / `zero_user` | 实际账户，用于可选systemd模板 |
| `model.base_url` / `model_id` | 可暂时保留空地址；后续在网页填写真实API与模型ID |
| `plugins.camera.rotation` | 原生显示校正角0/90/180/270，默认ZERO板型90°；核对实际安装 |
| `vision.calibration_result` | 初次保持空；完成相机标定后填`../data/camera-calibration.json` |
| `motion.allow` | 首轮保持`false` |

确认`plugins.required=true`。这使校准或相机插件缺少实际生效证据时无法获得运动票据。

首轮保留`robot.native_config`及两个`runtime_receipt`默认路径。它们引用已经运行的原生配置和回执；单改部署TOML不会迁移原生unit或Rust写入位置。本版两套原生补丁固定写入上述两个`/run/...`路径；定制路径必须先在原生端完成对应且经过审查的调整。

```bash
duck-deploy validate --config config/deployment.local.toml
duck-deploy render --config config/deployment.local.toml --out build/deployment
duck-deploy secrets --out build/deployment
```

`validate`只验证配置结构与文件；`render`只写本机文件。输出会明确指出相机尚未标定、API尚未填写等未就绪事项。它们不会联系机器人或调用模型。`secrets`生成独立随机令牌，不在终端输出值，已有密钥文件时拒绝覆盖。

生成目录：

| 文件 | 用途 |
|---|---|
| `pc/gateway.json`、`pc/vision.json` | 电脑配置，URL、地图、边界一致 |
| `pc/start-gateway.sh`、`pc/start-vision.sh` | 可读的前台原生命令，载入本机secrets.env |
| `pc/open-tunnel.sh` | 根据该部署生成SSH转发 |
| `zero/executor.json` | 精确daemon版本、插件门控、默认禁用运动 |
| `zero/native-overlay.toml` | 仅两个原生键，不能拿它覆盖完整robotd.toml |
| `zero/start-executor.sh`、`zero/start-tools.sh` | 板端前台启动命令 |
| `pc/*.service`、`zero/*.service` | 可选的原生systemd服务文件 |
| `deployment.json`、`render-manifest.json` | 非密钥部署摘要及生成文件哈希 |

重新render会更新未被手工改动的生成文件，保留secrets.env、真实标定和运行时模型配置。检测到手工修改的生成文件会拒绝覆盖；请把变更放回TOML，或输出到另一个目录。输出目录自带Git忽略规则，非默认`--out`同样不应把本机配置与凭据提交。不要直接修改生成的JSON来维持第二套参数。

## D. ZERO：取得相同工程与板端配置

在ZERO终端执行。已有原生daemon保持原来的总线所有权；执行器不需要OpenCV、torch或GPU。

```bash
sudo install -d -o "$USER" -g "$(id -gn)" /opt/microduck-harness
git clone https://github.com/superobk/microduck-harness.git /opt/microduck-harness
cd /opt/microduck-harness
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
mkdir -p deploy/generated
```

若目录已有内容，先检查现有仓库并正常更新，不覆盖未知文件。电脑与板上使用同一个harness提交；可在两端用`git rev-parse HEAD`核对。安装后确认本账户具有原生socket所需的`robot`组权限，变更组成员后重新登录。

在电脑复制**只有ZERO需要的生成目录**，替换下面SSH目标：

```bash
scp -r build/deployment/zero \
  YOUR_USER@YOUR_ZERO_HOST:/opt/microduck-harness/deploy/generated/
```

如果自定义了`paths.zero_root`，同步修改目标路径。在ZERO确认：

```bash
cd /opt/microduck-harness
chmod 600 deploy/generated/zero/secrets.env
cat deploy/generated/zero/executor.json
```

此处查看的是非密钥JSON。不要把secrets.env打印到共享记录中。

## E. 完成原生插件构建、校准和配置

**先执行本文末尾的“原生部署完整篇”**：固定对应tag、检查并应用源补丁、构建完整ARM程序集合、用自己的开发签名通过官方更新器预检与安装，再采集/求解/安装真实校准TOML。已有原生系统先完成第1–2节的核对和备份，跳过第3节bring-up，从第4节继续。新板先读第1节，再完成第3节bring-up；原生系统可用后补做第2节核对和备份，才继续第4节。这样回退所需的原始配置与版本记录才实际存在。

两个版本的完整插件都需要源码构建；不能只复制Python网页。原生校准CLI `install`默认dry-run，`--apply`只备份并写入文件，robotd重启仍需显式执行。

真实校准已安装后，在ZERO合并统一配置中的两个插件键：

```bash
cd /opt/microduck-harness
python3 scripts/apply_native_config.py \
  --config /etc/robot/robotd.toml \
  --overlay deploy/generated/zero/native-overlay.toml \
  --target daemon-v0.17.8
```

审阅后加`--apply`，需要写`/etc`时使用sudo；0.16.3替换target。自定义gyro比例还需在核对固件契约后加`--confirm-firmware-scale`。脚本只修改`bus.calibration_path`和`media.camera_rotate`，保留其他原生设置，绝不创建虚构的15关节校准。

在承托、卸力及没有自主任务的维护阶段，按完整篇显式重启对应daemon并核对。相机显示方向改变不会让ArUco再旋转原始图像。不要通过`--flip-in-pipeline`改变像素后沿用旧K/D与FK。

## F. 启动ZERO配置页面与只读执行器

两个不同终端：

```bash
cd /opt/microduck-harness
sudo bash deploy/generated/zero/start-tools.sh
```

```bash
cd /opt/microduck-harness
bash deploy/generated/zero/start-executor.sh
```

配置页面默认localhost:8768，默认不能自动应用相机重启；需要该功能时按照原生篇的明确参数单独启动。执行器读取生成的`allow_motion=false`，不会上电或启用策略。标准原生权限下，加入robot组的普通账户可以运行执行器；配置页面用sudo是为了已有`/etc/robot`文件的编辑权限，不用于额外打开总线。

在运行执行器的普通账户下确认配置和回执可读（自定义路径请同步替换）：

```bash
for plugin_file in /etc/robot/robotd.toml \
  /etc/robot/robot-calibration.toml \
  /run/robotd/calibration-effective.json \
  /run/mediad/camera-effective.json; do
  test -r "$plugin_file" || printf 'Missing or unreadable: %s\n' "$plugin_file"
done
```

官方默认文件模式可满足这项读取。若现场收紧为0640或使用自定义ACL，sudo配置工具原子替换文件后仍须核对owner/group和访问权限，按本机管理策略恢复robot组或ACL的读取授权。缺少证据时执行器保持阻断，不通过关闭插件检查解决。

电脑另开终端建立转发：

```bash
cd "$HOME/microduck-harness"
bash build/deployment/pc/open-tunnel.sh
```

SSH模式同时转发8766和8768；LAN模式只转发本机配置页面，执行器通过ZERO IP访问。同一Wi-Fi仍可能被访客网络或AP隔离阻断，需核对实际网络和防火墙。生成的URL位于`build/deployment/deployment.json`。

## G. 验证“已保存”和“实际生效”

在电脑新终端载入私有服务环境，然后使用当前部署的执行器地址。SSH默认：

```bash
cd "$HOME/microduck-harness"
set -a
. build/deployment/pc/secrets.env
set +a
curl --fail --silent --show-error \
  -H "Authorization: Bearer $EXECUTOR_TOKEN" \
  http://127.0.0.1:8766/state
curl --fail --silent --show-error \
  -H "Authorization: Bearer $EXECUTOR_TOKEN" \
  http://127.0.0.1:8766/plugins
```

LAN或改端口时使用deployment.json的`executor_url`。`/state`先完成真实hello/状态读取；`/plugins`核对本地文件与运行证据：

| 状态 | 含义与下一步 |
|---|---|
| `active` | 当前进程、target、保存值与实际生效值一致 |
| `saved` | 有有效配置，但没有可信的当前运行回执；检查补丁构建/服务/总线 |
| `pending` | 保存值与当前daemon不同，或有命令行覆盖；按维护流程应用并重新核验 |
| `unavailable` | 配置、版本、回执或进程身份不可用；按reasons修复 |

两插件都active才满足本部署的插件条件。`healthy`还取决于原生控制循环、15关节、IMU、策略、ToF等真实状态；插件active不代表已经可以行走。普通未启用/卸力状态允许继续只读检查，但任务引擎会要求原生策略就绪。

运行回执是daemon启动快照，不要求每秒改写；harness检查systemd MainPID、可执行程序证据、进程开始时间、target和有效参数，拒绝旧进程遗留。跨UID权限明确拒绝读取`/proc/PID/exe`时，只接受官方直接启动的systemd证据：Type=exec、active/running、MainPID与ExecMainPID及ExecStart.pid一致、明确的原生可执行路径，且仍核对真实`/proc/PID/stat`与回执时间。若现场hidepid等设置连这些证据都阻断，先解决读取授权；不会把无法核实视为active。票据绑定本次插件generation，保存或切换导致条件改变时，旧票据失效；动作执行中也持续核验，停止通道独立可用。

## H. 打印ArUco并取得相机内参

打印[五页A4打印包](../output/pdf/microduck-aruco-print-pack.pdf)，严格100%实际尺寸。前四页为DICT_4X4_50 ID0–3，黑边160mm、每侧纯白区至少20mm；第五页为9×6方格、20mm格长、8×5内部角点的相机标定棋盘。

四板竖直，TOP朝上，打印面朝原点；黑方块中心分别为`(1.2,0,.2)`、`(0,1.2,.2)`、`(-1.2,0,.2)`、`(0,-1.2,.2)`米。机身起点朝ID0，ID1在左侧。完整高度、朝向和量尺图见[放置PDF](../output/pdf/microduck-aruco-placement-guide.pdf)与[详细说明](aruco-placement.md)。

仍在已载入pc/secrets.env的电脑终端：

```bash
. .venv/bin/activate
python scripts/calibrate_camera.py capture \
  --executor-url http://127.0.0.1:8766 \
  --directory data/calibration-raw

python scripts/calibrate_camera.py calibrate \
  --images data/calibration-raw \
  --output data/camera-calibration.json
```

直连时替换executor-url。Enter保存原始图像，q结束；采集脚本不发送电机命令。建议20–30张不同倾角、距离和覆盖位置，至少12个有效不同视角。结果不会覆盖已有同名文件；重复标定请用新文件名。

审核K/D、原始尺寸、逐图误差和覆盖提示。将部署TOML的`vision.calibration_result`改为`../data/camera-calibration.json`，重新生成：

```bash
duck-deploy render --config config/deployment.local.toml --out build/deployment
python scripts/check_aruco_setup.py --config build/deployment/pc/vision.json \
  --printed-width-mm 160 --printed-height-mm 160 --scale-bar-mm 100
```

尺寸参数填写你的实体测量值。低重投影误差不能替代静态位置/方向验证；相机安装外参不同于原生模型时还需校准外参。四块地标不保证连续360°覆盖，定位丢失会停止，不能用旧位姿刷新时间。

## I. AgentJev、自有GLM和电脑服务

### I.1 AgentJev

采用固定上游AgentJev代码与权重版本，使用独立虚拟环境。在电脑新终端执行：

```bash
cd "$HOME/microduck-harness"
python3 -m venv .venv-agentjev
. .venv-agentjev/bin/activate
```

先使用[PyTorch官方安装选择器](https://pytorch.org/get-started/locally/)给出的原生pip命令，在这个环境安装与你的实际GPU驱动/CUDA兼容的PyTorch。不能仅从ZERO板型推断电脑的CUDA版本。确认该环境能够访问GPU，再显式安装AgentJev并下载固定权重：

```bash
python -c 'import torch; print(torch.__version__); print(torch.cuda.is_available())'
AGENTJEV_PYTHON="$PWD/.venv-agentjev/bin/python" \
  bash scripts/setup_agentjev.sh --install --download-weights

cd vendor/agent-jev
../../.venv-agentjev/bin/python -m jev_service.server \
  --checkpoint models/agentjev-v1/model.safetensors \
  --model-path models/agentjev-v1 \
  --temperatures models/agentjev-v1/temperatures.json \
  --device cuda:0 \
  --port 8149
```

保持该进程运行，另一个电脑终端验证：

```bash
curl --fail http://127.0.0.1:8149/health
curl --fail http://127.0.0.1:8149/api/info
```

服务地址应匹配部署TOML的`agentjev.url`，默认`http://127.0.0.1:8149`。上面的device与PyTorch环境要求电脑具备兼容CUDA设备；其他后端需按[AgentJev说明](agentjev.md)核对上游支持再改配置。本次没有运行GPU推理或宣称设备时延。0.85概率和0.20候选差距是初始调度参数，不代表真机成功概率。暂无AgentJev时可将`model.decision_mode`设为`glm`并render，或在Gateway模型设置中切换。

### I.2 视觉与Gateway

电脑分别开两个终端，从工程根目录运行：

```bash
bash build/deployment/pc/start-vision.sh
```

```bash
bash build/deployment/pc/start-gateway.sh
```

这两个脚本实际调用`.venv/bin/duck-harness vision/gateway --config ...`，载入私有secrets.env，不启动其他服务。打开`http://127.0.0.1:8765`，填写pc/secrets.env中的Gateway令牌。页面同时显示校准和相机插件状态，并提供配置页链接。

GLM API尚未提供也可以打开live工作台查看状态；模型字段未就绪时目标提交会被拒绝。在模型设置中填写Base URL、服务实际模型ID、认证和JSON mode，保存即可。环境变量方式使用启动进程中的`GLM_API_KEY`；编辑外部env不会热更新旧进程，需重启或在页面显式更换本地密钥。

已保存运行时模型设置优先于部署TOML的初始值，render不会覆盖它。更换endpoint/model时使用页面或`PATCH /api/model-config`；当前任务保留旧配置，下一任务使用新版本。详见[模型设置](model-config.md)。

### I.3 nanoMuse

nanoMuse添加服务地址`http://127.0.0.1:8765/mcp`，Key填写Gateway令牌。它自己的对话模型设置与harness的GLM配置分开。原有六个MCP工具负责观测、提交目标、查询、取消和停止；不开放配置写入、原生命令或shell工具。[接入说明](nanomuse.md)

## J. 原生启用、第一项任务和停止

先在原生完整篇第10节按你的既有真机流程完成`robotctl robot init`与`robotctl robot enable`核验。init会使关节上力并回home，enable才交给策略；二者不是只读操作。此阶段原生身体和传感状态必须健康，头部保持已标定朝向，其他手柄/远程运动入口按实际部署停止或仲裁。

机器人放在原点附近朝ID0。先只读观察世界位置接近`(0,0)`、yaw接近0、marker_0方位与尺度正确，再保持`motion.allow=false`提交“寻找marker_0”。已可见目标应由至少3个不同的新鲜帧确认；该任务仍会请求停止行走，不是只读API。

准备短距离行走时，在电脑将部署TOML中的`motion.allow`明确改为`true`，重新render并复制ZERO目录。secrets.env保持原值，不重新生成。停止正在运行的执行器后重新启动，让新的JSON生效；服务不会自动init或enable。

```bash
duck-deploy render --config config/deployment.local.toml --out build/deployment
scp -r build/deployment/zero \
  YOUR_USER@YOUR_ZERO_HOST:/opt/microduck-harness/deploy/generated/
```

执行器的生成JSON明确包含`allow_motion`。因此在使用这套配置时，**只在另一个终端export EXECUTOR_ALLOW_MOTION=1不会覆盖JSON**；应修改源TOML、render并重启对应服务。

随后提交“靠近marker_0并停下”。配套到达距离为机身参考点到板中心水平距离0.55m；ToF从传感器位置计，不能直接等同。确认该目标后再试有界探索，机身参考点活动范围为x/y各±0.8m。

停止自主任务：点击工作台停止，或在载入Gateway令牌的电脑终端执行：

```bash
curl --fail --silent --show-error -X POST \
  -H "Authorization: Bearer $GATEWAY_TOKEN" \
  http://127.0.0.1:8765/api/stop
```

Gateway不可达时，在能访问执行器的终端使用`POST /stop`和EXECUTOR_TOKEN；原生Unix socket停止命令见完整篇第10节。accepted表示请求被接收，物理停止仍需观测。`robotctl robot relax --yes`会卸力，必须先放稳/承托，不能把它当站立时的普通速度停止按钮。

## K. 可选systemd常驻与后续配置变更

首轮前台流程完成后，再安装已填真实账户/路径的生成service文件。电脑的这一步适用于Linux；macOS可继续前台运行或另配服务管理器。

电脑：

```bash
sudo install -m 0644 build/deployment/pc/microduck-vision.service /etc/systemd/system/
sudo install -m 0644 build/deployment/pc/microduck-gateway.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now microduck-vision microduck-gateway
```

ZERO：

```bash
cd /opt/microduck-harness
sudo install -m 0644 deploy/generated/zero/microduck-executor.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now microduck-executor
```

先关闭同端口前台实例，只运行一个Gateway worker和一个executor。SSH转发仍需保持；生成器不会把SSH密码写入unit，也没有自动恢复任务。工具配置Web默认按需前台启动，避免常驻拥有系统配置写权限的管理页面。

| 变更 | 操作 |
|---|---|
| GLM/AgentJev地址、模型、认证、调度阈值 | 工作台保存，下一任务生效 |
| 主机、端口、工作区、运动允许项 | 改部署TOML，render，复制受影响端，停止任务后重启相关服务 |
| 相机K/D或地图 | 更新实测来源，render，重启vision并重新静态核验 |
| 校准数组/IMU比例 | 原生校准CLI或配置Web保存，维护阶段显式重启robotd，检查active |
| 相机显示方向 | 同步部署TOML与原生camera配置，显式应用mediad，确认原始像素未变 |
| 0.16.3 ↔ 0.17.8 | 独立固定源码、对应插件构建/签名/安装及目标tag校准检查；不能只改版本字符串 |

不要把带真实密钥、真实采集、现场校准与本机配置的文件加入Git。仓库包含示例、合成测试和打印件，`.gitignore`已排除local配置、secrets、data、build与模型权重。

## L. 软件自检与当前验证边界

```bash
python -m pip install -e '.[test,vision,print]'
python -m pytest -q
python3 -m unittest discover \
  -s integrations/microduck_tools/upstream/calibration/tests -v
```

PDF光栅识别检查需要Poppler的`pdftoppm`。Rust构建和原生测试命令在下方完整篇中；本仓库的软件报告会区分Python、真实源码补丁应用、Rust编译和实体测量。当前没有为你的硬件、真实GLM或AgentJev推理结果作验收声明。

---

# 原生部署完整篇

下方与独立的[NATIVE_DEPLOYMENT.md](NATIVE_DEPLOYMENT.md)保持同一份原生命令说明，供第E步按顺序执行。完成原生安装、真实校准与相机配置后，返回第F步启动harness服务；第10节的物理启用留到第J步。

本指南负责 **ZERO 3W 上的原生软件、校准插件、相机配置、首次启用和恢复**。电脑端的 GLM、AgentJev、ArUco、Gateway、executor 配置与启动顺序见 [统一部署指南](DEPLOYMENT.md)。目标硬件始终是完整 **15 舵机 XL330 Microduck**。

这里的“插件”是经过固定版本适配的 **原生源码补丁 + 外置 TOML 校准文件 + 本机配置工具**。官方未经修改的 `robotd` 不会因为复制了一个网页或 TOML 就获得这些字段；必须先构建并安装对应补丁。本仓库保留 `microduck-tools` 原始快照，并在其外部适配两个 daemon tag。

本文命令依据固定源码核对。仓库中已做的 Python / 协议 / 补丁验证，不代表已经在你的 ZERO 上编译 Rust、驱动电机或验证物理平衡。下面的构建、安装和真机验收是你在设备上执行的步骤。

## 1. 两个版本与真实命令

| 项目 | 0.16.3 | 0.17.8 |
|---|---|---|
| 原生 tag | `daemon-v0.16.3` | `daemon-v0.17.8` |
| 固定提交 | `0b2b82f56ac1e32d69018837ac1cd7d4c687f5a0` | `942e58e3aa6dbb28e7037203fff11866122c7567` |
| `hello.daemon_version` | `0.16.3` | `0.17.8` |
| `hello.api_version` | `41` | `44` |
| harness 版本选择 | `EXECUTOR_ROBOTD_VERSION=0.16.3` | `EXECUTOR_ROBOTD_VERSION=0.17.8` |
| 本仓库插件适配 | 软件校准 + 相机四向配置 | 软件校准 + 本仓库移植的相机四向配置 |

`microduck-tools` 快照固定于 [`2b93cb3afc32fa343cc612ee060e94c5bdd7b01d`](https://github.com/superobk/microduck-tools/tree/2b93cb3afc32fa343cc612ee060e94c5bdd7b01d)。该上游提交的统一相机入口以 0.16.3 为基线；0.17.8 的相机适配由本 harness 提供，不能把上游旧目录的存在当成上游已验证的声明。

下表的命令在两版 `robotctl` 中都有对应解析入口。操作命令只在后面的人工验收阶段执行。

| 目的 | 两版共同的真实命令 | 含义 |
|---|---|---|
| 运行版本与进程身份 | `robotctl version --json` | 对照各 daemon 的版本、revision、实际可执行文件路径 |
| 本机健康 | `robotctl health --json` | 读取软件及设备状态；进程启动不等于 IMU / 电机就绪 |
| 配置读取 | `robotctl configure --file /etc/robot/robotd.toml --list --json` | 原生配置解析与列举，不驱动电机 |
| 原生配置交互编辑 | `sudo robotctl configure` | 使用原生 schema 编辑配置；注意保存后的重启要求 |
| 连续测量 | `robotctl monitor --hz 10 --json` | 订阅测量流，Ctrl-C 结束 |
| 已有策略 | `robotctl policy list --json` | 查看已安装策略；不下载或启用未知模型 |
| 驱动模式 | `robotctl robot mode --json` | 读取 walk / roller 模式，不是启用状态查询 |
| 电机回初始姿态 | `robotctl robot init` | 所有关节上力并在约两秒内到 home；此时没有策略负责平衡 |
| 启用平衡 / 行走策略 | `robotctl robot enable` | 把机器人交给已有策略，会发生物理动作 |
| 退出策略 | `robotctl robot enable --off` | 停止策略驱动，继续保持姿态；不等于卸力 |
| 电机卸力 | `robotctl robot relax --yes` | 切断关节扭矩，必须已放稳或由支架承托 |
| 停止行走意图 | 原生 JSON-RPC `robot.stop` | 清零速度，保留策略；见第 10 节 |

**两版都没有 `robotctl robot stand`、`robotctl robot walk`、`robotctl robot stop`，也没有 `robotd --check-config`。** `robotd --params FILE` 会启动 daemon；不要把它当成只读检查，更不能在已有 robotd 占用总线时再启动一个。`robotd init` 是另一条直接运行 daemon 的硬件路径，本指南使用已经运行的 daemon 和 `robotctl robot init`。

命令依据：[0.16.3 CLI](https://github.com/pollen-robotics/microduck/blob/daemon-v0.16.3/robotctl/src/main.rs)、[0.17.8 CLI](https://github.com/pollen-robotics/microduck/blob/daemon-v0.17.8/robotctl/src/main.rs)、[robotd 参数与 RPC](https://github.com/pollen-robotics/microduck/blob/daemon-v0.17.8/robotd/src/main.rs)。

## 2. 先记录设备上的真实配置

以下在 **ZERO 的 SSH 终端**运行。先完成已有任务的停止，把机器人放在稳定支架上。不要让手柄、netpadd、远程 Web 控制和自主任务同时发送运动指令。

```bash
uname -m
python3 --version
command -v robotctl
readlink -f /opt/robot/daemon/current
robotctl version --json
robotctl health --json
robotctl configure --file /etc/robot/robotd.toml --list --json
systemctl cat robotd mediad tofd updaterd
systemctl --no-pager status robotd mediad tofd updaterd
```

目标板应为 `aarch64`，Python 为 3.11 或以上。逐项记录 `systemctl cat` 中的自定义参数和 drop-in，特别是串口、`--params`、相机 `--rotate` / `--flip-in-pipeline` 及额外的服务权限。后面的示例采用官方默认路径；有定制路径时所有读写入口必须同时改成真实值。

### 2.1 原生路径

| 内容 | 官方默认路径 |
|---|---|
| 原生主配置 | `/etc/robot/robotd.toml` |
| 原生更新配置 | `/etc/robot/updater.toml` |
| 原生 release | `/opt/robot/daemon/releases/<release>/` |
| 当前 release 链接 | `/opt/robot/daemon/current` |
| 原生程序 | `/opt/robot/daemon/current/bin/` |
| `robotctl` 常用入口 | `/usr/local/bin/robotctl`，指向当前 release |
| 机器人 IPC | `/run/robotd.sock` |
| 相机 IPC | `/run/mediad/media.sock` |
| ToF IPC | `/run/tofd/tof.sock` |
| 原生进程身份 | `/run/<service>/identity.json` |
| 插件外置校准 | `/etc/robot/robot-calibration.toml`，由本次配置选择 |
| 校准实际生效证明 | `/run/robotd/calibration-effective.json`，补丁提供 |
| 相机实际生效证明 | `/run/mediad/camera-effective.json`，补丁提供 |

原生 `robotd.service` 的默认启动行为是：

```ini
ExecStart=/opt/robot/daemon/current/bin/robotd --socket /run/robotd.sock
User=root
Group=robot
```

`mediad` 使用独立的 `mediad` 账户；原生 socket 的访问权限应按已安装 unit 和 `robot` 组核对。只授予 harness 连接 socket 的权限，保持 **robotd 是唯一串口 / 舵机总线所有者**。来源：[robotd unit](https://github.com/pollen-robotics/microduck/blob/daemon-v0.17.8/robotd/systemd/robotd.service)、[mediad unit](https://github.com/pollen-robotics/microduck/blob/daemon-v0.17.8/mediad/systemd/mediad.service)。

### 2.2 备份

在 ZERO 上保留下面生成的备份目录名称，回退时会使用它。先记录，再修改：

```bash
umask 077
NATIVE_BACKUP="$HOME/microduck-native-backups/$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$NATIVE_BACKUP"
sudo cp -a /etc/robot/robotd.toml "$NATIVE_BACKUP/robotd.toml.before"
sudo cp -a /etc/robot/updater.toml "$NATIVE_BACKUP/updater.toml.before"
readlink -f /opt/robot/daemon/current > "$NATIVE_BACKUP/current.before"
robotctl version --json > "$NATIVE_BACKUP/version.before.json"
robotctl health --json > "$NATIVE_BACKUP/health.before.json"
systemctl cat robotd mediad tofd updaterd > "$NATIVE_BACKUP/units.before.txt"
printf '%s\n' "$NATIVE_BACKUP"
```

如果已经存在校准文件，同时备份它。不要删除已有配置、release 或未知的 systemd drop-in 来“清理环境”。

### 2.3 必须现场核对的硬件项

在 `[board]` 中，本目标是 `version = "zero3"`。官方 ZERO 3W 总线默认为 `/dev/ttyS2`；实际设备树、接线和定制 unit 的 `--port` 应一致。

15 个关节的数组顺序与总线 ID 不是按数字排序：

| 数组索引 | 关节 | 总线 ID |
|---:|---|---:|
| 0 | `left_hip_yaw` | 20 |
| 1 | `left_hip_roll` | 21 |
| 2 | `left_hip_pitch` | 22 |
| 3 | `left_knee` | 23 |
| 4 | `left_ankle` | 24 |
| 5 | `neck_pitch` | 30 |
| 6 | `head_pitch` | 31 |
| 7 | `head_yaw` | 32 |
| 8 | `head_roll` | 33 |
| 9 | `mouth` | 34 |
| 10 | `right_hip_yaw` | 10 |
| 11 | `right_hip_roll` | 11 |
| 12 | `right_hip_pitch` | 12 |
| 13 | `right_knee` | 13 |
| 14 | `right_ankle` | 14 |

本插件适配的是已有 **总线 IMU，ID 200**；没有提供任意 IMU ID 重映射，也没有把 I²C IMU 自动接入这条控制链。ZERO HAT 上的头部 IMU 与策略使用的主 IMU不能混为一项。必须核实 MCU 输出原始计数的单位、满量程与固件换算规则；`custom_raw_div_16_4` 只有在固件确实约定每 °/s 为 16.4 个计数时才能选。芯片名称本身不能证明这个约定。

官方 `[bus].fast_sync_read` 的启用还依赖 XL330 固件与 IMU MCU 对 0x8A 的支持；持续全总线读取超时时，按原生配置说明核对固件和该开关，不能用伪造健康值让任务继续。来源：[bus 配置](https://github.com/pollen-robotics/microduck/blob/daemon-v0.17.8/robotd-params/src/lib.rs)、[插件校准定义](../integrations/microduck_tools/upstream/calibration/common/duck_calibrate.py)。

## 3. 新板与已有原生系统

**已经能运行 0.16.3 或 0.17.8 的设备跳到第 4 节。** 若 ZERO 尚无可用原生系统，先按固定 tag 的 [原生逐步安装指南](https://github.com/pollen-robotics/microduck/blob/daemon-v0.17.8/docs/robot/install-by-hand.md) 完成系统镜像、串口设备树、网络、运行库和原生服务安装，再添加本插件。

新板先在电脑创建仅用于 bring-up 的固定源码 checkout。将版本设成统一配置 `config/deployment.local.toml` 的 `[robot].version` 所选版本；下面以 0.17.8 为例。该目录与后面的补丁构建目录分开，避免尚未克隆就引用脚本：

```bash
export NATIVE_VERSION=0.17.8
export NATIVE_TAG="daemon-v${NATIVE_VERSION}"
export BRINGUP_REPO="$HOME/microduck-bringup-${NATIVE_VERSION}"
git clone --branch "$NATIVE_TAG" --depth 1 \
  https://github.com/pollen-robotics/microduck.git "$BRINGUP_REPO"
git -C "$BRINGUP_REPO" rev-parse HEAD
cd "$BRINGUP_REPO"
```

对照第 1 节提交号，然后复制脚本到 ZERO 的家目录；中间有重启，不放在 `/tmp`：

```bash
scp scripts/setup-board.sh scripts/migrate-network.sh scripts/install.sh \
  scripts/setup-gstreamer.sh YOUR_USER@YOUR_ZERO_IP:~/
```

ZERO 上执行一次：

```bash
getent group robot || sudo groupadd --system robot
sudo usermod -aG robot "$USER"
sudo sh "$HOME/setup-board.sh"
sudo sh "$HOME/migrate-network.sh"
sudo reboot
```

重新 SSH 连接，再执行幂等的收尾步骤和安装器：

```bash
sudo sh "$HOME/setup-board.sh"
sudo sh "$HOME/migrate-network.sh"
sudo env DUCK_REPO=pollen-robotics/microduck DUCK_REF=daemon-v0.17.8 \
  sh "$HOME/install.sh"
sudo sh "$HOME/setup-gstreamer.sh"
robotctl version --json
robotctl health --json
```

上例脚本 ref 为 0.17.8；0.16.3 的 bring-up 需取对应 tag。**`DUCK_REF` 不保证第一次安装的二进制是该版本**：这两个安装脚本的 bootstrap 都会解析 `releases/latest`。安装结束后必须核对实际版本，再通过原生更新器明确选择目标。示例：

```bash
sudo robotctl update apply daemon --version 0.17.8 --dry-run
sudo robotctl update apply daemon --version 0.17.8
robotctl version --json
```

目标为 0.16.3 时，两处都替换成 `0.16.3`。若已有该 release，可用 `sudo robotctl update select daemon 0.16.3` 选择；不要把新版本的主配置不加检查地带回旧版本。第一次安装失败于网络、镜像、运行库、策略包或原生健康检查时，先解决原生系统；harness 无法替代这些前提。

安装器会启动服务。`DUCK_NO_START=1` 也不能描述为“所有 daemon 从未运行”：原生 postinstall 可能先启动再由安装器停止，官方说明要求重启完成隔离。来源：[install.sh](https://github.com/pollen-robotics/microduck/blob/daemon-v0.17.8/scripts/install.sh)。

## 4. 电脑：固定源码并准备双版插件

下文以 Linux / macOS 的 Bash 终端为例；Windows 可在具备 Docker 与 SSH 的 WSL 环境执行。电脑与 ZERO 在同一 Wi-Fi，用已核实的 IP 和 SSH 用户名。先按 [统一部署指南](DEPLOYMENT.md) 克隆 harness。**`NATIVE_VERSION` 必须与统一配置 `config/deployment.local.toml` 的 `[robot].version` 一致**；目标版本也会写入生成的部署清单，供逐项核对。这里单独展开变量，便于原生篇独立使用。

```bash
export HARNESS_REPO="$HOME/microduck-harness"
export NATIVE_VERSION=0.17.8
export NATIVE_TAG="daemon-v${NATIVE_VERSION}"
export NATIVE_REPO="$HOME/microduck-native-${NATIVE_VERSION}"
export DUCK_BOARD='YOUR_USER@YOUR_ZERO_IP'

git clone --branch "$NATIVE_TAG" --depth 1 \
  https://github.com/pollen-robotics/microduck.git "$NATIVE_REPO"
git -C "$NATIVE_REPO" status --short
git -C "$NATIVE_REPO" rev-parse HEAD

python3 "$HARNESS_REPO/scripts/prepare_native_plugins.py" \
  --repo "$NATIVE_REPO" --target "$NATIVE_TAG" --print-diff
```

先对照第 1 节提交号，再阅读 diff。默认调用只校验和展示计划；正式写入源码需显式 `--apply`：

```bash
python3 "$HARNESS_REPO/scripts/prepare_native_plugins.py" \
  --repo "$NATIVE_REPO" --target "$NATIVE_TAG" --apply
git -C "$NATIVE_REPO" diff --stat
git -C "$NATIVE_REPO" diff --check
```

0.16.3 使用独立 checkout，把 `NATIVE_VERSION` 改为 `0.16.3` 后重做该节。不要把一个版本打过补丁的源码目录直接切换到另一个 tag，也不要对未知定制 fork 强行应用。补丁器不会构建、安装、重启服务或连接总线。

这套修改涉及 `duck-control`、`robotd-params`、`robotd`、`mediad`。**必须一起构建完整 native 二进制集合**，因为 `robotctl`、`padd`、`tofd` 等也读取共享参数；只替换 robotd，而让旧程序解析新的 `bus.calibration_path` / `media.camera_rotate`，可能导致其他服务拒绝启动。

## 5. 电脑：构建、签名、预检同一份包

采用上游 `scripts/dev-push.sh` 的完整构建和签名路径。它保留原生更新器的签名、哈希、兼容性、健康检查和自动回退。这里不手工改写 `/opt/robot/daemon/current`，也不关闭签名验证。

### 5.1 构建环境与可选本机测试

需要可用的 Rust / Cargo、Python 3、Git、SSH / SCP，以及能够运行 `linux/arm64` 容器的 Docker。两版 Cargo 声明 Rust ≥ 1.89 / edition 2024；上游 CONTRIBUTING 以 Rust 1.94.1 为代码兼容上限环境。可在已安装 rustup 的电脑上选择该工具链：

```bash
rustup toolchain install 1.94.1 --profile minimal
cd "$NATIVE_REPO"
rustup override set 1.94.1
cargo --version
docker info
ssh "$DUCK_BOARD" 'uname -m; command -v robotctl'
```

Rust 首次安装入口见 [官方 rustup](https://rustup.rs/)，Docker 安装与 ARM 容器支持按电脑平台配置。Docker 路径使用该固定 tag 的 `scripts/dev-build.Dockerfile`，其中基础镜像仍是上游定义的镜像标签；源码 tag 固定不等于容器镜像 digest 固定。正式复现实验应把工具链版本、镜像 ID 和构建输出一起保留。

如果在 Linux 主机或 ZERO 上直接运行 Rust 测试，先准备 C 依赖和 GStreamer ≥ 1.22；以 Debian 12/13 为例：

```bash
sudo apt-get update
sudo apt-get install -y build-essential pkg-config libudev-dev \
  libgstreamer1.0-dev libgstreamer-plugins-base1.0-dev \
  libgstreamer-plugins-bad1.0-dev
cd "$NATIVE_REPO"
cargo test --locked -p robotd-params -p duck-control -p mediad
```

在 ZERO 本机原生编译全部程序的命令是 `cargo build --release --locked --bins`；x86 主机上同一命令生成的是 x86 程序，不能复制给 ZERO。低内存设备可显式使用 `CARGO_BUILD_JOBS=1`。编译期间不运行自由行走任务。下文统一使用官方 ARM Docker 构建，避免混用目标架构。来源：[CONTRIBUTING](https://github.com/pollen-robotics/microduck/blob/daemon-v0.17.8/CONTRIBUTING.md)、[dev-push 指南](https://github.com/pollen-robotics/microduck/blob/daemon-v0.17.8/docs/robot/dev-push.md)。

### 5.2 使用自己的开发签名密钥

仅在你管理的开发机器人上建立这项信任。密钥生成在电脑、仓库外；已有同名密钥时复用并核对，不覆盖：

```bash
cd "$NATIVE_REPO"
cargo run --locked -p xtask -- keygen --kind dev --name harness \
  --out "$HOME/.duck-keys"
cargo run --locked -p xtask -- keycheck \
  --key "$HOME/.duck-keys/harness.dev.key" \
  --public "$HOME/.duck-keys/harness.dev.pub"
export DUCK_DEV_SECRET_KEY="$HOME/.duck-keys/harness.dev.key"
scp "$HOME/.duck-keys/harness.dev.pub" "$DUCK_BOARD:~/harness.dev.pub"
```

只复制公钥。私钥不进入 Git、统一配置文件、ZERO 或聊天记录。名称中的 `.dev.pub` 是原生更新器识别开发密钥的条件之一。

在 **ZERO** 上安装该公钥，然后编辑已有更新配置：

```bash
sudo install -m 0644 "$HOME/harness.dev.pub" \
  /etc/robot/trusted_keys/harness.dev.pub
sudoedit /etc/robot/updater.toml
```

保留其他配置，将顶层已有的 `allow_dev_keys` 设置为 `true`。开发阶段如需保持插件版本不被周期更新替换，可把顶层 `auto_apply` 显式设为 `"off"`；这是本机更新策略选择，应记录在备份旁，之后恢复为你的维护策略。不要新增重复的 TOML 键。然后：

```bash
sudo systemctl restart updaterd
systemctl --no-pager status updaterd
robotctl update status daemon
```

此处仅重启更新器，不启用机器人。信任流程依据上游 [开发板配置](https://github.com/pollen-robotics/microduck/blob/daemon-v0.17.8/docs/robot/install-dev.md) 和 [xtask 密钥工具](https://github.com/pollen-robotics/microduck/blob/daemon-v0.17.8/xtask/src/main.rs)。

### 5.3 构建并做板端 dry-run

在 **电脑**运行：

```bash
cd "$NATIVE_REPO"
export DUCK_DEV_SECRET_KEY="$HOME/.duck-keys/harness.dev.key"
scripts/dev-push.sh --docker --dry-run "$DUCK_BOARD"
git diff --check
git diff -- Cargo.lock
```

这里的 `--dry-run` **会构建、签名、复制包到 ZERO，并运行原生安装预检**，但不会切换 current 或重启 daemon。检查控制台中的签名、artifact 哈希、兼容性、必需二进制和 unit 检查；失败就停在该步修复。

默认包位于 ZERO 的 `$HOME/duck-sideload`。不要把 sideload 包放进 `/tmp` 或 `/var/tmp`：`updaterd.service` 使用 `PrivateTmp`，它看到的目录与 SSH 终端不同。若设置了 `DUCK_SIDELOAD_DIR`，后面使用同一个实际绝对路径。

上游 `dev-push.sh` 会打包完整 native 程序、unit、hook 与所需资源。安装 release 名通常为 `0.17.8-dev.local.<时间>.g<短提交>`，但从固定稳定 tag 编译的 **`hello.daemon_version` 仍来自 Cargo 的 `0.17.8`，API 仍是 44**；0.16.3 同理。harness 检查的是 hello，不会凭目录名字猜版本。保留 `git diff` / 插件计划与包哈希以区分定制构建；自报版本不是二进制认证。

来源：[0.16.3 dev-push](https://github.com/pollen-robotics/microduck/blob/daemon-v0.16.3/scripts/dev-push.sh)、[0.17.8 dev-push](https://github.com/pollen-robotics/microduck/blob/daemon-v0.17.8/scripts/dev-push.sh)、[hello 实现](https://github.com/pollen-robotics/microduck/blob/daemon-v0.17.8/robotd/src/main.rs)。

## 6. ZERO：安装已审查的包并核验

现在只安装上一节已预检的包，不再次运行构建命令产生另一份包。先停止 harness 任务与会产生运动的其他控制入口；机器人放稳或由支架承托，按第 10 节停止行走，然后再卸力：

```bash
robotctl robot relax --yes
sudo robotctl update apply daemon --from "$HOME/duck-sideload" --dry-run
sudo robotctl update apply daemon --from "$HOME/duck-sideload"
```

更新器会按 unit/hook 顺序重启相关 daemon，并执行其原生健康门。如果新程序起不来，会按原生机制回退；不要绕过失败的门控。原生健康门允许某些 `degraded` 状态通过，因而“更新成功”仍不等于“可以行走”。

```bash
robotctl version --json
robotctl health --json
robotctl configure --file /etc/robot/robotd.toml --list --json
robotctl policy list --json
systemctl --no-pager status robotd mediad tofd updaterd
journalctl -u robotd -u mediad -u tofd -u updaterd -n 120 --no-pager
```

核对所有正在运行的 native 服务已经来自新 release，而不是只有 `current` 变了、旧进程仍在。`updaterd` 和承载连接的 `btd` 可能按上游更新流程稍后重启，等待它们实际更新后再继续。原有禁用的控制服务不应为了“全绿”随意启用。

首轮还需核对：15 个测量关节、总线读取、主 IMU 新鲜度、真实策略、相机原始帧、ToF 测量，以及是否存在手柄 / WebRTC 等其他行走来源。此时仍保持 harness `EXECUTOR_ALLOW_MOTION=0`。

## 7. ZERO：原生校准 CLI

在板上的 harness checkout 中，使用被保留的上游 CLI。默认路径与统一配置 `[paths].zero_root` 一致，为 `/opt/microduck-harness`；自定义部署时使用配置里的实际绝对路径。先设本节变量；`NATIVE_TAG` 必须与已安装 daemon 对应：

```bash
cd /opt/microduck-harness
export NATIVE_TAG=daemon-v0.17.8
CAL="$PWD/integrations/microduck_tools/upstream/calibration/common/duck_calibrate.py"
ROBOTCTL=/opt/robot/daemon/current/bin/robotctl
CAPTURES="$PWD/data/native-captures"
mkdir -p "$CAPTURES/imu" "$CAPTURES/joints"
python3 "$CAL" --help
```

### 7.1 采集主 IMU 安装方向

`record` 只通过 `robotctl monitor` 读取测量，不打开串口，不向电机发命令。机器人始终由人或支架承托；若需手动移动关节，应先按原生流程卸力，不能掰动正在保持姿态的舵机。

依次准备以下五种静止姿态，每次稳定后执行对应命令。机身坐标是 X 向前、Y 向左、Z 向上；倾角为相对于直立姿态约 10°–35°，前 / 后 / 左 / 右不能弄反。

```bash
python3 "$CAL" record --out "$CAPTURES/imu/still.jsonl" \
  --seconds 3 --hz 10 --robotctl "$ROBOTCTL" --confirm-supported
```

将机身前倾并保持静止：

```bash
python3 "$CAL" record --out "$CAPTURES/imu/fwd.jsonl" \
  --seconds 3 --hz 10 --robotctl "$ROBOTCTL" --confirm-supported
```

将机身后倾并保持静止：

```bash
python3 "$CAL" record --out "$CAPTURES/imu/back.jsonl" \
  --seconds 3 --hz 10 --robotctl "$ROBOTCTL" --confirm-supported
```

将机身向左倾并保持静止：

```bash
python3 "$CAL" record --out "$CAPTURES/imu/left.jsonl" \
  --seconds 3 --hz 10 --robotctl "$ROBOTCTL" --confirm-supported
```

将机身向右倾并保持静止：

```bash
python3 "$CAL" record --out "$CAPTURES/imu/right.jsonl" \
  --seconds 3 --hz 10 --robotctl "$ROBOTCTL" --confirm-supported
```

若之前没有软件校准、且当前使用官方已验证的映射与陀螺换算，可保留关节映射，只求安装方向：

```bash
python3 "$CAL" solve --captures "$CAPTURES" --target "$NATIVE_TAG" \
  --board zero3 --joint-mode keep --imu-mode measured --gyro-profile keep \
  --out data/robot-calibration.local.toml
```

**已有校准时**，在 solve 命令中额外提供 `--current-calibration /etc/robot/robot-calibration.toml`，以正确还原当前映射；省略它会把“没有提供旧校准”当作官方初始映射。默认 `keep` 也不能证明当前 MCU 的陀螺单位已核对。采集漂移、正交关系或安装修正超过门槛时，先重采或检查机械安装，不把 `--accept-stand-tilt` / `--accept-large-mount` 当常规参数。

### 7.2 关节零点与方向

若已有机械装配与关节映射正确，保持 `--joint-mode keep`。需要重新求解时，按第 2.3 节的 **全部 15 个关节**分别采集机械零姿态和已知正方向参考姿态：

```bash
python3 "$CAL" record --out "$CAPTURES/joints/zero_left_hip_yaw.jsonl" \
  --seconds 3 --hz 10 --robotctl "$ROBOTCTL" --confirm-supported
```

手动把该关节沿机器人模型约定的正方向移动 3°–25°、稳定后：

```bash
python3 "$CAL" record --out "$CAPTURES/joints/positive_left_hip_yaw.jsonl" \
  --seconds 3 --hz 10 --robotctl "$ROBOTCTL" --confirm-supported
```

对其余 14 个名称重复两次采集，再将 solve 的 `--joint-mode` 改为 `measured`。仅有前面一组样本不能计算完整校准；工具会拒绝缺失的文件。正方向必须来自实际关节模型，不是“所有电机顺时针”。零点偏差过大应修正舵盘装配。此工具不修改 Dynamixel EEPROM 或 Homing Offset。

### 7.3 已验证的陀螺换算

只改已查明的 MCU 计数换算、不重采安装方向时，可以使用 `gyro`。以下 **仅在核实固件仍输出 ST 500 dps 原始计数时**适用：

```bash
python3 "$CAL" gyro --target "$NATIVE_TAG" --board zero3 \
  --gyro-profile st_500dps \
  --current-calibration /etc/robot/robot-calibration.toml \
  --out data/robot-calibration-gyro.local.toml
```

第一次没有旧文件时省略 `--current-calibration`；这会使用官方初始关节映射和 ZERO 安装方向，不能替代实测校准。`st_500dps` 使用 `1 / 0.0175` 个计数每 °/s；`custom_raw_div_16_4` / `manual` 必须同时给出 `--confirm-firmware-scale`，manual 还需 `--gyro-raw-counts-per-dps` 的实际数值。选择配置只改变主机端换算，不会配置 MCU 的满量程寄存器。

### 7.4 检查、安装、人工重启

选择已核对的生成文件；以下采用第 7.1 节输出：

```bash
python3 "$CAL" check --calibration data/robot-calibration.local.toml \
  --target "$NATIVE_TAG"

python3 "$CAL" install --generated data/robot-calibration.local.toml \
  --target "$NATIVE_TAG" --config /etc/robot/robotd.toml \
  --dest /etc/robot/robot-calibration.toml
```

`install` 默认 dry-run。审阅数组长度、方向、四元数、target 和陀螺 profile 后再写入：

```bash
sudo python3 "$CAL" install --generated data/robot-calibration.local.toml \
  --target "$NATIVE_TAG" --config /etc/robot/robotd.toml \
  --dest /etc/robot/robot-calibration.toml --apply
```

非默认陀螺 profile 的 `install --apply` 需要补充 `--confirm-firmware-scale`；只读 `check` 没有这个参数。工具会备份并原子更新文件，在主配置 `[bus]` 中写入绝对路径 `calibration_path`，**不会重启 robotd**。不要把 `examples/demo-*.toml` 或合成采集直接装到真机。

保持承托和卸力，先用已经升级的原生 CLI 解析主配置，再明确重启：

```bash
robotctl configure --file /etc/robot/robotd.toml --list --json
sudo systemctl restart robotd
systemctl --no-pager status robotd
robotctl health --json
cat /run/robotd/calibration-effective.json
```

`configure --list` 不替代外置校准内容检查，也不证明硬件有效。实际生效需看新 robotd 的运行证据：文件路径、target、关节数组、安装四元数与 gyro 比例等实际有效值应匹配保存值，PID 与服务当前 MainPID 一致，且没有加载错误。校准回执不包含文件哈希；相机回执另有配置字节哈希，见第 9 节和 [插件运行检查](plugins-runtime.md)。修改或重启后不自动恢复 torque / 任务；重新走人工验收。

### 7.5 统一部署生成的原生配置覆盖项

完成真实校准文件安装后，统一部署生成的 `zero/native-overlay.toml` 可通过仓库脚本合并到现有主配置。把下面路径换成你复制到 ZERO 的实际生成目录；先预览，再显式应用：

```bash
python3 scripts/apply_native_config.py --config /etc/robot/robotd.toml \
  --overlay deploy/generated/zero/native-overlay.toml --target "$NATIVE_TAG"
sudo python3 scripts/apply_native_config.py --config /etc/robot/robotd.toml \
  --overlay deploy/generated/zero/native-overlay.toml --target "$NATIVE_TAG" --apply
```

该脚本只合并 `bus.calibration_path` 与 `media.camera_rotate`，并验证目标校准文件已经安装、合法且匹配 target。它不生成实测数据、不覆盖其他原生参数、不重启服务。自定义陀螺 profile 的应用同样要显式 `--confirm-firmware-scale`。合并后根据实际变更人工重启 robotd / mediad，并分别检查运行证据；不要把保存结果当成已加载结果。

## 8. ZERO：插件式 Web 配置入口

Web 是上述外置配置的编辑入口，默认只监听 localhost，端口 **8768**，与电脑 Gateway 的 8765、ZERO executor 的 8766 分开。普通校准保存不自动重启 robotd。

```bash
cd /opt/microduck-harness
sudo python3 scripts/tools_web.py --target daemon-v0.17.8 \
  --config /etc/robot/robotd.toml \
  --dest /etc/robot/robot-calibration.toml \
  --runtime /run/robotd/calibration-effective.json \
  --camera-runtime /run/mediad/camera-effective.json \
  --robotctl /opt/robot/daemon/current/bin/robotctl \
  --listen 127.0.0.1 --port 8768
```

0.16.3 改相应 target。使用 sudo 是为了该本机操作员工具能写 `/etc/robot`，以及在显式启用相机应用后管理 mediad；它不应作为无认证的局域网公共服务。

电脑另开终端：

```bash
ssh -N -o ExitOnForwardFailure=yes \
  -L 127.0.0.1:8768:127.0.0.1:8768 YOUR_USER@YOUR_ZERO_IP
```

打开 <http://127.0.0.1:8768/>，检查它显示的是真机进程和实际 runtime。保存后的“文件已更新”与“运行中已生效”是两种状态；重启 robotd 并读到匹配证据后才算后者。真机流程不用 `--demo` 或开发用 `--allow-inactive` 绕过验证。

## 9. 相机四向配置与 ArUco 的边界

相机配置支持 `0 / 90 / 180 / 270` 度顺时针修正。未显式配置时跟随原生板型；官方 ZERO 3W 板型的默认值是 90 度。选择实际适合你安装的角度，不把默认值当作实物证据。

需要从 Web 明确应用相机设置时，停止上一节 Web 进程，再用同样命令附加：

```text
--camera-web-port 8080 --enable-camera-apply
```

UI 会要求单独确认；该动作保存 `[media].camera_rotate` 并 **只重启 mediad**，期间相机 / WebRTC 会短暂中断，不重启 robotd。随后检查：

```bash
systemctl show mediad -p MainPID -p ActiveState
cat /run/mediad/camera-effective.json
journalctl -u mediad -n 60 --no-pager
```

原生 mediad 若通过 unit 中的 `--rotate` 显式指定角度，CLI 优先于配置；存在 `--flip-in-pipeline` 时也要核对实际像素方向与元数据，避免旋转两次。不要静默覆盖已有 unit 参数。

**ArUco 的 K / 畸变参数要对应实际送入视觉算法的原始像素。** 本 harness 的 executor 转发原生 UYVY 和旋转元数据，不为画面好看而修改图像。仅改显示方向不等于改内参；若改变传感器安装、实际像素变换、分辨率或裁剪，须重核内参与外参。完成相机设置后再按 [ArUco 放置指南](aruco-placement.md) 和 [统一部署指南](DEPLOYMENT.md) 采集、标定、打印与摆放。

## 10. 首次 init、enable、停止和卸力

本节是人工真机验收，不由 GLM、AgentJev 或配置页面代替。只有在总线、15 关节方向、IMU 单位 / 姿态、策略、ToF 与工作区域核对完成后执行。首次保持机械承托，手边有实际的断电 / 接管方式。

先读状态：

```bash
robotctl health --json
robotctl policy list --json
robotctl robot mode --json
robotctl monitor --hz 10 --json
```

Ctrl-C 只结束 monitor。`mode=walk` 不说明策略已经 enabled；以状态流中的实际 policy / 健康及后续 harness `/state` 为准。

### 初始姿态

在支架上进行关节方向和 home 姿态核验：

```bash
robotctl robot init
```

这条命令会移动所有关节，但不提供平衡。确认没有错误方向、机械干涉或异常负载；出错时按原有硬件操作流程处理，不连续重试。

### 启用已有策略

按已有机器人站立流程，在合适支撑和场地条件下：

```bash
robotctl robot enable
```

确认新鲜状态中的 policy 进入已验证的 `stand` / `walk`，机身实际稳定。harness 启动不会自动执行这条命令。原生短距离行走试验通过统一指南中的有界 executor 完成；不提供绕过截止时间的裸 `robot.move` 循环。

### 停止行走

正常自主任务使用工作台“停止”或 executor 的 `/stop`，这样任务与票据也会失效。若需在 ZERO 本机独立清零原生行走速度，以下是实际的 Unix socket JSON-RPC，而不是不存在的 CLI 子命令：

```bash
python3 - <<'PY'
import json
import socket

request = {"jsonrpc": "2.0", "id": 1, "method": "robot.stop", "params": {}}
with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
    connection.settimeout(2.0)
    connection.connect("/run/robotd.sock")
    connection.sendall((json.dumps(request) + "\n").encode())
    with connection.makefile("rb") as stream:
        reply = json.loads(stream.readline())
if reply.get("id") != 1 or "error" in reply:
    raise SystemExit(f"停止请求未确认：{reply}")
if reply.get("result", {}).get("accepted") is not True:
    raise SystemExit(f"停止意图未被接受：{reply}")
print("robot.stop 已接受；仍需状态和现场确认物理停止。")
PY
```

原生 `robot.stop` 是独立可用的停止方法，不要求先通过版本握手。它清零行走意图，仍由策略保持身体，不证明脚已经停止，也不能取消 `ground_pick` 等已启动的一次性技能。其他控制客户端若仍在发送速度，可以再次覆盖停止意图；先结束当前任务和并行控制源，再进行接管。该本机 RPC 本身不修改 harness 的任务 epoch。

### 退出策略与卸力

```bash
robotctl robot enable --off
```

以上退出策略但继续保持姿态。放稳 / 支架承托后，才卸力：

```bash
robotctl robot relax --yes
```

`systemctl stop robotd` 不是行走停止或安全卸力的同义命令。硬件错误恢复可能用到 `robotctl robot reboot-motors`，但这会先卸力、再重启指定电机或全部电机；应先查清过载、过热、装配或接线原因，不能由 agent 自动反复重启。

## 11. 配置撤销、回退与重新升级

### 11.1 只撤销软件校准

先停止自主任务，承托并卸力。移除外置校准指针默认仍为 dry-run：

```bash
python3 "$CAL" remove --config /etc/robot/robotd.toml
sudo python3 "$CAL" remove --config /etc/robot/robotd.toml --apply
robotctl configure --file /etc/robot/robotd.toml --list --json
sudo systemctl restart robotd
```

这不会修改舵机 EEPROM，也不会自动删除采集与校准文件。它只撤销校准指针，不负责移除相机扩展字段。

### 11.2 回退到未经补丁的官方版本

必须先恢复旧程序能解析的主配置。旧版官方程序不认识本插件新增的 `bus.calibration_path` / `media.camera_rotate`；仅回退二进制可能让多个服务一起加载失败。

将 `NATIVE_BACKUP` 设为第 2.2 节实际备份目录；先比较差异，若后来有其他有效修改，逐项保留，不盲目覆盖：

```bash
export NATIVE_BACKUP="$HOME/microduck-native-backups/YOUR_BACKUP_TIMESTAMP"
sudo diff -u "$NATIVE_BACKUP/robotd.toml.before" /etc/robot/robotd.toml
```

确认该备份仍适合恢复后，在任务已停止、机器人承托并卸力的状态执行：

```bash
sudo cp -a "$NATIVE_BACKUP/robotd.toml.before" /etc/robot/robotd.toml
sudo robotctl update rollback daemon
robotctl version --json
robotctl health --json
```

`rollback` 回到更新器记录的上一个 release；若已连续更新多次，改用 `robotctl update status daemon` 先确认，再以 `sudo robotctl update select daemon <已安装版本>` 选择第 2.2 节记录的已知 release。不要凭“上一个版本应该是某版”猜测。

如果官方稳定包尚未保留在板上，走明确版本的原生获取与安装：

```bash
sudo robotctl update apply daemon --version 0.17.8 --dry-run
sudo robotctl update apply daemon --version 0.17.8
```

回到 0.16.3 时改相应版本。恢复后按你的维护策略处理 `allow_dev_keys` / `auto_apply`，保留未知的原有公钥；撤销本次信任只涉及本次新增的 `harness.dev.pub`。任何回退或重启后都保持任务停止，重新核验物理状态，再由人工启用。

### 11.3 在两版之间切换

每个 tag 使用独立原生源码 checkout、独立补丁构建和对应 target 的校准文件。切换顺序为：停止任务并卸力 → 备份当前配置 → 暂时移除插件扩展字段或使用目标兼容配置 → 安装目标 tag 的补丁包 → 检查实际版本 / API → 生成或重新核对该 target 的校准 → 安装配置并人工重启 → 完成只读与真机验收。

随后把统一配置中的 daemon target / executor 版本选择同步修改。不能只改环境变量让一个实际运行 0.16.3 的 daemon 被当作 0.17.8。

## 12. 接回统一部署前的验收条件

完成本指南后，回到 [统一部署指南](DEPLOYMENT.md) 启动只读 executor、视觉与 Gateway。继续之前应能给出下列实际证据：

- hello 的版本 / API 正确，各正在运行的原生服务来自本次已签名 release。
- 主配置、校准文件与目标 tag 匹配；校准运行证明对应当前 PID 和有效数值，相机运行证明另核对配置字节哈希。
- 15 个关节的测量与机械方向一致，主 IMU 单位、坐标和新鲜度已实测；没有用 head IMU 或模拟值替代。
- 实际相机像素、内参、动态外参与 ArUco 地图一致，ToF 可用且新鲜；缺失时保持停止。
- 原生 `init`、策略启用、停止、卸力及人工接管已分别验证；停止请求被接受与物理停稳分开记录。
- 其他运动客户端已停用或有明确仲裁，重启后不自动上力、不自动恢复任务。

GLM Base URL / 模型 ID / 认证、AgentJev 门槛和 nanoMuse 接入属于电脑上的运行配置，可随任务需要修改；它们不改变这里的物理限速、原生权限、关节映射和硬件验收结论。
