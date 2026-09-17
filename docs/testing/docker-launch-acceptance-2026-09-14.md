# Docker 首发镜像运行验收（2026-09-14）

本记录验证本地生产镜像构建和独立容器中的实际链路，不代表镜像发布或完整模型分析验收。跟踪目标为 #130。

## 修复与构建

发现旧 Dockerfile 的默认末阶段为 `non-bypass-acceptance`，会携带测试用 `OPENCLI_BIN`；单独构建旧 `runtime` 又只有 Python 默认命令。实际旧镜像检查得到 `Entrypoint=null`、`Cmd=["python3"]`。

工作树提交 `ae522a9e` 已整合为 `295989dd`：将公共启动配置放入 runtime，默认末阶段改为只继承 runtime 的 production，本地 Compose 明确选择 production。显式验收 target 继续保留。`44461632` 同时修正发布契约测试中已经失效的 noVNC 旧实现断言，继续验证默认 6080、环境可配置和 loopback 绑定。

- 后端：源码 `44461632`，本地镜像 `opencli-launch-api:candidate`，manifest list `sha256:e28d39224ec88f5cc628901db46caf90ae4d278f6463fd1974d202b1a04f5f91`。
- 前端：源码 `70c1d6e1`，镜像 `opencli-launch-frontend:70c1d6e1`，manifest list `sha256:8bdbf7e6e572c445adfd43ca73a4e01729e681088f97df69992ce57ed3c95bc2`。后续两个提交没有前端改动。
- 默认后端镜像内实际确认 `/entrypoint.sh` 与 uvicorn 启动命令存在，`OPENCLI_BIN`、`III_CLI_PATH` 及 `/opt/non-bypass` 不存在。
- 前端 frozen lockfile 安装、优化构建、TypeScript 和页面生成全部通过。Docker Hub 在本机解析连接超时，构建时通过命名 context 使用 `public.ecr.aws/docker/library/node` 的 `sha256:ef24c5053d50fdc3e4e56eb4e7ddb7861874ab0fdc797046ba897581deb8e868` 镜像；未改动仓库的 registry 或依赖配置。
- 镜像及发布契约定向检查 **8 passed**；Sol High 独立复核生产／验收阶段边界通过，并独立运行 2 项包装检查。

## 独立容器与真实消费

Compose 项目 `opencli-launch-image-20260914` 使用新命名数据卷。API 为 `127.0.0.1:8230`，前端为 `127.0.0.1:3230`；没有复用或修改原 Docker 3000/8031 的数据与容器，也没有替换 3130/8130 的原生验收服务。凭据只通过本地忽略文件注入，不进入镜像构建参数或本记录。

新数据卷复用原生验收环境的现有本地密码哈希与初始化标记，两者均由 appuser 持有且权限为 0600；实际加载验证通过，没有更改密码。下面的自动浏览器验收使用 bootstrap 身份，不宣称已经代替用户做过密码登录。

| 检查 | 实际证据 |
| --- | --- |
| 全新数据库启动 | 容器健康检查通过，迁移到 `bsc20260914a` |
| 治理项目创建 | 正式 workspace 与 project bootstrap API 成功 |
| 真实公开抓取 | 运行 `27077663-becb-5b71-a4a9-39a27a2081e2`，Python 官方与 BBC Cloudfit 两篇 asyncio 页面均 fetched |
| 成果状态 | partial；未配置分析模型，findings 为空且缺口保留 |
| HTTP MCP | 正式 SDK 读取同一 run，两个来源的内容哈希与 REST 一致 |
| 下游 Codex | 安装的 CLI 实际调用 `opencli.get_project_research_run`，工具事件 completed、error=null，进程退出 0；回答明确区分采集与分析 |
| 无身份请求 | 仅保留 fleet 令牌时 API 返回 401 |
| 生产前端 | 首页进入 `/launch`；可选新工作区／项目并看到两个真实来源；7 个 API 响应成功，页面异常 0 |
| 知识库路由 | `/knowledge` 返回 200，实际知识库页面标题可见 |

Python 文档哈希为 `229daeb0c2748b82671849a4471080b267a3efa5a070718e75f696d7b32875f0`，BBC 文档哈希为 `5c95a18def3dad25947ff3b4ff203ac0783967f393b0a7e470ecc6c2036129cd`。私有原始证据和可重放脚本保留在主工作区 `.git/agent-closeout-20260912/` 的 `docker-image-*`、`docker-runtime-context.json`、`codex-docker-*` 文件中。

仅输入问题、不提供 URL 的真实搜索重试运行 `f69c2236-41a0-5eff-8175-880995f48767` 已通过：SearXNG 找到 Python 官方与 Reddit 页面，两个来源均抓取并持久化，HTTP MCP 返回相同哈希；状态 partial，仅保留模型未配置缺口。此前运行 `9f9a97e4-39b9-5757-916e-ca7ca37b42d5` 返回空搜索结果，旧代码错误提示“未配置 SEARXNG_URL”，该失败证据保留。`340ee064` 修正了空搜索诊断，三项定向回归通过；重试使用新 request_id，没有覆盖历史失败或绕过搜索。

## 安全增量后的镜像复验

最终安全集成为 `d0196bed`，来源为 #139 原始代码系列及隔离工作树 `6886966d`、`7597526c`，并修复独立复核发现的 `[REDACTED]` 前缀旁路。失败来源和 HTTP 日志 URL 仅保留 origin，失败消息与事件使用稳定错误码；成功来源仍保留可查询的引用 URL。256,000-byte 解码响应上限和 48,000 字符正文上限保留。

- 主代理及 Sol High 独立复核分别验证 **50 passed**；额外路径、未知参数、JSON、结构化日志及伪造脱敏标记反例通过。Ruff、diff-check 通过。未把曾中止的全量 unit 运行计为通过，也未执行 Postgres 验收。
- 当前后端镜像为 `opencli-launch-api:security-d0196bed`，manifest list `sha256:919e011c21ef1a3f1347d3f8f35bda7edf01cd9b41c54853a265380a4ab42e5b`。独立 Compose API 更新后健康；前端镜像保持不变。
- 新镜像实际检查确认生产启动命令、验收替身缺席和 HTTP 日志清洗均通过。
- 新两源运行 `ef97075b-689d-53bc-9559-23667fd9f815` 再次抓取 Python 与 BBC，REST/HTTP MCP 哈希一致，无身份请求仍为 401。
- 安装的 Codex CLI 再次真实读取该新运行，工具调用 completed、error=null，进程退出 0；回答准确保留未进行模型分析的缺口。
- 新无 URL 运行 `536c792c-0873-5a16-a1e5-a25cd2e99f11` 通过真实搜索抓到 Python 官方来源，另一个 Medium 来源失败，失败 URL 正确缩减为 origin；结果如实保留来源不足与模型缺口，未被提升为 completed。
- 3230 真实浏览器复验：知识库 200、7 个 API 响应成功、页面异常 0；3130/3230 知识库与 8130/8230 健康端点均为 200。
- 原生 8130 后端已从相同集成源码重启，原有工作区认证读取成功，Provider 仍为空。主工作区同步 7 个文件后 SHA256 全部匹配，暂存树哈希保持不变。

## 剩余范围

真实 chat 模型尚未配置，完整“采集 → 有效引用的分析 → 下游消费”仍未通过。研究失败与 HTTP 日志脱敏补强已进入上述新镜像并通过复核。最终 origin-based 集成与父目标复核仍按 #138／#130 合同推进；本地服务恢复不等于关闭整个首发目标。

路由：Luna High 独立工作树负责 Docker 阶段与陈旧测试的最小修复；Sol High 只读复核边界；主代理执行实际镜像构建、独立容器运行、浏览器与 Codex 消费验收。没有推送镜像或私有快照分支。
