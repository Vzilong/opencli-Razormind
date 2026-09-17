# 首发场景真实链路验收记录（2026-09-14）

这是当前开发分支的局部运行证据，不是发布完成声明。任务与未完成验收由
[总任务 #130](https://github.com/2233admin/opencli-Razormind/issues/130) 跟踪。

## 环境与边界

- 独立 SQLite 数据库、API `127.0.0.1:8130`、前端 `127.0.0.1:3130`。
- 独立 SearXNG `127.0.0.1:8180`，JSON 搜索已启用，容器镜像 ID：
  `sha256:e084201aa606fafce2151c8dc2844c9c3309025e90fbe7163b4f5e5183e474f0`。
- 使用真实治理工作区与正式项目 bootstrap 接口创建测试项目。
- 使用临时 bootstrap 用户身份和独立 fleet 令牌；二者没有写入本文件。
- 初始阶段未配置分析模型，相关运行保留该缺口。后续本地模型补充验收见下文。
- 未修改已有生产容器、数据库或用户的主工作区改动。

## 已取得的证据

| 检查 | 实际观察 |
| --- | --- |
| 搜索服务 | 查询 Python asyncio documentation，HTTP 200，返回 20 条真实结果；部分引擎报告 CAPTCHA/超时 |
| 研究执行 | 仅输入问题，经平台搜索并抓取 Python 官方文档；运行 `05a3f60a-aec2-4043-a752-ba7c60b12a80` 最终为 `partial` |
| 来源快照 | `https://docs.python.org/3/library/asyncio.html` 抓取成功，内容哈希 `7a1327e7983b4576dfbe9f5b24ffc06779f842499684a696b32a926be280459f` |
| 有界抓取 | 另一个来源响应超过大小限制，返回失败来源与 `response exceeds body limit` 缺口 |
| 模型缺失 | `analysis_ready=false`；结果明确报告需配置模型，`findings` 为空，没有把摘录当作分析 |
| 双来源采集 | 运行 `15aa7cff-9661-4d93-a593-78f148235781` 成功保存 Python 官方文档和 BBC Cloudfit 的 asyncio 文档；两者抓取均成功，唯一缺口为模型未配置，状态保持 `partial` |
| 请求幂等 | 同一项目、请求 ID 和输入返回同一运行；同一请求 ID 修改问题返回 HTTP 409 |
| 变化基线 | 运行 `992e46bd-f6c3-421e-a7b2-c19c753a38bd` 对官方文档建立基线；下一运行 `39e261fa-413b-412c-bf3b-987a32143c7b` 返回 `unchanged` 并引用前一基线 ID |
| HTTP MCP | 使用安装的 MCP SDK 通过网络连接 `/mcp`，发现工具并成功查询记录、上下文和研究列表 |
| 用户身份边界 | REST 请求只带 fleet 令牌、没有用户 bearer，返回 HTTP 401 |
| Codex 消费 | 安装的 Codex CLI 实际调用 `opencli.get_project_research_run`，工具事件为 `completed`；返回来源 URL、哈希和缺口均与平台一致 |
| 成果检索 | 集成 `eaa5e06c` 后，REST、HTTP MCP 和原生 Codex 的 `query_project_context(q="asyncio")` 均返回 5 条持久研究来源片段，覆盖 4 个运行与 2 个来源；所有片段标为 `captured-source`，并保留各运行的缺口 |
| 真实浏览器 | 首页跳转 `/launch`，实际选择治理工作区和项目；就绪、能力、研究列表接口均为 HTTP 200，已保存的来源和监测状态可见，零页面异常 |

Codex 验收通过单次命令配置 `env_http_headers` 从子进程环境读取凭据，未修改用户全局配置。
客户端只读取已保存结果，没有启动研究或更改项目。它明确区分了采集证据与尚未完成的模型分析。

双来源运行的第二个来源为 `https://bbc.github.io/cloudfit-public-docs/asyncio/asyncio-part-1.html`，
哈希为 `5c95a18def3dad25947ff3b4ff203ac0783967f393b0a7e470ecc6c2036129cd`。

## 本地模型与有效引用补充验收

在私有验收目录使用 llama.cpp build 10955 启动 Qwen2.5-1.5B-Instruct Q4_K_M，监听
`127.0.0.1:11435`。运行文件的 SHA256 分别为
`1a615c5461e8f01c74b69d4af17f71e80190d5841cba235aee182e9053547cc9`（llama.cpp 压缩包）和
`6a1a2eb6d15622bf3c96857206351ba97e1af16c30d7a74ee38970e434e9407e`（模型）。二者只用于
本地验收，没有写入仓库、镜像或用户级配置。

平台通过正式 Provider API 保存 `provider_type=local`、无密钥、显式 loopback base URL 的
连接，模型发现、连接测试和 chat default 均成功；`analysis_ready` 从 false 变为 true。
新研究运行 `1eef9cf6-d98e-55a2-a35b-1bb81589e2c3` 抓取 Python 官方文档和 BBC Cloudfit，
最终状态为 `completed`，`gaps=[]`，包含一条 finding 及一条逐字引用。平台在分析持久化前
验证引用属于对应来源内容；验收脚本又直接读取独立 SQLite 中保留的正文进行二次核验，
确认引用逐字存在，且两个来源哈希与此前双来源运行一致。

HTTP MCP 返回同一 run、finding、引用和两个来源哈希。安装的 Codex CLI 再次实际调用
`opencli.get_project_research_run`，工具事件 completed、error=null、进程退出 0，并准确复述
finding、quote、source_id 和 `gaps=[]`。Codex 同时正确指出当前下游结果尚未暴露实际分析
provider/model 身份；因此模型执行由平台配置、运行时日志与持久引用共同证明，但单独的
下游 receipt 还不能证明模型身份，这是后续需补齐的 provenance 字段。

## 定向回归

在集成分支 `3e664e2b` 执行以下命令，结果为 **41 passed**：

```bash
python -m pytest tests/unit/test_research_service.py tests/integration/test_research_api.py tests/unit/test_agent_data_service.py tests/integration/test_agent_data_api.py tests/unit/test_agent_data_mcp.py tests/unit/test_mcp_server.py tests/integration/test_studio_agent_session_access.py tests/integration/test_agent_project_control.py --no-cov -q
```

此测试结果证明这些测试覆盖的行为；它不替代真实模型分析、浏览器流程或最终独立审查。

研究安全修复合入 `b15affc1` 后，增加研究、聊天、治理工作区及上下文回归的集成检查：
**86 passed**（34.38 秒）。命令为上述命令追加
`tests/integration/test_chat_api.py tests/integration/test_chat_failover_api.py tests/unit/api/test_workspaces.py tests/integration/test_local_workspace_api.py`。
同一集成代码的首发页面测试更新为 **9 passed**（21 秒），包括权限错误和原文引用渲染。

在集成分支 `f6407a89`，复用独立 webpack 开发服务器执行以下命令，首发页面测试
**8 passed**。该测试使用模拟接口，另有上表所述的真实浏览器检查。

```bash
PLAYWRIGHT_SMOKE_PORT=3130 PLAYWRIGHT_REUSE_EXISTING_SERVER=1 node node_modules/@playwright/test/cli.js test e2e/launch-experience.spec.mjs --reporter=line
```

## 最终复核修复后的验证

集成提交 `e7c2235b` 修复凭证值查询泄露、检索与记录输出无界、空分析被视为完成、
内部聊天返回网页全文、成果重复哈希、网页后段变化缺少差异、无模型时指定 URL 入口禁用，
以及变化详情重复显示。独立复核的代码检查未发现这八项修复的剩余缺陷。

同一组后端回归现为 **96 passed**（34.54 秒），首发页面测试 **10 passed**（25.6 秒）；
TypeScript `tsc --noEmit`、修改文件 Ruff 和 `git diff --check` 均通过。
首次前端重跑漏设复用服务器参数，尝试启动未构建的 standalone 服务器而失败；使用上面的
3130 端口和复用参数后通过，并非应用测试失败。

重新启动隔离后端后，真实浏览器在无模型状态输入 Python 官方文档 URL，点击“仅采集资料”，
POST 返回 202，创建运行 `4de0769e-92a6-5e03-b302-7465bc1fb736`。网页实际抓取成功，
运行最终为 `partial`，`findings` 为空，缺口明确列出未配置模型及只有单一来源；零页面异常。
此次未模拟浏览器接口，也未把采集摘录当作分析结果。

## 主工作区整合

已将私有起始快照到集成提交 `6d473757` 的任务增量应用回主工作区，未重置或提交用户原有改动。
应用前检查及应用后反向检查均通过；33 个受影响文件与已验证集成版本逐文件哈希一致。
整合后再次实测 5 个 REST 端点和 HTTP MCP 项目查询成功；缺少调用方身份仍返回 401。

本次采用研究、接口、体验三个隔离 worktree，由主 Agent 整合；复杂安全与最终复核使用
Sol High 专项复核。前期研究与体验采用 Terra Medium，因租约、幂等和安全边界返工升级复核。
路由验收依据为上述定向回归、独立审查和真实采集/消费证据，不以子任务自报完成为准。

## 完整目标的剩余验收

完整的“采集 → 模型分析 → 下游消费”已由上述本地模型运行实际贯通。后续也已补齐公开动态内容的真实变化、HTTP MCP 和无模拟浏览器展示，见[真实变化监测补充验收](research-watch-live-acceptance-2026-09-14.md)。当前仍需把已审功能重放到 origin-based 候选，并让下游结果携带不含秘密的分析 provider/model provenance，再做父目标独立复核。

真实浏览器检查发现的治理项目列表缺失已在 `7b2c4a0a` 修复，并通过正式接口重新验收。
独立复核发现的变化摘录类型、Codex 环境变量转发问题已在 `ef72ae8d`、`f6407a89`
修复并通过页面测试。独立复核已批准 `e7c2235b` 的八项修复；真实模型补充验收结果如上。
