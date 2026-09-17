# 内化浏览器 Agent 协作能力

日期：2026-09-14。

状态：已实施原生持久控制权阶段（Issue #147）；真实 API、Chromium 与页面接管流程已通过，见[验收记录](../testing/native-browser-control-acceptance-2026-09-14.md)。

## 决策

借鉴 ego-lite 的任务空间、操作上下文和用户接管设计，将能力实现在 OpenCLI 自己的浏览器基础设施中。核心功能不要求安装 ego-lite，也不把其单独分发的浏览器视为本项目已经拥有的源代码。

本项目已有 BrowserInstance、BrowserAccount、BrowserBinding、BrowserSpace、能力 bundle、任务租约及有序事件。沿用这些模型，避免新增第二套账号、Space 或任务对象。现有独占空间以受管理的浏览器实例为边界；它不等同于在用户日常浏览器中复制隔离标签空间，也不证明共享 Cookie 的多个 Space 具有账号隔离。

| 要内化的能力 | 现有基础 | 本阶段结果／后续边界 |
| --- | --- | --- |
| 登录态复用 | 受管理账号、Profile、BrowserBinding | 继续使用明确授权的账号和运行实例；不迁移用户全部浏览器资料 |
| 任务隔离 | BrowserSpace 实例独占、单活跃任务、数据库约束 | 保持已有隔离边界和取消确认；不同 Agent 不通过全局“当前标签”串用页面 |
| 用户接管 | 原来只有取消与关闭 | 新增持久 agent/human 控制模式、版本检查和事件，人工模式阻断平台自动任务 |
| 批量操作 | runtime bundle 暴露结构化能力，已有工作流编排 | 沿用显式能力与参数校验；跨步骤序列须逐步保留权限、超时与失败结果，不引入任意 Node/宿主机代码执行入口 |
| 页面快照 | 具体 runtime 的读取能力 | 后续统一时，引用须绑定页面/文档版本，陈旧引用失败后重新读取；本阶段不声称已实现 ego 内核快照 |
| 成果消费 | 数据、研究成果与 REST/MCP | 浏览器只是执行和取证层；结果仍进入平台授权的数据与成果契约 |

## 控制权契约

BrowserSpace 增加 `control_mode: agent | human`，旧空间默认为 agent。以同一 Space 的 `revision` 执行条件更新，避免陈旧页面覆盖新控制状态。

`POST /workspaces/{workspace_id}/browser-spaces/{space_id}/control` 接收 `{mode, expected_revision}`，使用工作区配置权限及既有 owner/manager 边界。仅无排队/运行任务、未关闭且远端清理已确认的空间允许切换。正在运行的任务不因请求“接管”就被视为停止；必须先等待结束或走已有确认取消流程。

human 模式保留实例预留，拒绝新的自动任务。普通浏览器池必须排除任何活动 Space 预留的实例，不能借账号登录预留的旁路参数重新取用。旧的直接 capability 调用也不能绕过预留；只有匹配当前 Space 的内部运行任务能调用预留实例的能力。切换成功记录 `control_changed`，仅包含模式、修订等安全元数据。

UI 将“暂停 Agent，人工接管”和“交回 Agent”明确显示为控制权操作。它不承诺自动打开远程桌面；实际人工查看/输入仍取决于实例已支持的浏览入口。

## 来源与复用方式

参考 [citrolabs/ego-lite](https://github.com/citrolabs/ego-lite/tree/d01be93325c7ea59d41c2ca9f4c59b58b4be4046) 的 TaskSpace/Page API 与 `native-gate.ts`。该 helper 包依赖浏览器注入的 `globalThis.ego`，不能直接在本项目普通 Chromium 上当作完整运行时使用。

本阶段是根据公开设计，在现有 Python/React 实现上补齐控制权；没有复制或 vendor 上游源码。未来如复制 MIT 许可的源码，随对应代码保留其版权和许可声明，并明确依赖的浏览器协议与平台兼容性。

## 验收

Issue #147 跟踪本阶段实现。必须有迁移默认值、权限、版本竞争、运行/隔离拒绝、人工期间任务与直接调用阻断、恢复调度的测试，以及真实 API 和界面证据。单纯增加菜单或文字不构成完成；控制面切换成功也不能代替真实远程浏览器动作验收。
