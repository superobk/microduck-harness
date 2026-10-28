# ZERO 3W 原生 daemon 与 microduck-tools 插件部署

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
