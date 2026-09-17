# 管理员专用原生会话节点

本目录为 Linux/WSL 上的专用推理运行器，不是直接在控制平面启动 CLI 的快捷方式。
目前接入 Codex JSONL 和 Oh My Pi RPC；OpenCLI 继续负责工作区权限、工具执行、提案确认和对话持久化。

## 边界

- 仅平台管理员且拥有工作区运行权限可用。每个工作区/运行时必须有唯一的服务端节点映射。
- 浏览器不能传入可执行文件、节点地址、工作目录、账号或环境变量。
- 已映射的专用节点禁止通用 Operations Agent、Workbench 或其他工作区调度复用；内部派发标记不能通过任务 JSON 提供。解除映射前先停用节点并撤销其凭据副本，不能把仍带私人账号的节点转成公共执行资源。
- 原生模型只能返回文本或申请 OpenCLI 工具。允许的工具是项目/工作流读取、创建项目和更新工作流草稿；写操作只产生待确认提案。
- Codex 的 shell、unified exec、apps、plugins、MCP skill 安装、skill 搜索和多 Agent 功能禁用；其 HOME 不含用户配置、技能、历史或项目代码。
- OMP 直接复用上游 `createAgentSession` 和 `runRpcMode`，禁用原生工具、扩展发现、MCP、LSP、上下文文件和技能，并在启动时核验。不能用普通 `omp --no-tools` 代替此入口。
- bubblewrap 清空环境、隔离 PID/挂载/用户等命名空间、丢弃 capabilities，只挂载对应运行时的专用凭据副本与只读空工作目录。网络供模型 API 使用，不是断网沙箱。
- 账号副本可以由 CLI 刷新令牌；不会回写原账号文件。OAuth 轮换可能使副本过期，过期时必须由管理员重新授权，不能自动回退其他账号。
- 会话保存的是 OpenCLI 历史和执行映射指纹，不使用原生 CLI 会话恢复 ID。

## 专用节点准备

需要 Python 3.12+、bubblewrap、Node.js 和 npm。在专用 Linux/WSL 环境准备：

```sh
mkdir -p /opt/opencli-native-chat/work /opt/opencli-native-chat/credentials/{codex,omp}
chmod 700 /opt/opencli-native-chat/credentials /opt/opencli-native-chat/credentials/{codex,omp}
npm install --prefix /opt/opencli-native-chat --no-audit --no-fund \
  @openai/codex@0.153.4 bun@1.4.2 @oh-my-pi/pi-coding-agent@18.1.15
```

OMP SDK 桥接严格绑定上述版本，升级必须重新验证协议及禁用工具断言。
将本目录置于节点管理员拥有、普通工作区成员不可写的位置，确保两个 `.sh` 文件可执行。
普通成员不能修改运行器、包目录、凭据目录或 `work` 的父目录。

取得账号所有者明确授权后，使用 `provision_auth.py` 输出专用副本：

```sh
python3 provision_auth.py codex /authorized/profile/auth.json /opt/opencli-native-chat/credentials/codex/auth.json
python3 provision_auth.py omp /authorized/profile/agent.db /opt/opencli-native-chat/credentials/omp/agent.db --provider <已授权provider> --account-id <已授权账号行ID>
```

输出目标已存在时拒绝覆盖。OMP 必须明确指定 provider，只复制一个已启用 OAuth 账号；同 provider 多账号时必须指定账号行 ID，禁止自动轮换其他账号。认证版本元数据保留，设置、历史、插件或缓存数据不复制；失败不会留下半成品目标。
Windows 活跃 SQLite 数据库应在 Windows Python 上只读导出，再将导出结果复制到 WSL；不要直接通过 `/mnt/c` 打开带活跃 WAL 的源库。
不要把凭据放入仓库、日志、提示词或命令行参数。

若模型网络需要代理，在 `/opt/opencli-native-chat/proxy.json` 写入管理员控制的 `https_proxy` 地址。
该配置不接受 URL 用户名/密码。WSL NAT 中代理需要实际可达的主机地址；不要因此向整个 LAN 开放控制平面或取消防火墙。

## Fleet 配置

使用现有受认证的 WebSocket 节点入口，配置下列节点环境变量。令牌通过受保护的进程环境注入，不写入前端或命令行。

```text
AGENT_REGISTER=ws
CENTRAL_API_URL=<节点可达的控制平面地址>
AGENT_ADVERTISE_URL=<唯一的规范节点地址>
AGENT_API_TOKEN=<现有 Fleet 令牌>
AGENT_CODEX_ISOLATED_RUNNER=<本目录绝对路径>/run-codex.sh
AGENT_OMP_ISOLATED_RUNNER=<本目录绝对路径>/run-omp.sh
AGENT_CODEX_ALLOWED_ROOTS=["/opt/opencli-native-chat/work"]
AGENT_OMP_ALLOWED_ROOTS=["/opt/opencli-native-chat/work"]
```

控制平面环境变量 `NATIVE_CHAT_BINDINGS` 是 JSON 数组，分别配置两个运行时：

```json
[
  {"workspace_id":"<已授权工作区ID>","runtime_id":"codex","agent_url":"<规范节点地址>","cwd":"/opt/opencli-native-chat/work","timeout_seconds":180},
  {"workspace_id":"<已授权工作区ID>","runtime_id":"omp","agent_url":"<规范节点地址>","cwd":"/opt/opencli-native-chat/work","timeout_seconds":180}
]
```

只有 exact node 在线且已发布 `operator_chat` 能力时才可选择执行。
映射改变后旧对话拒绝重新执行，用户必须新建对话确认新配置。不同工作区不要共用凭据/工作目录，除非账号所有者另外授权。

## 验证与运维

1. 运行 `run-codex.sh --version` 与 `run-omp.sh --operator-chat-probe`，后者必须声明所有原生工具、MCP、扩展与用户上下文均已禁用。
2. 验证沙箱中看不到主机代码、控制平面秘密或另一个运行时的凭据；工作目录不可写。
3. 管理员在 `/launch?workspace=...` 新建对话，选择运行时，真实发送、刷新恢复、连续追问、读取项目、检查待确认提案与停止行为。
4. 停止只有在节点确认进程树清理完成后才成为终态。节点断线/清理不明时保留运行锁，不要通过超时或改数据库伪造“已停止”。
5. Next.js API rewrite 超时设为 660 秒，覆盖单次最长 600 秒原生执行与清理窗口，避免默认 30 秒代理提前断线。多次工具推理仍可能触及请求上限；前端断开会触发受控停止。

当前入口是 GUI 受控推理，不宣称提供 Alice 全部 TUI、原生文件编辑、插件、模型选择、搜索或其他运行时。未配置的能力必须保留明确不可用状态。
