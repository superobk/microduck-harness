# nanoMuse Desktop 接入

nanoMuse 负责目标输入与对话；独立 Gateway 负责机器人任务生命周期。关闭聊天窗口、取消模型生成或切换模型不会自动等价于停止机器人。停止物理任务请调用 Gateway 的停止／取消工具或工作台的停止按钮。

## 1. 添加自有 GLM5.3-Flash

在 nanoMuse Desktop 的模型设置中添加自己的 OpenAI 兼容服务；当前界面在自有密钥流程的 **More ways** 中提供手动填写 OpenAI-compatible endpoint 的入口。填写你部署的服务地址、API Key 与服务实际公布的模型 ID，然后把它选为对话模型。`GLM5.3-Flash` 是本工程的配置默认名；如果你的推理服务公布 `glm-5.3-flash` 或另一个名字，应以服务实际 ID 为准。

Gateway 的 `GLM_BASE_URL`、`GLM_MODEL` 与 `GLM_API_KEY` 也要指向这个服务。nanoMuse 的对话模型配置与 Gateway 的任务规划模型配置是两个独立客户端，修改一个不会自动修改另一个。结构化视觉数据可以直接进入文本规划；首版不要求 GLM 具备图像输入。

## 2. 添加机器人 MCP 服务

先运行本工程 Gateway。默认 MCP 地址：

```text
http://127.0.0.1:8765/mcp
```

在 nanoMuse Desktop 打开 **Settings → Connectors → Services → Connect by address**：

1. 名称填写 `microduck`，地址填写上面的 URL。
2. 在 **More options** 中填入 Gateway 的 `GATEWAY_TOKEN`，它作为 Bearer Key 使用。不要把密钥放进 URL。
3. 连接后核对工具列表，再开启需要的观测、任务与停止工具。工具在模型侧的名称会带有 `mcp__microduck__` 前缀，具体以列出的工具名为准。

当前 nanoMuse 自定义连接允许 localhost／127.0.0.1 的 HTTP。远程电脑上的 Gateway 可以经 SSH 转发到本机，或者通过带认证的 HTTPS 地址提供；不能直接假定任意局域网 HTTP 地址都能在这个界面通过检查。本工程没有自动替你配置公网入口。

本工程保留 MCP SDK 的 loopback Host 校验，因此已经可以直接使用的是同机连接与 SSH 转发。若添加 HTTPS 反向代理，还需为 MCP 明确配置相应 Host allowlist；仅改变 Uvicorn 监听地址不能使外部域名通过校验。

不需要修改 nanoMuse 源码，也不需要把整个机器人循环塞进聊天工具调用。MCP 的提交操作应快速返回任务 ID，随后查询状态；这样 nanoMuse 的单次工具超时不会成为机器人任务的执行时长。

## 3. 建议的对话约定

可以把下列要求放进机器人对话的初始指令：

> 你通过 microduck MCP 控制机器人。先读取状态与最新观测，再提交一个目标任务；提交后使用任务 ID 跟踪执行。不要使用终端、串口、GUI 遥控或网络请求绕过 Gateway。重复提交沿用同一个 request_id。只根据任务终态和观测证据报告完成，accepted 只代表接收。用户说停止时立即调用停止工具。定位过期、路径受阻或目标丢失时报告具体原因；不要编造已完成的动作。

首版可尝试的目标：

- “在配置的工作区里探索，观察周围有什么，达到步数预算后报告。”
- “寻找 `marker:7`，找到后报告它的位置。”
- “靠近 `home`，停在配置的接近距离。”
- “停止当前任务。”

演示模式中使用演示世界已有的目标名；真机使用视觉服务发布的标签或目标 ID。自由文本的理解由你的 GLM 服务完成，Jev 只在代码已经限定的动作候选中选择，不负责解释任意新技能。

## 4. 验证顺序

先用 `tools/list` 核对 MCP 已连接，再调用只读观测，确认模式、画面时间与机器人状态。接着在演示世界中提交、观察、取消目标。最后才连接已经完成硬件检查与视觉标定的真机执行器。nanoMuse 实际客户端与自有 GLM 服务还需要在你的环境完成这一轮联调；本工程的自动测试验证服务端 MCP 契约和适配器接口，不冒充已经安装并操作了你的桌面应用。

## 上游依据

核对时间：2026-10-28（北京时间）。nanoMuse Desktop 的自定义连接入口、Streamable HTTP、Bearer Key 与模型设置依据：

- [nanoMuse Desktop 说明](https://github.com/nano-muse/nanoMuse/blob/main/docs/desktop.md)
- [桌面页面与 Connectors 设置](https://github.com/nano-muse/nanoMuse/blob/main/docs/desktop-muse.md)
- [自定义连接实际实现](https://github.com/nano-muse/nanoMuse/blob/main/harness/dsh-nanomuse/src/connectors.ts)
- [Desktop Harness 模型与 MCP 架构](https://github.com/nano-muse/nanoMuse/blob/main/docs/harness.md)
