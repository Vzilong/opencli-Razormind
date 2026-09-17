# OpenAlice 对齐核对：2026-09-15

## 结论

当前已经接上项目创建、持久会话、Inbox 追问、工作标签四条基础路径；文件、报告、精确结果归属、原生 Agent 恢复仍未形成与 OpenAlice 相当的完整工作体验。

本次选取 12 个通用工作平台维度：**4 项基础路径已接线、6 项部分对齐、2 项未核实到等价闭环**。4/12（33%）只是本次清单中“基础路径已接线”的比例，不能当作整个产品完成率；没有依据宣称已经对齐 80% 或全部完成。部分对齐的工作量差异很大，不按半分加权制造总百分比。

## 范围与版本

- 本地项目：`D:/projects/opencli-Razormind`，HEAD `ae02a27d`，同时检查当前大量未提交及新增文件。结论针对工作目录，不等于已提交、发布或线上版本。
- 上游：[TraderAlice/OpenAlice](https://github.com/TraderAlice/OpenAlice)，本次新拉取到 `D:/Temp/openalice-comparison-20260915`，HEAD `c02a3c7d7d438783897e3f7610714b121c41367c`。
- 既有研究使用 `52b51f29809178594b7b57bf666133829368b7b4`，不能直接当成本次版本的验收。例如旧文档引用的 `ResumeCta.tsx` 在新快照中已不存在，应沿当前 session/runtime 模块继续追踪。
- 本次是源码横向核对与定向回归；未启动上游应用，未完成双项目浏览器逐页验收、真实模型运行、真实外部渠道验收。
- 验收口径：找到两边实际实现，区分基础接线与完整用户闭环，用最小针对性检查核实已有基础路径。

## 12 项能力矩阵

“基础已接线”表示有对应实现与局部验证，不表示与上游的全部边界行为一致。“未核实”不等于项目绝无相关底层能力。

| 维度 | 当前判断 | 本地证据与剩余差距 | 上游参考 |
|---|---|---|---|
| 项目创建与 Agent 操作草稿 | 基础已接线 | `frontend/components/studio/project-create-form.tsx`；`backend/api/v1/chat.py` 已有 create_project、get_workflow_draft、update_workflow_draft。现有提案治理接入；本次未重新验证真实模型生成质量。 | `src/workspaces/workspace-creator.ts`、`workspace-creator.spec.ts` |
| 项目级持久会话与续聊 | 基础已接线 | `frontend/components/studio/project-agent-workspace.tsx` 提供项目会话列表、筛选与打开 Dock；服务端会话绑定已存在。继续对话不能等同恢复原生进程。 | `src/workspaces/persistent-session.ts`、`product-session-coordinator.ts` |
| Inbox 原会话追问及来源跳转 | 基础已接线 | `frontend/components/inbox/inbox-conversation-thread.tsx`、`frontend/lib/inbox/origin-navigation.ts`。已有原会话发送入口；产物阅读与精确数据查询另计。 | `ui/src/pages/InboxPage.tsx`、`src/core/inbox-store.ts` |
| 工作标签与上下文导航 | 基础已接线 | `frontend/lib/work-tabs.ts`、`frontend/components/shell/work-tabs.tsx`、`frontend/lib/studio/run-navigation.ts`；本轮定向检查通过。 | `ui/src/pages/WorkspacePage.tsx`、workspace 组件目录 |
| 项目文件与产物浏览 | 部分对齐 | 数据页有来源分组和记录详情，但 `frontend/app/(app)/studio/projects/[projectId]/data/page.tsx:710` 上传按钮明确 disabled，并提示上传适配器未接入。不能视为通用文件工作台完成。 | `ui/src/components/workspace/FilesPanel.tsx`、`src/workspaces/file-service.ts`、`ui/src/pages/FileViewerPage.tsx` |
| Markdown、HTML 报告与会话并览 | 部分对齐 | Inbox 回复仍在 `inbox-conversation-thread.tsx:81` 用普通段落显示；有消息阅读和追问基础，本轮未发现接入相当的统一报告阅读器。 | `ui/src/components/FileContentView.tsx:35` 按扩展名分派 Markdown 与隔离 HTML 阅读器 |
| 精确结果归属 | 部分对齐 | 有 Run/Trace 上下文和记录的 workflow_run_id 元数据，但 `backend/api/v1/records.py:33` 列表没有 run 筛选参数；`run-context-banner.tsx` 明示按项目展示。URL 上的 run 不等于查询按 run 隔离。 | `src/core/inbox-store.ts`、`src/tool/inbox-push.ts` 的来源关联 |
| 原生 Agent 工作状态与恢复 | 部分对齐 | `backend/agent_runtimes/` 有 Codex、Claude Code、Pi 等适配；项目会话 UI 只按 active/closed 分类。本轮未证明暂停、受阻、退出及原生会话恢复已贯通此工作面。 | `src/workspaces/session-runtime-store.ts`、`session-presentation.ts`、`resume-registry.ts` |
| 配置就绪与就地修复 | 部分对齐 | `backend/agent_runtimes/base.py:30` 已有 RuntimeReadiness，不能算完全没有。尚未核实安装、凭据、模型与修复入口构成统一产品闭环。 | `src/workspaces/agent-runtime-readiness.ts`、`agent-credential-readiness.ts`、`ui/src/pages/HarnessSettingsPage.tsx` |
| 自动化与阻塞解释 | 部分对齐 | `backend/api/v1/automations.py` 有安装、创建、更新、手动触发，既有持久调度；未核实与负责人原生 Session 状态相结合的统一健康投影。 | `src/workspaces/issues/automation-health.ts` 显式组合 owner、blocker 和 scheduled execution |
| 外部渠道回复原会话、按需领取产物 | 未核实等价闭环 | 有 notifications、webhooks、delivery 路由，不能说没有渠道能力。尚无本轮证据证明“接收结果→渠道回复→原会话续办→产物领取”全链路成立。 | `services/connector/src/core/adapter.ts:40` 的产物请求队列、`:63` 的产物交付及 health 接口 |
| 项目文件 Git 历史与工作区恢复 | 未核实等价闭环 | 当前项目/工作流修订与会话持久化不等于用户工作区文件的 Git 浏览、历史与恢复；本轮未验证等价用户路径。 | `src/workspaces/git-service.ts`、`git-execution.ts`、`workspace-lifecycle.ts` |

## 整个产品层面的区别

架构并非同一套：本地是 Next 前端、Python 后端、数据库中的 Project/Workflow/Run 与授权对象；OpenAlice 有 TypeScript 工作区服务、原生 Session、文件及 Git 工作区、独立 connector/UTA 服务。应对齐用户能力与状态语义，不以目录相似或复制文件数计算完成度。

OpenAlice 当前源码还包含行情、投资组合、交易审批、模拟器、AutoQuant/Auto Prediction 等金融产品模块：`ui/src/pages/MarketPage.tsx`、`PortfolioPage.tsx`、`TradingAsGitPage.tsx`、`SimulatorPage.tsx`、`AutoQuantSetupPage.tsx`。这些未计入上述通用能力分母，也没有认定本项目需要照搬。

本项目同时有数据源采集、DAG 工作流、浏览器账号与空间、品牌和通用知识库等独立模块（`backend/api/v1/sources.py`、`studio_workflows.py`、`browser_accounts.py`、`browser_spaces.py`、`brand_knowledge.py`、`knowledge_libraries.py`）。这说明整个项目不适合用一个未定义口径的 OpenAlice 百分比概括。本次没有逐一验证这些本地独立模块的运行成熟度。

## 建议对齐顺序

1. 先修结果归属：从运行进入数据页，查询必须真正按该运行筛选，并验证权限与跨运行数据隔离。
2. 接通通用文件/产物列表、读取和统一 Markdown/HTML 阅读；把文件、报告和原会话并排呈现，保留来源及返回路径。
3. 把原生运行状态、可恢复性、readiness 与自动化阻塞原因统一投影到项目工作面；按真实运行时能力提供操作。
4. 接通渠道回复原会话和产物领取，补实际外部渠道验收。
5. 按产品需要决定工作区 Git 历史的范围。金融专用模块另行定义需求，不能混进本轮通用体验对齐。

以上为差距排序，不是已实施承诺；本轮没有修改应用代码或创建新的任务计划文件。

## 本轮验证与局限

从 `frontend/` 执行：

```text
node --test scripts/check-work-tabs.mjs scripts/check-run-navigation-behavior.mjs scripts/check-inbox-origin-navigation.mjs scripts/check-agent-conversation-regressions.mjs scripts/check-project-agent-workspace-regressions.mjs
```

实际结果：exit 0，19 tests，19 pass，0 fail。包含源码契约及局部行为检查，不是全栈 E2E；不能证明上表所有能力可用。本轮未运行完整后端测试、全量构建或双项目浏览器体验验收。历史文档中的 9/9、81/81 等数字未算作本轮测试结果。

路由说明：本轮为只读跨仓库核对，由主代理完成；没有并行 Agent、没有应用代码变更，也没有高风险实施或模型升级。新增本核对记录，保留所有既有工作目录修改。
