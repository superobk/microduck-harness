# AgentJev 与自有 GLM5.3-Flash

这份工程使用开源 **malevrigns/agent-jev** 的本地 HTTP 服务，版本固定为 `741c58f567681b78e4afa1e12d7491d00fda69e7`。它与 TypeSafe 的托管 Jev 产品不是同一个部署。适配器调用 `POST /api/evaluate`，不把 AgentJev 当成生成文字的 Chat Completions 模型。

## 两层模型怎样合作

1. GLM 把用户自然语言目标编译为 `explore`、`find` 或 `approach`，包含目标标签、步数和期限。暂不支持的抓取、搬运、楼梯任务会明确拒绝。
2. Harness 根据新鲜观测和当前技能生成少量可执行候选。候选带固定动作参数和语义说明，模型只能返回候选 ID。
3. AgentJev 在这组候选中返回完整概率分布。高于配置的概率与 margin 阈值时，可以使用它的选择；低于阈值、超时、格式错误或无进展时，回到 GLM 或暂停任务。
4. 执行前重新核验观测时效、姿态、地理范围和执行租约。模型响应期间机器人保持静止，不在等模型时持续刷新旧速度。
5. 完成判据由任务执行器与观测证据计算。模型选择了某动作、返回较高概率、说“完成”，都不算完成证据。

AgentJev 的 choice 概率是**当前候选集合内的相对偏好**。例如 `P(forward)=0.9` 不代表前进有 90% 成功率，也不是碰撞风险上限。GLM 的生成式选择没有校准概率，因此 `Decision.probability` 与 `Decision.margin` 是 `null`，不会伪造置信度。

## 在电脑 GPU 上启动 AgentJev

建议在单独的 Python 环境运行 AgentJev；板端执行器不需要安装 PyTorch。先按电脑 CUDA/驱动版本安装对应 PyTorch。

在 harness 根目录运行：

```bash
python3 -m venv .venv-agentjev
source .venv-agentjev/bin/activate
# 按 https://pytorch.org/get-started/locally/ 安装与当前驱动匹配的 PyTorch。
bash scripts/setup_agentjev.sh --install --download-weights
cd vendor/agent-jev
python -m jev_service.server \
  --checkpoint models/agentjev-v1/model.safetensors \
  --model-path models/agentjev-v1 \
  --temperatures models/agentjev-v1/temperatures.json \
  --device cuda:0 \
  --port 8149
```

只取代码、不安装包或权重：`bash scripts/setup_agentjev.sh`。可通过 `AGENTJEV_DEST` 更改代码目录，`AGENTJEV_PYTHON` 指定 Python 路径。脚本遇到脏工作区、其他来源或其他提交时退出，不重置现有分支。

上游下载脚本将 v1 权重固定到 Hugging Face revision `7d433994fbde17a3f0993c2f2b02fe8ca1370db1`，校验文件大小与 SHA-256，总量约 1.2 GB。模型默认监听 `127.0.0.1`。如 harness 在另一台主机，可使用受控 SSH 端口转发，保持模型服务监听回环地址。不要直接复制上游文档中的作者服务器地址。

```bash
curl --fail http://127.0.0.1:8149/health
curl --fail http://127.0.0.1:8149/api/info
```

本次工程交付没有下载权重、运行 GPU 推理或测量用户硬件时延；测试使用了按真实契约构造的 HTTP 响应。请记录实际模型 SHA-256、冷启动时间、P50/P95 延迟、决策覆盖率和任务结果，不把上游某块显卡的测速视为本机保证。

## 接入自有 GLM5.3-Flash

适配器接受 OpenAI-compatible **Chat Completions** 协议，使用自有服务的 `/chat/completions` 路径。`model` 是你的服务实际暴露的模型 ID；默认显示名 `GLM5.3-Flash` 不代表存在同名公有 API。

```python
from microduck_harness.decisions import GLMClient

glm = GLMClient(
    base_url="http://127.0.0.1:8000/v1",
    model="GLM5.3-Flash",
    api_key="",  # 从进程环境传入；不要写到仓库或日志。
    response_format=True,
)
```

如果私有推理网关不支持 `response_format={"type":"json_object"}`，配置中关闭 JSON mode；客户端仍会严格解析与验证返回 JSON。支持一个完整的 `json` Markdown 围栏，但不会从任意自然语言中猜测或提取动作。请求明确设为非流式，拒绝工具调用、未正常结束的生成和候选集外动作。

本适配器只发送结构化文字观测，不假设 GLM 具有视觉能力。摄像头图像由视觉服务解析，nanoMuse/工作台仍可展示原始图像。需要原生多模态时，应增加独立、经过验证的图像适配，而不是把图片占位文字当成模型看见了图片。

## 阈值怎样落地

首版阈值只是保守的工程起点，**没有声称已经在 Microduck 上校准**。建议先使用 shadow 方式：记录 AgentJev 推荐的候选、完整分布及 margin，由 GLM/现有控制器执行；通过轨迹结果选择阈值，再逐步启用自动接管。

每条记录至少包含观测 ID、目标、候选语义与参数、模型版本、分布、最终执行来源、动作前后位姿、进展、失败原因以及实际完成证据。按场景或完整任务划分训练、校准和测试集，避免把同一次任务的相邻帧分散到不同集合导致泄漏。

评价应同时报告：任务完成率、单位成功任务耗时、GLM 调用次数、AgentJev 接管覆盖率、无进展循环和错误完成声明。不能仅以少调用了几次 GLM 或单步选择准确率判断端到端效果。

AgentJev v1 的输入路径上限是 2,048 tokens。Harness 应发送紧凑的当前状态与有限近期历史，完整日志保存在任务记录中；超长时不会静默裁剪。某些训练 checkpoint 会报告 `usage.truncated_inputs`；适配器看到非零值会拒绝该结果。不要仅换 checkpoint 而忽略输入截断语义。

## 证据与固定版本

- [AgentJev 固定代码版本 README](https://github.com/malevrigns/agent-jev/blob/741c58f567681b78e4afa1e12d7491d00fda69e7/README.md)
- [实际 HTTP 输入与输出契约](https://github.com/malevrigns/agent-jev/blob/741c58f567681b78e4afa1e12d7491d00fda69e7/jev_service/contract.py)
- [上游 Python 客户端](https://github.com/malevrigns/agent-jev/blob/741c58f567681b78e4afa1e12d7491d00fda69e7/agentjev_client.py)
- [服务及不同 checkpoint 的截断行为](https://github.com/malevrigns/agent-jev/blob/741c58f567681b78e4afa1e12d7491d00fda69e7/jev_service/README.md)
- [固定 v1 权重](https://huggingface.co/aimeigaoshou/agent-jev/tree/7d433994fbde17a3f0993c2f2b02fe8ca1370db1)

上游代码为 Apache-2.0；本工程通过 HTTP 调用，不复制其模型代码或分发其权重。
