# Codex 浏览器前端优化验证 · 2026-09-16

2026-09-17 原生运行续验见 [Codex / OMP 原生会话验收](native-agent-chat-2026-09-17.md)：本机隔离执行、真实模型回复、受控工具与停止确认。

## 用户纠正：运行时目录不是 Alice 对齐完成

用户指出 All agent runtimes 与 Alice 不同。重新 Computer Use 对照确认：Alice 按 Installed / Not installed 分组，已安装项可选择；未安装项显示品牌图标、安装命令、复制和文档。此前本系统按 available 分组，将所有原生项固定为 installed=null，并展示相同英文“未接通”，不是原版的安装检测与交互。

被推翻的判断：目录有同名条目、对话框和搜索并不等于这个功能对齐。根因是把安装检测与执行桥接混在一个 available 标志里，并自行简化上游展示。后续按上游组件与真实安装检测修正；不将“已安装”伪装成“已能在 OpenCLI 执行”，也不能用占位目录验收整体 Agent 功能。左侧导航及已有权限边界保持不变。

### 本轮修正与实测

- 复用上游运行时分组、四项快捷选择、图标、安装命令/复制/文档、搜索目录；保留内置 OpenCLI，不把它冒充原生 CLI。图标来源与许可见聊天组件 README。
- 后端仅为平台管理员检查 API 主机的允许列表 CLI 安装状态；不启动 CLI、不探查账号、不输出二进制路径。普通成员保持未知状态，不把未知归为“未安装”。安装检测不授予会话执行权限。
- Computer Use 实际对照 Alice 的 Installed / Not installed 目录。本系统真实目录现在同样检测到 Claude Code、Codex、Oh My Pi、opencode；Cursor Agent、Antigravity、Grok Build、Pi 未安装，另保留内置 OpenCLI。此结果来自实际服务，不是演示数据。
- Computer Use 已操作完整目录、搜索 Cursor、复制命令并确认“已复制”反馈；选择 Codex 后按钮显示 Codex、原草稿保留、内置 AI 接入控件隐藏；未接通引擎原因可见、发送仍禁用。没有安装软件或执行原生命令。
- 前端三个专项文件共 **30 项通过**（57.7 秒）；覆盖安装分组、真实字段语义、选择不发送、复制与失败回退、已保存会话锁定，以及既有会话恢复/停止回归。模拟 API 测试不是原生执行证据。`tsc --noEmit`、专项 ESLint 通过。
- 本轮没有改动左侧导航或全局字体。已对照目录行高与字号；仍有字体等视觉差异，不宣称像素级完整 1:1。
- 窄屏仅 Playwright 390×844 回归通过；内置浏览器窄屏 override 未生效，不能宣称 Computer Use 窄屏通过。真实模型、搜索、原生执行桥接、本机账号接入及 TUI 仍未完成。

路由记录：主控承担前端集成和真实 Computer Use，Poincare 专项处理后端只读安装探测与测试，Popper 独立复核权限、安装语义及复制行为；复核发现 Windows `shutil.which` 隐式搜索当前目录，定向返修为仅探测显式绝对 PATH 项并保留 PATHEXT，不扩展为整仓改动。

最终复核：新增当前目录、空/相对 PATH 和 PATHEXT 回归；主控及独立复核分别重跑后端两个专项文件，均 **84 项通过**，既有依赖弃用告警未处理。Scoped Ruff/format 通过，独立复核无剩余发现。保留原启动命令、环境与临时数据库重启 API 后，Computer Use 整页重载并再次选择 Codex，真实四项安装结果、草稿保留及未桥接发送阻断均仍成立。未提交、回退或清理其他未提交改动。

## Agent 功能补齐：执行配置与会话控制

范围仍限于右侧聊天和既有会话 API，不改左侧导航，不新造业务对象或绕过提案确认。

- 按 Alice 的实际菜单流程接入 runtime 菜单 → Others → 可搜索的 All agent runtimes 对话框；AI access 与模型使用本系统的真实、脱敏配置目录。原生引擎没有接入前明确不可选，安装状态未知不冒充已安装。
- 新会话可保存连接/模型选择；恢复时沿用原执行配置，换连接需新建会话。服务端校验权限、启用状态及模型归属；显式选择不静默回退，不修改全局默认、不返回凭据。
- 未配置默认路由、目录请求失败或目录结构无效时阻断发送，但允许编辑草稿并重试配置。
- 增加服务端合作式停止协议及前端运行状态同步；“已接受停止请求”和“实际停止”分开，只有服务端持久化终态才解除运行锁。刷新、断线及超过 50 轮的会话读取最新一页，不把旧历史误判为当前已空闲。
- 修复旧深链异步恢复未等待、切换会话不更新 URL 的问题；取消失败仍显示错误并保持运行中，已有结果不删除。已完成的外部操作不能承诺撤销。

### 本批验证与边界

- Playwright：`agent-launch-options.spec.mjs`、`agent-dock-ux.spec.mjs`、`agent-conversation-session.spec.mjs` 共 26 项通过；覆盖配置选择与恢复、错误目录、无模型、长会话、取消确认/失败、断线恢复和 390×844 布局。这些使用模拟 API，不是实际模型成功证据。
- 独立复核暴露的空轮询错误解锁、迟到取消响应影响下一轮、终态早于 POST 响应时仍锁住输入、明确的执行前拒绝导致永久锁定，均有定向回归覆盖并通过。
- `tsc --noEmit` 和本批前端文件 ESLint 通过。
- 后端专项及相关回归 138 项通过；最终修复后主控再次执行取消测试 29 项通过，覆盖提交、提交后 refresh、model session 初始化时被取消。Scoped Ruff 与 diff 检查通过；既有依赖弃用告警仍在。
- Computer Use：实际打开 Alice 的 runtime、AI access、模型/推理入口；OpenCLI 真实 API 创建并保存测试会话，未配置模型时得到实际失败且保留草稿。重启 API 后实测会话恢复、新建、建议填入、runtime 菜单/搜索、AI access；无模型预检查明确阻断发送，草稿仍可编辑。原导航、顶部工具栏及工作标签保留。
- 再次尝试内置浏览器 390×844 override，页面实际仍为 1308×1272，已 reset；因此仅自动化窄屏通过，不能声称内置浏览器窄屏实测通过。
- **仍未完成**：Codex/Claude 等原生引擎桥接、本机账号 AI access、TUI、reasoning override、真实模型/搜索执行。运行时目录及 GUI 壳不是这些功能的完成证据。原生引擎涉及本机账号和终端/文件权限，不能默认开放给工作区成员。

本批分工：主控负责前端集成与 Computer Use；后端专项负责配置校验、持久化及取消；独立复核负责权限和竞态。根据复核证据返修，未扩展为全仓重构，未提交、回退或覆盖已有修改。

最终独立复核确认本批所列权限与竞态问题已修复，无剩余阻断项。API 保留原启动参数、环境与临时数据库重启；最终整页重载后新对话、原应用外壳及真实配置阻断提示可见。浏览器日志保留开发热更新时 effect 依赖数组变化的历史提示，不能将历史日志宣称为零错误；最终重载后未观察到新的同类错误。

## 最新范围修正：只对齐右侧聊天区

- 用户明确纠正：只需要右侧 Alice 聊天体验，不替换整套应用外壳。
- 已移除本次新增的 Alice 左侧导航，恢复既有 AppSidebar、AppHeader、工作标签与命令面板；未改写导航配置或其他页面。
- 右侧保留移植的 landing/composer 布局、建议填入、工作区选择；会话历史与新建入口收在右侧工具栏，不占用原左栏。
- Computer Use 实测 3010 `/launch`：原左栏的项目、插件、资源与管理入口可见；顶部工具栏、已打开的工作标签仍在；建议点击后仅填入草稿、聚焦输入并隐藏建议；新建对话恢复空态；底部输入框完整可见。
- 验证：`tsc --noEmit`、修改的聊天展示层/外壳/测试文件 ESLint、`agent-dock-ux.spec.mjs` 4 项均通过。自动化发送失败使用模拟 API，不是模型成功运行证据。
- 独立复核还发现共享会话控制器的旧深链恢复等待与会话 URL 同步风险；本次导航范围修正未处理，不能据此宣称会话生命周期全部通过。
- 未验证窄屏、真实模型/搜索、TUI；不宣称完整功能 1:1。源码移植及 AGPL 来源说明见 `frontend/components/experience/alice-chat/README.md`。

以下为此前各批次记录，不代表当前 `/launch` 仍使用旧研究表单。

已启动两套服务，并通过 Codex 内置浏览器实际操作 OpenAlice 和本项目。此次完成 Agent 对话与研究入口的具体优化；不代表整个项目已经与 OpenAlice 全面对齐。

## 已打开的服务

| 服务 | 浏览器入口 | 运行范围 |
| --- | --- | --- |
| OpenCLI Admin | [开始研究](http://127.0.0.1:3010/launch) | API 8031；临时 SQLite 测试数据 |
| OpenAlice | [Chat](http://127.0.0.1:5173/chat) | API 47331；独立开发数据目录、Lite 模式 |

两页均保留在 Codex 浏览器。OpenAlice 参考版本为 `c02a3c7d7d438783897e3f7610714b121c41367c`。本项目原数据库和 `.env` 未修改；测试创建的工作区、项目与对话位于临时环境。

## 实际改动

- `frontend/components/shell/global-agent-dock.tsx`：发送失败保留草稿；常用建议只填入并聚焦输入框；统一会话加载、关闭和待确认时的输入阻断提示；消息保留换行；支持展开/收起；切换 URL 指定的会话时使旧异步请求失效。
- `frontend/components/experience/launch-experience.tsx`：无项目时明确提示创建或选择；能力状态增加文字、错误重试及配置入口；接入代码改为按需展开；列表请求失败与真正的空列表分开显示；清理失效项目选择，并同步创建成功后的列表缓存与选择。
- `frontend/e2e/agent-dock-ux.spec.mjs`、`frontend/e2e/launch-experience.spec.mjs`：补充草稿、建议、展开、加载阻断、配置折叠、空态及列表错误重试的回归用例。

### 导航、Inbox 与项目续跑

- `frontend/components/experience/launch-experience.tsx`：首发入口优先恢复 URL 指定且当前可访问的 Workspace，其次恢复最近选择；把确认后的选择同步到 URL 与项目页共用存储，并让“查看所有项目”显式携带 `workspace`，避免进入项目页后丢失作用域。
- `frontend/lib/workspace-preference.ts`、`frontend/app/(app)/studio/page.tsx`：最近 Workspace 改为按登录身份隔离，并把浏览器拒绝本地存储视为可选能力，不再让隐私策略或存储异常阻断页面。
- `frontend/components/shell/work-tabs.tsx`：在 Inbox、通知等没有 `workspace` 查询参数的全局页面恢复最近确认的 Workspace 工作标签；标签与最近 Workspace 都按身份隔离，存储内容仍经既有本地路由白名单校验。
- `frontend/app/(app)/inbox/page.tsx`：Inbox 不再无条件回退到第一个 Workspace；会恢复当前用户仍可访问的最近 Workspace，把选择同步到 URL，并在切换后供首发入口和工作标签继续复用。
- `frontend/components/action-center/tasks-pane.tsx`、`frontend/components/shell/data-states.tsx`：任务空态不再只描述“采集”，明确研究、项目工作流和自动化都可产生任务，并提供携带当前 Workspace 的“开始研究”“打开项目”入口。
- `frontend/e2e/launch-experience.spec.mjs`、`frontend/e2e/openalice-work-tabs.spec.mjs`、`frontend/scripts/check-inbox-regressions.mjs`：补充 Workspace 传递、Inbox 最近 Workspace 恢复与切换、全局页面工作标签恢复及任务空态入口回归。

## Computer Use 验证

| 场景 | 实测结果 |
| --- | --- |
| OpenAlice Quick Start、Inbox、New chat | 页面可用；Inbox 分组切换有反馈；建议填入聊天框后获得焦点，没有自动发送 |
| 本项目登录与创建项目 | 正常认证；创建项目后自动选中并加载能力状态 |
| Agent 常用建议 | 填入草稿并聚焦，未自动发送 |
| Agent 发送失败 | 未配置模型时返回实际 API 错误，草稿仍保留 |
| Agent 展开 | 首次发现按钮变化但宽度被限制；修正后 DOM 实测宽度为 720px、max-width 为 none |
| 空工作区 | 显示明确的创建/选择项目提示，研究按钮禁用 |
| 空工作区创建首个项目 | 最终版本再次实测成功，新项目自动选中并展示实际能力状态 |
| Agent 接入配置 | 默认折叠；展开后出现 Codex、Claude Code 配置及复制按钮；可收起 |
| OpenAlice 导航对照 | 实际操作 Quick Start、Inbox、Issues、Tracked、Chat Harness 及 Workspace 菜单；确认其 Workspace 与新会话入口持续驻留 |
| 首发入口 → 项目 | 首发入口已选中 OpenCLI 工作区时，侧栏“项目”和“查看所有项目”均继续显示同一工作区及 2 个实际项目 |
| 项目 → 工作流 | 从“前端体验验证”项目进入“打开工作流草稿”，可见同一 Workspace / Project / Workflow 上下文、生命周期操作和画布 |
| Inbox 任务空态 | 显示研究、项目工作流和自动化三类来源，并出现“开始研究”“打开项目”；“打开项目”继续保留 OpenCLI 工作区 |
| 全局工作标签 | 从项目与编排进入 Inbox / 通知规则后，项目与编排标签仍可见；点击项目标签返回原项目成功 |
| 全局 Workspace 连续性 | 在首发入口选择“空工作区体验验证”后进入 Inbox，整页重载仍选中同一 Workspace 且 URL 自动补全；在 Inbox 切回“OpenCLI 工作区”后，工作标签立即恢复，随后进入首发入口仍保持同一 Workspace 与项目 |
| Launch 同页切换 | 在“OpenCLI 工作区”和“空工作区体验验证”之间切换时，URL、项目选择和壳层工作标签同步更新；不会出现研究表单与工作标签属于不同 Workspace 的状态 |
| 任务空态 CTA | 从 OpenCLI 工作区的 Inbox“工作项”点击“开始研究”，实际进入带同一 `workspace` 的 Launch，并继续选中“前端体验验证”项目 |

## 检查结果与范围

- 本轮相关组件、页面、脚本与 E2E 文件的 ESLint 与 `tsc --noEmit` 通过。
- `check-global-agent-dock-regressions.mjs`、`check-agent-conversation-regressions.mjs`：7 项通过、0 失败。
- `check-global-agent-dock-regressions.mjs`、`check-agent-conversation-regressions.mjs` 的既有 7 项，以及本轮 `check-work-tabs.mjs`、`check-inbox-regressions.mjs` 的 8 项均通过。
- 本轮通过已运行的 3010 开发服务执行 `launch-experience.spec.mjs`、`openalice-work-tabs.spec.mjs`：16 项通过、0 失败。独立 standalone E2E 启动器仍受 Windows `.next/standalone` 依赖路径 `EPERM` 阻塞；未删除或重建现有产物。
- 本轮相关文件 ESLint 与 `tsc --noEmit` 通过，跟踪文件的 `git diff --check` 通过。`agent-dock-ux.spec.mjs` 仍未在自动化浏览器套件中运行；URL 会话请求失效仍是代码契约检查，尚未进行真实延迟请求竞争测试。
- 列表 API 错误/重试分支完成独立代码复核并补充用例，未在真实服务中注入故障。
- 先前尝试 390×844 窄屏覆盖后浏览器仍报告 1308px；本轮内置浏览器没有提供 viewport capability，因此仍未将移动端记为通过。
- 临时环境未配置搜索服务与模型，没有验证真实模型回复、研究生成或外部连接器执行。浏览器历史日志中包含热更新时 effect 依赖数量变化提示、既有 THREE.Clock 弃用提示及 React Flow attribution 提示；最终版本已整页重新加载，本轮没有出现新的 error 级日志。

## 分工与复核

服务启动由两名 Luna High Agent 分别负责；Agent 对话改动由 Terra Medium Agent 负责；主 Agent 完成研究入口、集成和实际浏览器操作。Luna 独立复核发现展开宽度、会话请求失效、列表错误误判及失效项目选择问题，修复后复核通过。最终独立代码复核又发现 Launch 客户端 URL 分裂、最近 Workspace 跨身份共享、任务 CTA 丢失作用域与本地存储异常四项边界；主 Agent 修复后完成定向自动化与真实浏览器复测，原复核者已逐项确认通过。沿用现有组件与依赖，未提交或回退工作区已有改动。
