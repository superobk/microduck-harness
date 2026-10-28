# 随时修改 GLM 与 AgentJev 配置

模型服务是工作台中的运行时配置。Base URL、实际模型 ID、认证方式、API key、超时、JSON 模式与 AgentJev 参数都可以在 gateway 运行期间修改，不需要重启 gateway，也不要求先提供模型 API 才能打开页面。

保存配置不会发出模型测试请求，不会启动、停止或取消机器人任务。服务是否真正兼容 Chat Completions、模型 ID 是否存在、GPU 是否就绪，需要接入你的 API 后验证。`ready=true` 仅表示配置具备发起请求所需的字段，不是模型可用性或真机安全认证。

## 1. 从工作台设置

第一次启用配置写入时，为 gateway 设置一个本地操作者令牌。例如将 `GATEWAY_TOKEN` 放入只由部署用户读取的环境配置，然后启动：

```bash
duck-harness gateway
```

打开 `http://127.0.0.1:8765`，在“连接设置”输入这个令牌并点击“连接工作台”。随后在独立的“模型服务设置”区域填写：

| 设置 | 填写方式 |
| --- | --- |
| 决策模式 | `hybrid` 为 AgentJev 快速选择、GLM 规划和重新判断；`glm` 为直接使用 GLM；`demo` 只用于模拟设备的演示规则 |
| GLM Base URL | 你的兼容服务地址，例如 `http://192.168.1.30:8000/v1`；也接受完整 `/chat/completions` 地址 |
| 实际模型 ID | 服务实际接受的精确字符串；默认 `GLM5.3-Flash` 是可修改的配置名 |
| 认证方式 | 无认证、环境变量引用，或本地密钥文件 |
| GLM 超时 | 单次模型请求总超时，必须大于 0 且不超过 120 秒 |
| JSON 响应模式 | 默认开启 `response_format={"type":"json_object"}`；兼容服务不支持时关闭，返回 JSON 的结构校验仍保留 |
| AgentJev URL | 默认 `http://127.0.0.1:8149`；仅 hybrid 模式使用 |
| AgentJev 参数 | 超时 2 秒、选择概率下限 0.85、第一第二候选差距下限 0.20、连续快速选择上限 8，均可修改 |

点击“保存模型设置”后，页面显示当前版本与已保存版本。未经保存的编辑不会被状态轮询覆盖。“重新读取”会用服务器保存的值替换表单内容；两个页面同时编辑时，过期版本的保存请求返回 409，避免静默覆盖。

默认的无令牌演示仍可打开、观察并执行演示任务，但不能通过 HTTP 修改模型配置。为配置写入设置一次 gateway token 后，后续模型参数修改全部在页面内完成。gateway 的令牌和监听地址属于部署参数，不在模型设置页面里修改。

## 2. 当前任务与下一任务的边界

每次启动任务时，gateway 固定该任务使用的模型配置版本、GLM 客户端、认证凭证和 AgentJev 参数。任务回执包含 `model_config_revision` 和 `model_id`，因此能够核对某次动作序列使用了哪个模型。

| 保存时的状态 | 生效方式 |
| --- | --- |
| 没有运行中的任务 | 立即准备新客户端；下一任务直接使用新版本 |
| 当前任务在规划、推理、执行或验证 | 新配置保存为待启用版本；当前任务继续使用原来的客户端和参数 |
| 当前任务结束后又启动任务 | 在接受新任务之前启用最新保存版本并关闭旧客户端 |
| 连续保存多个版本 | 当前任务保持原版本；下一任务使用最后一次成功保存的版本 |
| 保存后 gateway 重启 | 加载最新保存版本；原本未完成的任务仍按已有规则标记为中断，不自动恢复 |

任务执行期间不会关闭它正在使用的客户端，不会中途切换端点或凭证。已经保存、尚未启用的版本会继续显示“下一任务启用”；自然结束当前任务后，也在下一次启动任务时完成切换。

若想立即终止当前任务，请使用工作台已有的停止按钮；修改模型设置本身没有隐含的停止操作。

## 3. 认证、密钥与端点变更

### 环境变量引用

推荐使用 `environment` 认证方式，例如引用 gateway 进程已有的 `GLM_API_KEY`。保存的配置只包含变量名，不包含其值。变量名必须符合 `[A-Za-z_][A-Za-z0-9_]*`。如果变量没有值，配置可以保存，但模型任务不可启动。

环境变量属于已启动进程。修改外部 `.env` 或终端中的变量不会改变正在运行的 gateway 进程环境；需要即时更换密钥时，可在工作台明确选择“写入新密钥”，或切换到该进程中已经存在的另一个变量引用。采用环境变量方式的部署更新可以由服务管理器重启应用生效。

### 本地密钥文件

工作台不会回显已有 API key，也不会填入 `********` 等占位符。密钥操作有明确语义：

| 操作 | 结果 |
| --- | --- |
| `keep`：保留 | 不修改已保存的本地密钥；密码输入框必须不提交任何值 |
| `set`：写入 | 必须提供一个新的非空密钥，并使用 `stored` 认证；空白、遮罩占位符和控制字符会被拒绝 |
| `clear`：删除 | 删除本地密钥；默认同时切换无认证，也可明确切换环境变量引用 |

只选择“无认证”会禁用认证；要删除已有本地密钥，请明确选择“删除本地密钥”。选择“环境变量引用”也不会自动清除以前保存的本地密钥。

URL 只允许 HTTP 或 HTTPS，不能包含用户名、密码、查询参数、片段、空白或无效端口。API key 必须通过认证配置提供，不要把凭证嵌进 URL。

### 更换模型服务器

当 Base URL 的协议、主机或端口发生变化，工作台不会默认把保存的密钥转发给新服务器。需要明确采取以下一种操作：

1. 选择“写入新密钥”，填写要用于新地址的密钥。
2. 选择“无认证”，必要时同时删除本地密钥。
3. 明确选择环境变量名，并在页面确认将该引用的凭证用于新地址。

同一协议、主机和端口下修改路径不属于更换服务器。本地密钥和环境变量引用都绑定其明确授权的服务器来源；即使先禁用认证、改地址，再重新选择认证，旧地址的凭证也不会被自动发送给新地址。环境变量当前没有值时也保留该绑定；更换来源后必须重新明确填写 `glm_auth_mode="environment"` 和 `glm_api_key_env`。

## 4. 持久化与启动优先级

默认文件位于数据库旁边：

```text
data/model-settings.json
data/model-settings.credentials.json
```

`model-settings.json` 保存非密钥参数、版本、密钥引用和密钥所属服务器来源。通过页面录入的密钥位于独立的 `model-settings.credentials.json`。两者在 POSIX 系统均以 0600 权限写入，读取时拒绝对组或其他用户开放的文件，也拒绝符号链接。

写入采用同目录临时文件、刷新文件、原子替换的流程。修改密钥时先写入可同时解析旧、新引用的凭证文件，再提交参数文件，最后清除退休的密钥引用。因此参数文件提交失败时，旧配置仍能解析旧密钥。若最后的凭证清理因磁盘错误失败，可能暂时留有未使用的本地密钥，后续成功保存会再次清理。

原子替换成功就是当前文件的提交点。如果替换完成之后的目录刷新失败，内存仍同步到已经写入的版本，保留上一代凭证引用，响应的 `saved.persistence_warning` 和页面会说明断电持久性尚未确认；此时检查磁盘并再次保存。不会把已经完成的替换当成未发生，从而在后续密钥轮换时破坏当前引用。

可用 `MODEL_CONFIG_FILE` 或静态配置项 `model_config_file` 指定参数文件路径；凭证文件在其旁边，名称为 `<参数文件名去掉后缀>.credentials.json`。设置 `HARNESS_DB=:memory:` 且不指定配置文件时，仅在内存保留修改，适合测试，不适合需要保存设置的部署。

启动时先加载配置文件和环境变量作为初值。如果已有运行时模型配置，则该文件中的模型字段优先。环境变量仍可用于提供它引用的密钥。运行时模型设置不会覆盖活动区域、速度、ToF 门限、相机参数、机器人地址或 gateway/executor/vision 令牌。

如果同一数据目录之前保存了 `demo` 决策模式，切换真机部署前先在页面将它改为 `hybrid` 或 `glm`。真机启动会拒绝加载保存的 demo 模型配置；也可为演示与真机分别指定 `MODEL_CONFIG_FILE`，明确隔离两个部署的设置。

同一模型配置文件由一个 gateway 实例管理。备份或迁移时，应将参数文件和凭证文件一并处理并保留访问权限；环境变量引用方式还需要在新进程中设置对应变量。不要将含密钥的文件加入版本库或分享给他人。

## 5. 操作者 HTTP API

这些接口不出现在 MCP 工具列表中。nanoMuse 或其他模型客户端只能调用已有任务与观测工具；模型不能通过 MCP 修改其服务地址、凭证或决策阈值。

### 读取

```http
GET /api/model-config
Authorization: Bearer <gateway-token>
```

返回以下结构，任何分支都不包含密钥：

```json
{
  "saved": {
    "revision": 4,
    "configuration": {
      "decision_mode": "hybrid",
      "glm_base_url": "http://192.168.1.30:8000/v1",
      "glm_model": "your-exact-model-id",
      "glm_auth_mode": "environment",
      "glm_api_key_env": "GLM_API_KEY"
    },
    "api_key_configured": true,
    "ready": true
  },
  "active": {"revision": 3, "configuration": {}, "ready": true},
  "pending": true,
  "writes_enabled": true,
  "application_policy": "next_task"
}
```

示例省略了部分常规配置字段。`saved` 是下一任务的目标配置；`active` 是当前已经应用的配置。`ready` 是本地字段检查结果，不发送网络请求。

### 更新

```http
PATCH /api/model-config
Authorization: Bearer <gateway-token>
Content-Type: application/json
```

正文仅需包含要修改的字段及从 GET 读取的 `expected_revision`：

```json
{
  "expected_revision": 4,
  "decision_mode": "hybrid",
  "glm_base_url": "http://192.168.1.30:8000/v1",
  "glm_model": "your-exact-model-id",
  "glm_auth_mode": "environment",
  "glm_api_key_env": "GLM_API_KEY",
  "glm_timeout_s": 25,
  "glm_json_mode": true,
  "jev_url": "http://127.0.0.1:8149",
  "jev_threshold": 0.85,
  "jev_margin": 0.20,
  "jev_max_streak": 8
}
```

未传入的字段保持原值。不存在“空字符串等于保留密钥”的隐含规则：替换密钥必须使用 `glm_api_key_action="set"` 和新的 `glm_api_key`；保留时不要提交 `glm_api_key`；删除时使用 `glm_api_key_action="clear"`。`null` 字段会被拒绝。

完整请求字段如下，除 `expected_revision` 外均可省略：

| 字段 | 类型与约束 |
| --- | --- |
| `expected_revision` | 必填整数，大于等于 0，必须等于最近读取的保存版本 |
| `decision_mode` | `demo`、`hybrid`、`glm`；真机不允许 `demo` |
| `glm_base_url` | HTTP(S) URL，最多 2048 字符；空字符串表示尚未配置 |
| `glm_model` | 非空模型 ID，最多 200 字符 |
| `glm_timeout_s` | 数值，`0 < 值 <= 120` |
| `glm_json_mode` | 布尔值 |
| `glm_auth_mode` | `none`、`environment`、`stored` |
| `glm_api_key_env` | 环境变量名，最多 100 字符；明确授权环境引用时与 `glm_auth_mode` 一起提交 |
| `glm_api_key_action` | `keep`、`set`、`clear`，省略时为 `keep` |
| `glm_api_key` | 仅 `set` 时提供；非空可见 ASCII，不含空白，最多 8192 字符 |
| `jev_url` | 非空 HTTP(S) URL，最多 2048 字符 |
| `jev_timeout_s` | 数值，`0 < 值 <= 15` |
| `jev_threshold` | 数值，`0 <= 值 <= 1` |
| `jev_margin` | 数值，`0 <= 值 <= 1` |
| `jev_max_streak` | 整数，`1 <= 值 <= 50` |

常见状态码：

| 状态码 | 含义 |
| --- | --- |
| 200 | 已保存；检查 `pending` 判断是否等待下一任务启用 |
| 401 | gateway 已配置令牌，但请求缺少正确令牌 |
| 403 | gateway 尚未配置令牌，因此禁用模型配置写入；或请求来自不同浏览器 Origin |
| 409 | `expected_revision` 过期，需要重新读取；启动任务时也可能表示模型配置尚不完整 |
| 422 | 字段无效、密钥操作不明确、端点更换未重新选择认证，或试图在真机模式选择 demo 决策 |

`GET /health` 保持只读，额外给出当前版本、保存版本、是否待启用、模型 ID 与配置是否就绪；不返回模型 URL、环境变量值或任何访问令牌。`GET /api/state` 和任务回执也可核对配置版本。

## 6. 尚未提供模型 API 时

模拟设备可以继续使用 `mode=demo, decision_mode=demo`。真机 gateway 使用 `mode=live`、`decision_mode=hybrid` 或 `glm`，并提供原有的 gateway、executor、vision 三个令牌后，即使 `GLM_BASE_URL` 留空也能启动页面与只读配置接口。此时模型任务返回明确的 409 配置提示，在此检查前不会创建任务或发送停止指令。

收到 API 信息后，在页面保存即可。模型接口可配置不代表其他真机前提已满足：位姿、测距、机器人状态、活动边界与每次动作前重新验证仍按现有控制流程执行。

## 7. 自动化验证范围

`tests/test_model_config.py` 使用真实 FastAPI ASGI 请求验证认证、Origin 检查、配置保存与重载、密钥脱敏、遮罩拒绝、端点变化、文件权限、提交失败恢复和无模型 API 的只读启动。另有阻塞中的本地假模型验证：当前任务保持旧客户端和配置，新任务使用新模型 ID、密钥、超时与 JSON 模式，保存过程不额外停止机器人。

这些测试不连接你的模型服务、AgentJev 服务、ZERO 3W 或实体机器人。
