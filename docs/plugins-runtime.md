# 原生配置插件：保存状态与运行状态

本桥接将 `microduck-tools` 的校准及摄像头配置接入板端执行器。原生 robotd 应用关节零点、方向、IMU 安装四元数与 gyro 原始单位换算；mediad 读取相机方向。Harness **只检查和展示已经加载的结果**，不再次转换任何关节或 gyro 数据，不重启服务，也不通过这个接口写配置。

上游接口依据固定提交 [`2b93cb3afc32fa343cc612ee060e94c5bdd7b01d`](https://github.com/superobk/microduck-tools/tree/2b93cb3afc32fa343cc612ee060e94c5bdd7b01d)：

- `calibration/common/duck_calibrate.py` 的 schema 1/2、15 关节和 gyro profile 校验。
- `calibration/common/apply_source_patch.py` 发布的实际 decoder 回执。
- `calibration/integration-0.16.3/web/camera_config.py` 与相机原生补丁的运行回执。

两版原生插件补丁及工具入口由本工程的集成层提供；这个模块不以官方原版 daemon 自动具有插件能力为前提。

## 1. 配置执行器

统一部署默认开启插件检查。旧版独立启动方式保留 `false`，使未采用原生插件的既有部署可以继续显式使用原来的接口。

| Executor JSON 字段 | 环境变量 | 默认值 |
|---|---|---|
| `require_native_plugins` | `EXECUTOR_REQUIRE_NATIVE_PLUGINS` | `false` / `0`；统一部署设为 `true` / `1` |
| `native_config_path` | `NATIVE_CONFIG_PATH` | `/etc/robot/robotd.toml` |
| `calibration_runtime_path` | `CALIBRATION_RUNTIME_PATH` | `/run/robotd/calibration-effective.json` |
| `camera_runtime_path` | `CAMERA_RUNTIME_PATH` | `/run/mediad/camera-effective.json` |
| `robotd_version` | `EXECUTOR_ROBOTD_VERSION` | `auto`，或明确 `0.16.3` / `0.17.8` |

所有路径是板端本地绝对路径，不是电脑路径。只读接口不接受用户提供文件名。相关证据必须是有界的普通文件，符号链接或设备文件不会作为有效配置证据读取。

`auto` 使用原生 `hello` 中已核对的版本选择插件目标，因此先读 `/state`，再读 `/plugins`。明确版本也会与已知的实际版本核对。插件读取错误只显示固定诊断，不返回任意 TOML/JSON 原文或解析错误中的输入内容。

## 2. 查看保存与运行状态

在电脑的 SSH 转发通道已建立、执行器令牌已设置时：

```bash
curl --fail --silent --show-error \
  -H "Authorization: Bearer $EXECUTOR_TOKEN" \
  http://127.0.0.1:8766/state

curl --fail --silent --show-error \
  -H "Authorization: Bearer $EXECUTOR_TOKEN" \
  http://127.0.0.1:8766/plugins
```

局域网直连时，将地址换为 ZERO 的实际 IP。`/state` 也包含同样的 `plugins` 对象，Gateway 获取状态时会一并带回。所有插件操作均为 GET，没有配置写入或服务控制端点。

| 状态 | 含义 |
|---|---|
| `active` | 保存的配置、实际运行回执、当前 daemon 进程身份均已核对一致 |
| `saved` | 已读到合法保存配置，但缺少能够验证为当前进程的有效运行回执 |
| `pending` | 已验证运行值，但它与保存值不同，或存在命令行方向覆盖 |
| `unavailable` | 配置本身不可读取、不合法、版本未知，或证据不足 |
| `disabled` | 未要求插件门控；不能解读为原生插件已经生效 |

公共结构如下（省略实际数值）：

```json
{
  "required": true,
  "ready": true,
  "status": "active",
  "generation": "configuration-and-process-generation-sha256",
  "calibration": {"status": "active", "saved": {}, "active": {}, "reasons": []},
  "camera": {"status": "active", "saved": {}, "active": {}, "reasons": []}
}
```

当 `required=false` 时，`ready=true` 表示这个可选门控没有阻止旧部署，**不表示插件运行证据通过**；应看 `required`、组件 `status` 与原因。即使插件 `active`，机器人策略健康、新鲜定位与 ToF 等其他要求仍需独立通过。

## 3. 怎样证明配置已经加载

校准检查读取原生 TOML 的 `[bus].calibration_path`，再读取它指向的真实校准文件，核对 schema、固定目标 tag、完整 15 个关节零点和方向、单位四元数及 gyro profile。schema 1 的原生默认敏感度为 `1 / 0.0175` counts/(degree/s)；schema 2 按保存的合法 profile 核对，包括明确的自定义 16.4 比例。这个检查不会猜测你的 IMU 固件实际使用哪个比例。

运行回执必须来自 `source="robotd:DynamixelIo:live"`、`bus_verified=true` 的原生 decoder，并与保存值、文件路径和目标 tag 一致。该回执是 native I/O 已打开并核验总线后报告的内存状态；不能把后来修改的 TOML 当作已经运行的参数。

相机回执必须来自 `source="mediad:camera:live"`、`pipeline_started=true` 的对应 tag，使用同一原生配置文件，图像尺寸及四向角度合法。缺省旋转按 ZERO 3 的 90° 或 beta 的 0° 解析；明确配置优先。命令行 `--rotate` 覆盖会显示为 `pending`，需要先统一配置入口。

两个回执都核对：

1. 回执 PID 与 systemd 对应 `robotd.service` / `mediad.service` 的 MainPID 一致。
2. 优先读取 `/proc/<PID>/exe`，确认它是对应 daemon。官方 unit 分别以 root / mediad 账户运行，而执行器可以是普通 `robot` 组账户；当内核明确返回 `EACCES` / `EPERM` 时，采用下面严格的 systemd 身份分支。进程消失、执行文件错误或其他读取失败不会进入这个分支。
3. 回执文件时间不早于这个进程的实际 start time，且不在未来。这会拒绝进程重启或 PID 重用留下的旧文件。

低权限分支要求同一 systemd 属性快照中 `Type=exec`、`ActiveState=active`、`SubState=running`，且 `MainPID`、`ExecMainPID`、唯一 `ExecStart` 的运行 PID 与回执 PID 全部相等。命令必须直接执行对应官方路径 `/opt/robot/daemon/current/bin/robotd` 或 `/opt/robot/daemon/current/bin/mediad`，`argv[0]` 必须相同；shell、包装器、其他路径、多个命令和无法明确解析的输出均拒绝。每次仍读取内核 `/proc/<PID>/stat`，核对进程名、存活与开始时间。如果 `/proc/stat` 也被隐藏，门控继续失败，不会为了通过检查要求执行器提权。

运行身份中的 `executable_evidence` 标明使用 `proc_exe` 还是 `systemd_exec`。这是官方 unit 的直接启动约定，不是仅凭配置中的文件名猜测实际进程；自定义启动包装器不能使用低权限分支，需要恢复官方 unit 或另行审查可验证的身份约定。

回执是启动快照，不是周期心跳。一个持续运行多天的进程，其启动时回执仍可有效；不能加一个几秒的 TTL 把它永久判过期。传感器与控制循环仍按原来的毫秒级新鲜度检查。systemd 属性快照只缓存 200 ms，每次检查都会重读 `/proc` 的执行文件与进程开始时间。插件证据检查使用独立线程与 250 ms 本地预算，超时返回未就绪，避免阻塞停止接口。

官方安装器将主配置设为 0644，校准工具安装校准文件时也设为 0644；官方服务默认 umask 下的运行回执可供普通账户读取。自定义文件权限或 ACL 时，应在每次工具保存后以执行器实际账户检查四个配置/回执文件的可读性。上游原子写入保留文件 mode，但不保证保留既有 owner/group；例如自行改成 0640 后使用 sudo 保存，可能需要重新设置 `robot` 组的读取权限。桥接不会修改这些权限。

此身份核对针对同板部署误配和旧回执，不是对恶意 root、伪造二进制或固件硬件真实性的认证。

## 4. 对动作与相机的影响

`require_native_plugins=true` 时，任一插件未生效都会令 `/state.healthy=false`，不发放新的 `state_token`，其 TTL 为 0。每张有效动作票据还绑定校准、相机及原生进程的 generation；票据发放后发生变化，旧票据不能用于运动。

动作执行前和每个速度刷新周期都重新检查。执行中保存了不同校准或切换了进程，即使新进程最终加载成功，当前脉冲仍会因 generation 变化停止。退出路径继续发送独立的 `robot.stop`；缺少插件证据不会阻止停止请求。检查消耗的时间也计入原生测量年龄及既有动作期限；脉冲队列延迟不能延长从票据时刻计算的绝对观测截止时间。

ArUco 的 K/D 和原生 camera FK 采用原始 UYVY 像素约定。正常 `camera_rotate` 只是显示与帧元数据，桥接不会再次旋转图像。若回执报告 `flip_in_pipeline=true`、缺少该标志，或帧旋转元数据不一致，则不允许以当前原始 K/FK 合约进行动作。使用这种像素变换需要明确重建视觉几何，而不能仅修改显示角度。

修改校准后，按统一部署指南显式检查、重启相应 native daemon，再确认 `active`。桥接不会自动重启、启用电机、重新开始中断任务，也不会发送串口指令。

## 5. 软件验证范围

`tests/test_plugins.py` 使用实际临时 TOML/JSON 与可控制的进程身份边界，覆盖两个 tag、schema 1/2、缺失与假回执、15 关节和 NaN 检查、相机元数据、PID 复用、认证、票据绑定以及执行期间配置改变后的最终停止。身份测试模拟 systemd 及内核权限返回，检查低权限分支拒绝 shell、错 PID、错执行路径与不可读的进程状态。Linux 的 root 测试环境还运行一个临时 `sleep` 合成进程，并让读者子进程降为另一 UID，验证真实内核拒绝 `/proc/exe` 后仍能使用受约束的 systemd fixture 与真实 `/proc/stat`；非 root 环境跳过这一项。测试不启动或停止真实服务，不接触硬件。还与既有原生 Unix socket、执行器及跨层契约测试联合运行。

这些软件证据不替代实际 ZERO 3W 的 daemon 构建、真实校准数据、模型、相机与运动验收。
