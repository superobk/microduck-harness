# microduck-tools 配置插件接入

本目录把用户指定的 [superobk/microduck-tools](https://github.com/superobk/microduck-tools) 固定在提交 **`2b93cb3afc32fa343cc612ee060e94c5bdd7b01d`**。`upstream/` 是该提交全部65个受版本管理文件的原样快照；`UPSTREAM.json` 记录每个文件的SHA256和Git blob，加载桥接前会核对。原仓库在该提交没有LICENSE文件，本工程不为其重新声明许可；保留来源及原始权利归属。

上游的“插件式配置”是原生daemon的独立校准TOML与可选配置字段，配套Python工具和Web界面；它不是可以加载任意Python代码的插件市场。关节与IMU转换只在原生DynamixelIo里应用一次，harness读取转换后的状态。模型不会获得写舵机、修改校准或重启daemon的工具。

## 文件职责

| 路径 | 用途 |
|---|---|
| `upstream/calibration/common/duck_calibrate.py` | 原始只读采集、求解、校验、安装/移除TOML指针；安装默认dry-run |
| `upstream/calibration/common/apply_source_patch.py` | 原始双版本关节/IMU源码补丁生成逻辑 |
| `upstream/calibration/integration-0.16.3/web/` | 原始统一校准与相机Web，保留未经修改 |
| `native.py`、`templates/` | harness自有双版本源码适配，加入四向相机配置及实际运行回执 |
| `bridge.py` | 加载固定Web；保留原来的CSRF/备份/显式应用，增加已移植版本的相机入口 |
| `patches/` | 在真实官方固定tag源码上生成并实测应用的完整补丁，供审阅 |
| `SOURCE_VALIDATION.json` | 真实Git源码检查记录，不冒充Rust构建或设备测试 |

## 版本范围

| 原生tag | 固定上游提交 | 当前软件证据 |
|---|---|---|
| `daemon-v0.16.3` | `0b2b82f56ac1e32d69018837ac1cd7d4c687f5a0` | 真实源码dry-run、实际apply、反向check、diff检查；Python配置行为测试 |
| `daemon-v0.17.8` | `942e58e3aa6dbb28e7037203fff11866122c7567` | 同上；相机部分由本目录从0.16.3扩展移植 |

上游README原本只把统一相机入口视为0.16.3基线，不能仅看到0.17.8目录就当作完整支持。本适配在两个真实tag逐项核对并生成补丁，且保留这个区别。生成的Rust包含配置校验、原始gyro比例、15关节映射、camera rotation及四向PNG的原生测试，但当前执行环境没有Rust工具链，**未编译、未运行Rust测试、未安装实机**。构建机仍必须按统一原生部署指南完成这些步骤。

## 准备原生源码

从harness根目录运行；`--repo`必须指向已经检出对应官方tag的Git工作目录：

```bash
python3 scripts/prepare_native_plugins.py \
  --repo /path/to/microduck-native --target daemon-v0.17.8

python3 scripts/prepare_native_plugins.py \
  --repo /path/to/microduck-native --target daemon-v0.17.8 --print-diff

python3 scripts/prepare_native_plugins.py \
  --repo /path/to/microduck-native --target daemon-v0.17.8 --apply
```

前两条只检查和显示；最后一条仅修改经过核对的源码。脚本验证HEAD与tag提交、原始文件内容、未提交的目标文件变更和符号链接，先对所有文件运行`git apply --check`，再应用同一个完整patch。不会构建、下载依赖、安装程序、重启服务或控制电机。已有未知更改会被保留；目标文件已变更时要求使用另一份干净checkout，不自动reset/stash。

校准插件新增`[bus].calibration_path`，读取schema1/2配置。相机插件新增`[media].camera_rotate`，其值为0/90/180/270或不设置。未设置时仍使用原生板型默认值。完整构建/部署步骤由仓库的原生部署指南统一说明。

## 启动配置工作台

默认端口为**8768**，避免与harness Gateway的8765冲突。仍只允许localhost绑定，通过SSH转发使用。

```bash
python3 scripts/tools_web.py --target daemon-v0.17.8 \
  --config /etc/robot/robotd.toml \
  --dest /etc/robot/robot-calibration.toml \
  --port 8768
```

配置账户需要对目标文件的明确权限；不要用改变总线权限来解决配置写入问题。`--help`保留上游参数含义。默认不启用相机应用；`--enable-camera-apply`允许操作者经页面再次确认后只重启mediad，robotd重启仍在维护终端执行。`--allow-inactive`是上游保留的开发文件编辑入口，实际部署不应默认设置。

离线界面检查可用`--demo --no-telemetry`，并将`--config`指向快照中的`calibration/integration-0.16.3/evidence/demo/robotd.toml`，`--dest`指向临时文件。DEMO显示合成状态，不把它视为原生运行验证。

## 保存与实际生效的区别

原生校准回执为`/run/robotd/calibration-effective.json`，相机回执为`/run/mediad/camera-effective.json`。Web使用实际进程、target和来源核对回执，显示磁盘保存值和运行中的值。保存文件不等于正在运行的daemon已使用它。

相机方向默认只传递显示方向metadata，原始UYVY像素仍保持原生方向。harness ArUco使用原始图像的K/D和camera FK，不再按这个显示角度转一次。若使用原生`--flip-in-pipeline`改变像素，必须采用对应标定与外参；配置界面会拒绝在这个覆盖状态下保存方向。

`custom_raw_div_16_4`只在实际ID200固件契约确认为该原始计数比例时选用；它不会写IMU硬件量程寄存器。harness接收的gyro已经是rad/s，不会再次除以16.4。所有15关节的软件偏移和方向也由robotd执行，harness不叠加第二套映射。

## 软件回归

```bash
python3 -m unittest discover \
  -s integrations/microduck_tools/upstream/calibration/tests -v

python -m pytest -q tests/test_tools_bridge.py
```

上游测试使用合成采集和本地模拟HTTP。桥接测试覆盖双版本相机配置、实际PID/target校验、原配置保留、待应用状态、显式mediad重启、CSRF/Origin、默认端口和原始snapshot哈希。源码apply记录单独保留，软件结果不代表现场IMU、相机或舵机已标定。
