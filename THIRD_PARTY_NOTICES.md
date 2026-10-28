# 第三方来源与许可边界

根目录 `LICENSE` 的 MIT 条款适用于本项目原创 harness 代码。以下来源及派生资产保留其原始权利与许可条件，不因放入本仓库而改为 MIT。

## microduck-tools

- 来源：<https://github.com/superobk/microduck-tools>
- 固定提交：`2b93cb3afc32fa343cc612ee060e94c5bdd7b01d`
- 原始快照：`integrations/microduck_tools/upstream/`，65个文件逐字节保留。
- 来源和逐文件校验：`integrations/microduck_tools/UPSTREAM.json`。
- 该来源在此提交未提供 LICENSE。本次根据该仓库所有者明确要求将其配置功能整合至本仓库；不代其授予第三方许可。
- `integrations/microduck_tools/templates/`、`patches/`及原生适配中从该来源派生的内容保留来源说明，不宣称独立取得上游内容的重新授权。

## Microduck 原生源码

原生补丁针对 <https://github.com/pollen-robotics/microduck> 的固定 `daemon-v0.16.3`、`daemon-v0.17.8` 提交生成，包含上游上下文及适配。完整原生源码不随 harness 分发，按统一指南从官方来源取得；该来源的许可继续适用。固定提交与本次补丁检查证据见 `integrations/microduck_tools/SOURCE_VALIDATION.json`。

## 打印字体

`assets/fonts/MicroduckGuideSans.ttf` 是 Noto Sans SC 的改名静态文字子集，使用 SIL Open Font License 1.1。原始许可在 `assets/fonts/OFL-Noto.txt`，来源、修改方式和固定源文件标识在 `assets/fonts/README.txt`。字体许可与 harness 代码许可独立。

## 外部运行组件

AgentJev、nanoMuse、MCP SDK、OpenCV、可选 YOLO 及其他安装依赖继续适用各自项目的许可。本仓库不分发 AgentJev 或 YOLO 模型权重；下载动作由使用者按文档明确执行。AgentJev 固定代码与权重来源见 `docs/agentjev.md`。
