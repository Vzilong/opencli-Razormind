# Codex / OMP 原生 TUI 验证（2026-09-17）

## 范围

- 页面：`http://127.0.0.1:3010/launch`
- 控制面 API：`http://127.0.0.1:8031`
- 隔离节点：WSL `Ubuntu-24.04`，Fleet 出站通道 `127.0.0.1:19833`
- 运行时：Codex、Oh My Pi（OMP）
- 部署方式：本机原生开发服务；未使用 Docker 或 PowerShell 启动项目服务

## Computer Use 实测

通过 Codex 内置浏览器操作 `/launch` 右侧聊天区完成以下流程：

1. 选择 Codex → TUI，提交“只回复 `TUI-CODEX-AUTO-OK`”。真实 Codex 0.153.4 TUI 自动收到首条提示并返回 `TUI-CODEX-AUTO-OK`，不需要额外按回车。
2. 刷新页面后重新附着同一 Codex PTY，历史画面和回复由终端快照恢复，没有启动第二个 CLI。
3. 第二个浏览器窗口打开同一会话时显示“只读 · 其他窗口正在控制”；点击“接管”后第二窗口获得控制，原窗口显示“控制权已被其他窗口接管”。
4. 点击“停止终端”后显示退出码 `-9`；WSL 中未残留 Codex 进程。
5. 选择 OMP → TUI，提交“只回复 `TUI-OMP-AUTO-OK`”。真实 OMP TUI 复用已有 `openai-codex` 登录并返回 `TUI-OMP-AUTO-OK`。
6. 带工作区参数刷新页面后重新附着同一 OMP PTY并恢复快照；停止后 WSL 中未残留 OMP 进程。

实测同时确认：开发环境浏览器 WebSocket 直连 `8031`，Fleet 中间件只对精确的终端 WebSocket 路径让渡给 60 秒签名票据鉴权；错误的相似路径仍由 Fleet 鉴权拒绝。

## 审查后真实服务复测

独立审查修复完成后，使用真实 Chromium 连接 `3010` 页面和 `8031` API 再次执行完整运行链路（未 mock API、WebSocket 或 CLI）：

1. Codex TUI 会话 `7acd3e68-8bfa-4b6d-af7f-b9f5e1c1d118` 返回首条唯一标记；仅重启控制面 API、保留 WSL 边缘节点与 PTY 后，页面自动申请新票据并重新附着同一终端，第二条唯一标记也由同一 Codex PTY 返回；停止后进程组不存在。
2. OMP TUI 会话 `c0ef4b66-2d44-4a4d-a6ab-7a9a443fb4ff` 完成同样的首条响应、API 重启续接、二次响应与进程组清理流程。
3. 停止结果只有在进程组探测确认消失后才写入 `cleanup_confirmed=true`；探测超时会失败关闭，不再提前声称清理完成。

本次最终复测时 Codex 内置浏览器的 Computer Use 桥接服务返回 `nodeRepl.fetch request failed`；按恢复流程重置并重试后仍不可用。因此审查后的重启续接证据来自真实 Chromium 自动化，前一节的内置浏览器操作证据来自同一轮较早的 Computer Use 实测。

## 自动化验证

- `.venv\Scripts\python.exe -m pytest tests\unit\test_agent_server.py tests\unit\test_ws_agent_manager.py tests\unit\api\test_agent_terminal.py tests\unit\security\test_fleet_auth.py tests\unit\agent_runtimes\test_native_chat_deployment.py --no-cov -q`
  - 结果：`196 passed`
- `npm run typecheck`（工作目录 `frontend`）
  - 结果：通过
- `set PLAYWRIGHT_SMOKE_PORT=3010&& set PLAYWRIGHT_REUSE_EXISTING_SERVER=1&& npm exec -- playwright test e2e/agent-terminal.spec.mjs e2e/agent-launch-options.spec.mjs e2e/agent-conversation-session.spec.mjs --workers=1`（工作目录 `frontend`）
  - 结果：`30 passed`

## 验证边界

- 未验证窄屏 TUI 布局；不能声称窄屏已通过。
- 未验证 Codex / OMP 之外的运行时；当前 TUI 仅开放给这两个已绑定运行时。
- 未配置 OpenCLI GUI 的真实模型或搜索服务；本记录不声称这些能力可用。
- OMP 首次安装时的 provider / theme 引导曾由本轮 Computer Use 手动完成；后续自动首发是在该本机已完成初始化的账号环境中验证。
