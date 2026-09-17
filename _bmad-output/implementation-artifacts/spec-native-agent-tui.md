---
title: 'Codex / OMP 原生 TUI 会话'
type: 'feature'
created: '2026-09-17'
status: 'done'
baseline_commit: '23997ee8215357af3f3b755fa4bd6404fe97c365'
review_loop_iteration: 0
context:
  - 'frontend/components/experience/alice-chat/README.md'
  - 'agent/native-chat/README.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** `/launch` 目前只把 Codex / OMP CLI 当作 GUI 对话的推理后端；模式菜单仍禁用 TUI，用户无法获得 Alice 已有的原生终端交互、持续会话和重连体验。

**Approach:** 在右侧聊天区接入真实 PTY 终端。前端复用 Alice 的 xterm 交互；控制面通过已认证 WebSocket 与 Fleet 出站连接复用现有 WSL 隔离节点，节点持有 PTY、回放缓冲和进程清理证据。首版支持已经授权的 Codex 与 OMP，不改变左侧导航或 GUI 对话路径。

## Boundaries & Constraints

**Always:** 工作区身份、平台管理员与执行权限必须在创建、附着、输入、接管、停止时逐次校验；只允许匹配当前原生绑定的保留节点。PTY 在既有 bubblewrap 边界内运行，仅挂载对应运行时凭据与只读空工作目录，不获得控制面环境、主机仓库或另一运行时凭据。刷新/短暂断线重附着同一 PTY并回放权威终端快照；单会话仅一个写入控制者。停止必须等待进程组清理确认，Ctrl+C 只作为终端输入。

**Ask First:** 开放任意项目目录写入、CLI 原生工具/插件/MCP、普通成员或其他工作区共享本机账号、接入 Codex/OMP 之外运行时。

**Never:** 伪造终端输出；在控制面直接启动 CLI；用 Docker 或 PowerShell 作为 TUI 前提；复制 Alice 的 localhost 免认证；把浏览器断开当作进程停止；绕过既有提案、权限或保留节点门禁；改写左侧导航。

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| 首次启动 | 有效工作区、Codex/OMP 在线绑定、选择 TUI | 创建持久技术会话，终端显示真实 CLI，键盘输入与 ANSI 输出双向流动 | 启动失败不生成可附着会话，显示可重试原因 |
| 刷新重连 | PTY仍存活，浏览器重载或网络短断 | 重新附着同一会话，先回放快照，再解锁输入 | 有界退避；未知/已结束会话明确关闭，不自动新建 |
| 多窗口 | 第二控制者附着同一会话 | 只读拒绝并显示接管操作；显式接管后旧连接被踢出 | 不自动抢占，不缓存断线期间输入 |
| Resize / 中文输入 | 容器尺寸变化、IME或Kitty键盘模式 | PTY尺寸同步，组合输入与特殊键不丢失 | cols/rows限制在1–1000，畸形控制帧关闭连接 |
| 停止与退出 | 点击停止、CLI自然退出、API重启 | 停止等待清理证明；自然退出显示退出码；API恢复后可查询同一节点状态 | 未确认清理保持锁定，不显示假成功 |
| 越权 | 错误工作区、非管理员、绑定变更或通用调度调用 | 拒绝创建、附着、输入和派发 | 返回通用错误，不泄露节点、路径、命令或凭据 |

</frozen-after-approval>

## Code Map

- `frontend/components/experience/alice-chat/chat-surface.tsx:113` -- 当前 GUI/TUI 模式菜单；TUI被硬禁用。
- `frontend/components/shell/global-agent-dock.tsx:741` -- `/launch` 右侧会话编排、运行时选择与停止入口；在此切换 GUI/终端内容。
- `frontend/lib/api/agent-conversations.ts:151` -- 执行配置与会话 API 类型；增加终端会话/状态客户端。
- `frontend/components/experience/alice-chat/terminal/` -- 新终端叶组件；从 Alice `Terminal.tsx`、协议、输入/IME、renderer 与 appearance 依赖闭包移植，保留 AGPL 来源说明。
- `backend/schemas/agent_conversation.py:21` -- 当前执行模式仅 `gui`；增加受约束的 `terminal`。
- `backend/api/v1/agent_conversations.py:179` -- 既有会话取消入口；同一认证域新增终端创建、状态、停止和 WebSocket。
- `backend/services/agent_native_chat.py` -- 复用精确工作区/运行时/节点绑定与权限校验，不另建宽松授权路径。
- `backend/ws_agent_manager.py:335` -- Fleet 唯一派发门；增加保留节点终端帧多路复用、附着、输入、resize、状态和清理确认。
- `backend/agent_server.py:480` -- 节点任务生命周期与终态证明；新增节点侧 PTY 管理、单写入、回放缓冲和重附着。
- `agent/native-chat/runner.py` -- 复用 bubblewrap 隔离清单，增加交互 TUI 入口但不扩大挂载或环境。
- `D:/Temp/openalice-comparison-20260915/ui/src/components/workspace/Terminal.tsx:294` -- 只读参考：冷附着回放、二进制输入、resize、重连、IME/Kitty与单控制者体验。
- `D:/Temp/openalice-comparison-20260915/src/webui/workspaces-ws.ts:43` -- 只读参考：PTY WS 协议与状态码；其 localhost 信任规则不得照搬。

## Tasks & Acceptance

**Execution:**
- [x] `backend/models/`, `backend/schemas/agent_conversation.py`, `backend/api/v1/agent_conversations.py` -- 持久化终端会话映射与授权 API/WS，保持其为会话实现而非新业务对象。
- [x] `backend/services/agent_native_chat.py`, `backend/ws_agent_manager.py` -- 将终端协议接入精确原生绑定和 Fleet 出站通道，拒绝通用派发绕过。
- [x] `backend/agent_server.py`, `agent/native-chat/` -- 在隔离节点实现 PTY、快照/环形回放、单写入、resize、重附着与清理证明；Codex/OMP 使用各自真实 TUI。
- [x] `frontend/components/experience/alice-chat/terminal/`, `frontend/components/experience/alice-chat/chat-surface.tsx`, `frontend/components/shell/global-agent-dock.tsx` -- 移植 Alice 终端交互并启用真实 TUI 模式，GUI行为保持不变。
- [x] `tests/`, `frontend/e2e/`, `docs/verification/` -- 覆盖矩阵中的协议、权限、竞态和实际浏览器流程，记录未验证边界。

**Acceptance Criteria:**
- Given 已授权工作区且 Codex/OMP 绑定在线，when 选择 TUI并提交首条指令，then 右侧显示真实 CLI TUI并可连续交互，不创建 GUI 消息流的替代输出。
- Given TUI运行中，when 刷新页面或短暂断网后恢复，then 同一 PTY恢复终端快照和后续输出，不重复启动 CLI。
- Given 未授权身份、错误工作区或通用任务路径，when 尝试创建或附着终端，then 服务端拒绝且无节点/路径/凭据泄露。
- Given 用户点击停止，when 节点尚未确认进程组退出，then 页面保持“正在停止/未确认”；确认后才解锁并显示终态。
- Given GUI模式的现有会话，when 本功能上线，then 既有创建、恢复、受控工具提案和取消测试继续通过。

## Spec Change Log

## Design Notes

浏览器只连接控制面同源 WebSocket；控制面不持有本机账号，也不运行 PTY。终端二进制帧通过 Fleet 连接按 `terminal_session_id` 多路复用，控制消息使用有界 JSON；节点维护权威回放与控制权。API重启后由持久映射向同一节点查询/重附着，节点丢失时保持不可恢复状态，不用超时伪造清理。

## Verification

**Commands:**
- `.venv\Scripts\python.exe -m pytest tests/unit/test_agent_server.py tests/unit/api/test_agent_terminal.py tests/unit/test_ws_agent_manager.py tests/unit/security/test_fleet_auth.py --no-cov -q` -- expected: PTY协议、权限、重连、鉴权例外和清理竞态通过。
- `npm --prefix frontend run typecheck` -- expected: xterm与终端 API 类型通过。
- `set PLAYWRIGHT_SMOKE_PORT=3010&& set PLAYWRIGHT_REUSE_EXISTING_SERVER=1&& npm exec -- playwright test e2e/agent-terminal.spec.mjs --workers=1`（工作目录 `frontend`）-- expected: GUI/TUI切换、回放、接管和停止通过。

**Manual checks (if no CLI):**
- 使用 Codex 内置浏览器分别操作 Alice 与 OpenCLI：Codex、OMP真实TUI输入/输出、刷新重附着、双标签接管、停止后进程归零；不宣称未实测的窄屏或其他运行时。

## Suggested Review Order

**右侧聊天入口**

- 从会话编排入口理解 GUI/TUI 切换与终端生命周期。
  [`global-agent-dock.tsx:829`](../../frontend/components/shell/global-agent-dock.tsx#L829)

- Alice 风格终端负责重连、回放、控制权与输入分片。
  [`native-terminal.tsx:67`](../../frontend/components/experience/alice-chat/terminal/native-terminal.tsx#L67)

**认证与控制面**

- 单次票据 WebSocket 在每个输入动作前重新授权。
  [`agent_conversations.py:113`](../../backend/api/v1/agent_conversations.py#L113)

- 会话服务绑定工作区、运行时、节点和单调状态。
  [`agent_native_chat.py:214`](../../backend/services/agent_native_chat.py#L214)

- 数据库原子消费终端票据，阻止有效期内重放。
  [`agent_native_chat.py:421`](../../backend/services/agent_native_chat.py#L421)

- Fleet 管理器复用唯一出站连接并限制附件数量。
  [`ws_agent_manager.py:397`](../../backend/ws_agent_manager.py#L397)

**隔离 PTY 生命周期**

- 节点原子启动 PTY，避免并发和取消泄漏。
  [`agent_server.py:1029`](../../backend/agent_server.py#L1029)

- 停止后轮询进程组消失，未确认时失败关闭。
  [`agent_server.py:956`](../../backend/agent_server.py#L956)

- 隔离运行器选择真实 Codex/OMP TUI 命令。
  [`runner.py:12`](../../agent/native-chat/runner.py#L12)

**持久化契约**

- 技术会话状态独立持久化，不新增用户业务对象。
  [`agent_conversation.py:101`](../../backend/models/agent_conversation.py#L101)

- 迁移约束运行时、状态、版本及会话唯一性。
  [`nat20260917a_add_agent_terminal_sessions.py:19`](../../backend/migrations/versions/nat20260917a_add_agent_terminal_sessions.py#L19)

- 前端客户端统一创建、查询、停止与票据请求。
  [`agent-conversations.ts:229`](../../frontend/lib/api/agent-conversations.ts#L229)

**验证证据**

- 浏览器回归覆盖切换、回放、重连、接管与停止。
  [`agent-terminal.spec.mjs:25`](../../frontend/e2e/agent-terminal.spec.mjs#L25)

- 节点测试证明进程组清理只在确认消失后成功。
  [`test_agent_server.py:1213`](../../tests/unit/test_agent_server.py#L1213)

- 真实 Codex/OMP、API 重启续接和边界集中记录。
  [`native-agent-tui-2026-09-17.md:24`](../../docs/verification/native-agent-tui-2026-09-17.md#L24)
