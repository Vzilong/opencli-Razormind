# 公开资料研究

研究 API 绑定到受治理工作区中的实际项目：

`/api/v1/workspaces/{workspace_id}/projects/{project_id}/research`

成员在自己的受治理工作区可读取研究成果；只有具备运行权限的成员可以启动研究。
本地或 bootstrap 平台管理员可使用现有窄 Studio 桥接，但仍必须锚定到实际 Studio
项目并使用受治理存储工作区。普通 API token 本身不授予项目资料访问。`GET /readiness` 会明确
返回网页读取、搜索和模型分析是否可用。

配置 `SEARXNG_URL` 为可访问的 SearXNG 服务根地址后，平台以
`GET {SEARXNG_URL}/search?q=...&format=json` 执行最多六条候选的搜索。未配置时，
仍可用 `seed_urls` 进行指定资料研究，响应会说明没有执行搜索。每个 URL 仅允许
HTTP/HTTPS，经过服务端 SSRF/DNS-rebinding 防护；运行最多六个来源，单页正文保留
在运行证据中并将响应摘录限制为 1200 字符。

本地开发可单独运行固定镜像的 SearXNG，并显式启用 JSON 格式：

```yaml
# settings.yml
use_default_settings: true
server:
  secret_key: "replace-with-a-random-secret"
  limiter: false
search:
  formats: [html, json]
```

```sh
docker run --rm --name opencli-searxng -p 127.0.0.1:8180:8080 \
  -v /absolute/path/settings.yml:/etc/searxng/settings.yml:ro \
  ghcr.io/searxng/searxng@sha256:e084201aa606fafce2151c8dc2844c9c3309025e90fbe7163b4f5e5183e474f0
```

设置 `SEARXNG_URL=http://127.0.0.1:8180` 和 `SEARXNG_ALLOW_PRIVATE=true` 后，
仅这个由运营者配置的搜索服务可访问回环地址。模型或用户提供的候选 URL 始终采用严格的
公开网络 SSRF 防护，不能借此访问私有地址。

提交 `POST /runs` 时使用稳定的 `request_id`，同一项目使用相同载荷重复提交会返回同一运行；
同一个 ID 配合不同载荷会得到冲突响应。项目、`request_id` 共同生成确定性的运行主键，
因此多个 API 进程的并发提交也由数据库唯一性裁决，不依赖进程内锁。接口
立即返回 `queued`；用 `GET /runs/{run_id}` 查询 `queued`、`running`、`completed`、
`partial` 或 `failed`。没有已配置的 chat 模型时，资料抓取仍完成，但结果会将它标记为
未分析，绝不会把网页摘录称为模型结论。公开资料简报少于两个不同的可读来源时只能是
`partial`。模型分析把网页正文作为不可信数据隔离，并要求每个 finding 返回已知来源 ID
及不超过 500 字符的逐字引用；引用必须能在保存的正文中精确匹配。伪造来源或引用会被
拒绝，并保留原始抓取证据供后续读取。

`competitor-watch` 只比较当前运行之前、同项目同 URL 的最后一次成功抓取。每个来源返回
`watch_status`（`baseline-established`、`new-source`、`unchanged`、`changed` 或
`fetch-failed`）及可用的 `baseline_run_id`。变化来源提供受限的前后摘录和统一 diff；
首次成功运行建立基线，相同内容不产生变化，失败来源不会覆盖已有基线。

每个运行使用数据库条件更新取得租约，SQLite 和 Postgres 都只允许一个执行者认领。启动后
会持续扫描 `queued` 和已过期租约；活跃执行者持续更新租约，租约续期失败或所有权丢失时
立即取消在途搜索、抓取或模型调用。关闭时在途任务被取消，并由到期后的周期恢复接手；
后台任务与恢复扫描的异常会写入服务日志。

平台聊天可在项目上下文中调用 `web_search`、`read_url`、研究 readiness 和研究运行读取
工具。搜索摘要只用于选择候选来源；需要引用正文时应调用 `read_url` 或创建持久研究运行。
