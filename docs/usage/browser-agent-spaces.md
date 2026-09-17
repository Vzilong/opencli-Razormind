# 浏览器任务空间与人工接管

OpenCLI 的 Browser Space 为任务保留一个受管理的浏览器实例。它使用平台现有运行时、账号和能力配置，不要求安装 ego-lite。

## 使用方式

1. 在“执行资源”的浏览器页面选择已就绪的运行实例，创建 Space 并选择它可以调用的能力。创建实例预留需要平台管理员权限。
2. 在 Space 中提交已授权任务。普通采集任务不会再从浏览器池取用这个被预留的实例。
3. 需要人工操作时，等待当前任务结束，点击“暂停 Agent，人工接管”并确认。人工模式保留实例和登录状态，拒绝平台自动任务。
4. 通过该实例已有的浏览入口进行人工操作；这个控制权按钮本身不会建立远程桌面连接。
5. 操作结束后点击“交回 Agent”并确认，随后可继续在同一个 Space 提交任务。

正在排队、运行或尚未确认远端清理的任务会阻止切换。请求取消不等于远端已停止；界面会保留状态，直到能够安全切换。关闭 Space 才会释放实例预留。

此处的人工模式是平台调度控制，不是操作系统级访问控制。持有节点管理权限的工具仍必须遵守该实例的运维边界。共享同一 Profile 的账号状态也不能被视为不同账号之间的隔离。

## API

使用现有用户身份与工作区权限访问 Space API。控制权切换需要工作区配置权限，并满足 owner/manager 检查。

读取 Space 的最新 `revision` 后，调用：

```http
POST /api/v1/workspaces/{workspace_id}/browser-spaces/{space_id}/control
Content-Type: application/json

{"mode":"human","expected_revision":4}
```

恢复时传入 `mode: "agent"` 和最新修订。成功响应包含新的 `control_mode` 与 `revision`；事件列表新增 `control_changed`。版本过期或任务仍活跃时返回 409，应读取最新状态后由调用方重新决定，不能盲目重试切换。

人工模式下 `/tasks` 拒绝新任务。直接调用旧的 `/browser-sessions/{instance_id}/capabilities/{capability}/invoke` 也不能绕过 Space 预留；预留实例仅接受平台内部的当前 Space 运行任务。

详情见 [原生浏览器协作决策](../adr/0048-internalize-browser-agent-task-control.md)。此阶段实现控制权和调度隔离；通用远程桌面、内核级快照及任意脚本批处理不在此能力声明中。
