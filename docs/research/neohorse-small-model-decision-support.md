# 本项目的小模型扩展能力研究：以 NeoHorse 为候选

研究日期：2026-09-17。范围：本项目现有模型运行时与扩展执行链，以及 NeoHorse 官方仓库、模型发布说明和技术报告；本次未部署模型或复现评测。

## 结论

目标是让本项目支持 NeoHorse 或其他小模型承担可验证的扩展功能，而不是把 NeoHorse 写死为一个业务供应商。当前项目已经具备本地 OpenAI-compatible 模型接入、模型目录、角色候选和故障切换；需要补齐的是能力声明、结构化调用契约、按任务能力选择模型、超时与升级策略，以及真实本地推理服务的契约测试。

NeoHorse 值得作为首个候选模型。它发布了面向 Agent、工具调用、代码和指令遵循的 4B/9B 模型，并描述了异构模型池上的分级路由机制。但需要区分三个命题：

| 命题 | 当前证据与判断 |
| --- | --- |
| 小模型能参与任务理解、工具选择和执行步骤决策 | 官方模型定位与 Agent/工具使用评测提供支持；具体业务效果仍需实测。 |
| 系统会根据任务所需能力选择不同模型 | 技术报告明确描述了 C0–C3 分级路由和策略调整。 |
| 发布的 4B/9B 模型本身是专门训练的模型调度器 | 尚无充分证据，不能从“routing-guided post-training”推导这一结论。 |

这里的“路由引导训练”指利用路由信号组织训练样本和课程，而不是直接证明模型已经学会可靠地替我们分配所有任务。[官方 README](https://github.com/TokenRhythm/NeoHorse#readme)、[论文 §3.4、§4](https://arxiv.org/html/2609.08183v1#S3.SS4)。

## 已公开的核心机制

NeoHorse-1 的 4B 和 9B 检查点基于 Qwen3.5 后训练，采用文本输入、文本输出接口，发布权重供自托管推理使用。官方提供量化版本和推理服务说明，因此可以作为独立模型服务接入现有 Agent 系统。[模型说明与部署入口](https://github.com/TokenRhythm/NeoHorse#readme)。

论文中的路由器按每个用户回合工作，输入包括当前请求、近期对话、之前的路由决策和可用执行状态：

| 档位 | 论文定义 |
| --- | --- |
| C0 | 边界明确、低风险的请求 |
| C1 | 通用默认档位 |
| C2 | 多步骤推理和执行 |
| C3 | 最高能力或可靠性需求；某些部署可使用多个提案模型加聚合器 |

策略层可以依据风险、上下文压力、先前失败和服务约束调整路由。档位是相对能力需求，不是固定的模型名或参数量；不能直接把 C0 等同于 4B、C1 等同于 9B。[论文 §3.4](https://arxiv.org/html/2609.08183v1#S3.SS4)。

每回合分别记录：路由器原始预测、策略调整后的决策、实际服务档位，并关联执行轨迹。任务完成情况、验证反馈和恢复成本用于评价路由行为。这种分离允许区分“判断错了”“被策略覆盖了”“实际服务发生了变化”。[论文 §3.4](https://arxiv.org/html/2609.08183v1#S3.SS4)。

执行轨迹经质量筛选后用于路由引导的课程监督微调和在策略蒸馏，再根据能力缺口调整下一轮训练数据。论文将多代持续迭代及进一步训练路由器列为后续方向，目前验证只覆盖一轮更新循环。[论文 §6](https://arxiv.org/html/2609.08183v1#S6)。

## 公开实现的边界

研究代理核对了当前 main 的完整递归文件树：公开内容为 README、技术报告、LICENSE、三张图片以及 `examples/chat.py` 和 `examples/tool_call.py`。未发现 router/harness 实现、训练流水线、多供应商路由配置、失败回退实现或测试套件。这仅描述公开仓库范围，不推断作者内部实现情况。[官方文件树 API](https://api.github.com/repos/TokenRhythm/NeoHorse/git/trees/main?recursive=1)。

两个示例向用户指定的服务发送请求，展示聊天和工具调用；README 提供 SGLang/vLLM 的 OpenAI-compatible 接口部署方式。它们可以作为模型接入参考，但不能当作现成的调度引擎。[聊天示例](https://github.com/TokenRhythm/NeoHorse/blob/main/examples/chat.py)、[工具示例](https://github.com/TokenRhythm/NeoHorse/blob/main/examples/tool_call.py)。

## 本项目已经具备的基础

项目的 `openai` 和 `local` provider 共用 OpenAI-compatible adapter；`local` 允许显式配置私网 `base_url` 和无密钥服务，并保留 URL 校验与 DNS pinning。因此，只要 NeoHorse 通过 vLLM、SGLang、Ollama、LM Studio 或 LocalAI 提供 `/v1/chat/completions`，即可作为现有 `local` provider 注册，无需新增 `neohorse` provider 类型。[adapter 工厂](../../backend/llm/factory.py)、[本地 OpenAI-compatible 实现](../../backend/llm/openai_compat.py)、[本地服务预设](../../frontend/lib/provider-presets.ts)。

项目已经持久化 provider、模型目录及模型的 `capabilities` JSON，并支持 `chat`、`executor`、`enrichment` 三类模型默认候选和连接级故障切换。[模型目录](../../backend/models/provider_model.py)、[角色候选](../../backend/models/model_default.py)、[故障切换](../../backend/llm/resolver.py)。

浏览器 Skill 执行链已经是小模型扩展的参考实现：当前默认模型包含 `qwen3:4b`，模型每轮只提出一个工具动作；动作需满足固定 schema，并在执行前经过风险门和红线检查。[Skill channel](../../backend/channels/skill_channel.py)、[执行循环](../../backend/skills/loop.py)、[动作约束](../../backend/skills/actions.py)、[风险策略](../../backend/skills/risk.py)。

## NeoHorse 的特殊适配

NeoHorse 与通用聊天模型的差异主要在 Agent 训练方式、thinking 内容和工具调用模板，不在 HTTP 传输层。官方部署仍提供 OpenAI-compatible `/v1/chat/completions`；SGLang/vLLM 服务必须启用 `qwen3` reasoning parser 和 `qwen3_coder` tool-call parser，vLLM 还需启用自动工具选择。[官方部署说明](https://github.com/TokenRhythm/NeoHorse#deployment)。

官方聊天与工具示例通过 `chat_template_kwargs.enable_thinking=true` 开启 thinking，工具调用返回标准 `message.tool_calls`。[聊天示例](https://github.com/TokenRhythm/NeoHorse/blob/main/examples/chat.py)、[工具调用示例](https://github.com/TokenRhythm/NeoHorse/blob/main/examples/tool_call.py)。服务端 parser 配置正确时，项目现有主聊天与 Skill loop 已能处理标准工具调用和空 `content`；不应为 NeoHorse 新建 provider 类型。

若扩展统一通过当前 `ProviderAdapter.chat()` 调用，则必须把返回值从纯字符串升级为结构化 completion，至少保留 `content`、`reasoning_content`、`tool_calls`、`finish_reason` 和 `usage`。还应增加模型级、受控的请求默认值，以透传 thinking 配置，并按实际部署值记录上下文窗口。NeoHorse 是文本模型，不能把模板中的图像 token 当成视觉能力。[4B 模型卡](https://huggingface.co/TokenRhythm/NeoHorse-1-4B)。

NeoHorse 公开仓库没有发布论文所述 routing harness 的实现。因此，本项目可以复用模型权重和 Agent/工具能力，但能力路由、策略覆盖、结果验证和反馈记录需要由本项目实现。

## 当前缺口

`ProviderAdapter.chat()` 只返回文本，无法统一表达工具调用、结构化 JSON、流式输出、视觉输入等能力。模型目录中的 `capabilities` 还是自由 JSON，resolver 只按固定角色和候选顺序选择模型，不会根据工具调用、结构化输出、成本、时延、上下文窗口或任务难度过滤候选。

部分调用仍绕过统一 adapter/resolver：主聊天、Skill channel、Crawl4AI 和若干 processor 各自维护模型调用或默认值。当前单元与集成测试覆盖本地私网接入、密钥保护、模型目录、角色候选和失败切换，但没有针对真实 Ollama/vLLM/NeoHorse 服务的协议测试。provider 也缺少显式超时配置，Linux 容器访问宿主机推理服务还需要 `host-gateway` 或独立推理 sidecar。

## 建议的能力层

扩展功能应声明“需要什么能力”，不声明“必须使用哪个模型”。首批能力字段建议至少包含：

- `tool_calling`：是否支持项目需要的工具协议；
- `structured_output`：是否能按 JSON Schema 返回结果；
- `vision`、`streaming`、`context_window`：输入与运行能力；
- `decision_support`：是否通过对应任务集的验收；
- 可观测属性：超时、预计成本、部署位置和版本。

调用链保持为：扩展功能提交能力需求 → resolver 过滤候选 → 小模型生成结构化建议 → schema 与 allowed-choice 校验 → 确定性业务门执行或拒绝 → 失败、超时或越界时升级强模型 → 记录建议、实际动作和结果。模型的自报置信度只能作为观测字段，不能替代验证。

## 优先扩展功能

| 顺序 | 功能 | 自动化边界 |
| --- | --- | --- |
| P0 | Records 标签、实体、摘要、质量建议 | 只写 enrichment/candidate；schema、去重、来源证据和质量门仍由确定性代码负责。 |
| P0 | 研究候选、反方论点和情景草案 | 只生成候选；无证据或未验证内容不能发布为可信结论。 |
| P1 | 浏览器 Skill 的页面理解、元素选择与只读提取 | 只允许已注册动作；写入、付款、发布、删除等继续经过确认和红线门。 |
| P1 | Workflow 分支建议与失败归因 | 只能从已编译的分支 ID 中选择；不得生成或注入执行器。 |
| P2 | Agent/runtime/model 推荐 | 先以 shadow mode 记录建议；最终选择仍由能力声明和确定性选择器裁决。 |

## 最小落地顺序

1. 先用现有 `local` provider 接入一个 NeoHorse 或其他 4B/9B OpenAI-compatible 服务，并把它限定为 `executor` 或 `enrichment` 的首选候选，强模型作为后备。
2. 为模型能力建立受校验的 schema，并让 resolver 接受 `required_capabilities`；不满足能力的模型在调用前被排除。
3. 统一结构化/工具调用结果契约，先把 Skill channel 接入 `executor` resolver，移除其内嵌模型默认值。
4. 选择 Records 富化和研究候选两个低破坏场景做首批扩展，并建立离线基准与 shadow mode。
5. 补充 provider 超时、真实推理服务契约测试和 Linux Docker 网络配置，再扩展到浏览器动作和语义路由。

这套设计同样适用于 Qwen、Llama、Gemma、Phi 等模型；NeoHorse 是需要通过同一验收矩阵的一个候选，而不是架构特例。

## 证据能说明什么

官方报告十项基准的宏平均：4B 从 58.94 提升到 64.87，9B 从 65.60 提升到 69.04；4B 的 BFCL v4 从 61.02 到 61.79。数据支持后训练改善部分 Agent 能力，但并不证明它具备高准确率的业务决策或路由分类能力。宏平均分数也不能当作真实任务成功率。[官方评测表](https://github.com/TokenRhythm/NeoHorse#evaluation)。

论文指出较大模型在迭代调试、执行失败恢复和长操作链状态维护方面仍有明显优势；未证明递归改进能跨多代持续积累。[论文 §6](https://arxiv.org/html/2609.08183v1#S6)。

## 最小验证方案

要判断是否适合本项目，先用有明确答案或验收条件的历史任务做离线对比，不直接让候选决策控制生产动作：

1. 建立 Records 富化、研究候选、工具选择、参数生成、失败后升级五类样本，并保留一组未参与调参的样本。
2. 比较规则基线、NeoHorse 小模型、当前主模型；如研究训练增益，再加入同规模基础模型。
3. 分别统计任务成功率、工具与参数有效率、该升级却未升级的比例、无必要升级比例、端到端延迟和每个成功任务的总成本。
4. 将重试、验证和升级开销计入总成本。只有在满足业务成功率和错误边界的前提下确有收益，才考虑接入。

不预设小模型一定更快或更便宜，也不把其自报置信度直接当作可靠性保证。上线阈值需要依据具体业务验收标准确定。

## 研究与验证记录

采用三名只读探索代理分别核对项目模型运行时、扩展用例和配置测试面；另有一名研究代理核对 NeoHorse 公开实现范围。主代理核对论文、项目源码和测试，区分现有能力、缺口与建议。未进行本地推理、性能测试或应用集成验证。
