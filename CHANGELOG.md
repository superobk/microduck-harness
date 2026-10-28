# 变更记录

## 0.3.0 — 2026-10-28

### microduck-tools 原生插件整合

- 固定原始提交 `2b93cb3afc32fa343cc612ee060e94c5bdd7b01d`，保留全部65个文件及逐文件哈希，外部桥接不改原始快照。
- 提供daemon-v0.16.3 / daemon-v0.17.8的完整软件校准与相机四向源码补丁；0.17.8相机为本工程明确移植。
- 配置Web默认localhost:8768；与Gateway、executor、vision端口分开。配置保存和daemon实际生效分别显示。
- 执行器增加认证的`/plugins`，核对保存值、当前进程身份和运行证明；插件generation绑定状态票据，动作中持续检查，停止保持独立可用。
- 保持关节/IMU转换只在原生总线层执行一次；原始相机像素、K/D与FK保持同一几何约定。

### 统一原生部署

- 增加`duck-deploy validate/render/secrets`和完整TOML模板，生成电脑与ZERO独立配置、可读启动脚本、SSH转发及可选systemd服务。
- 三个服务令牌独立生成，ZERO只拿executor令牌；密钥与模型配置不进入部署模板，重复render保留秘密和运行时模型设置。
- 显式合并脚本只更新`bus.calibration_path`和`media.camera_rotate`，保留其他原生TOML内容，不重启、不上力。
- `docs/DEPLOYMENT.md`收录端到端原生命令、官方完整构建/开发签名/更新器安装、真实校准、ArUco、模型接入、停止和版本回退。
- live数据库/模型设置使用独立的`data/live/`，避免演示配置影响真机部署。

### 验证

- 两版固定官方源码的补丁dry-run、实际apply、reverse-check和差异检查通过；原始上游35项测试通过。
- 新增配置生成、保留式合并、插件身份/生效/过期、动作期限和桥接测试；最终数量及结果见`docs/VALIDATION.md`。
- 未运行Rust编译、GPU推理或实体Microduck动作，不将源码应用或合成协议测试当作硬件验收。

## 0.2.0 — 2026-10-28

### 可变模型配置

- 网页与操作者REST接口可调整GLM兼容服务地址、准确模型ID、认证、超时、JSON mode，以及AgentJev快速判断参数。
- 每个任务固定配置revision；执行中保存的新配置供下一任务使用。凭据不回显，参数与密钥分离，环境引用和本地密钥都绑定授权服务来源。
- 参数/凭据通过原子替换持久化；无API也可启动live工作台，模型未就绪时不创建新目标。demo与live可使用独立配置路径。

### ZERO与双版本

- 审计并支持robotd0.16.3/API41、0.17.8/API44；支持明确版本或auto，实际hello和状态字段决定是否兼容。
- 每个移动连接再次握手，核对状态连接的版本/API/revision；停止保持独立可用。允许合法自定义构建并显示提示。
- 拒绝fake/sim ToF用于真实执行。保留完整15舵机、原生总线所有权、有界动作与观测时效要求。

### ArUco首轮部署

- DICT_4X4_50 ID0–3，160mm黑边、20mm白净空；配套四方位竖直板地图与±0.8m机身参考范围。
- 5页矢量A4打印包、5页中文图文放置指南、独立SVG/PNG源图。
- 20mm棋盘（8×5内角点）、只读原生PNG采集与真实OpenCV标定工具；模板不填假K/D。
- 补充同Wi-Fi下SSH转发与局域网直连步骤、成套视觉/Gateway配置与首轮可见目标确认。

### 验证

- 248项测试通过，0跳过；涵盖真实本地Unix socket/MCP协议、ASGI配置、视觉几何与最终PDF尺寸/识别。
- 两份PDF合计10页已渲染并目视检查。未连接实体Microduck、用户模型API或运行AgentJev权重；详细证据见docs/VALIDATION.md。
