# 原生浏览器控制权验收（2026-09-14）

## 范围与实现

按用户“参考直接内化进来”的选择，沿用平台 BrowserSpace、BrowserInstance 和 runtime capability，内化 Agent／人工接管能力。没有安装 ego-lite 或复制其源码，也没有引入新的依赖。

- 后端工作树提交 `2b4d1497`，集成为 `bbbf603a`：持久 `control_mode`、修订条件更新、单任务约束、权限与事件；人工模式阻止新任务和派发。
- 前端工作树提交 `082a070f`，集成为 `e2b7b5c5`：双步骤接管／归还确认、状态显示、人工模式禁用任务表单。
- 直接 capability 入口验证内部 Space 任务；Local／Redis 浏览器池在获取前后排除活动 Space 预留；账号创建遵守同一预留边界。
- 迁移 `bsc20260914a` 为旧空间设置 agent 默认值；有人工控制状态或新增审计事件时拒绝有损降级。

## 自动检查

集成树运行以下完整定向集：**124 passed，1 skipped**，155.01 秒。跳过项需要显式指定一次性 Chromium 可执行文件；真实浏览器链路另由下面的实际 HTTP／Chromium 验收覆盖。未运行 PostgreSQL 实例测试。

```text
python -m pytest tests/integration/test_browser_space_contract.py tests/unit/test_browser_account_pool.py tests/unit/test_browser_pool.py tests/unit/api/test_browser_accounts.py tests/unit/api/test_browser_spaces.py tests/unit/test_migration_heads.py -q --no-cov -x
```

前端 `tsc --noEmit` 通过；Browser Space Playwright 7 项通过，静态回归 5 项通过。Playwright 原夹具让无关页面请求携带虚构身份访问真实 API，401 导致退出登录；修正为完整模拟该页面 API 边界，产品鉴权未改动。

后端实施工作树的 scoped ruff、74 项 pool／account／API 单元检查、3 项迁移检查均通过。Sol High 独立复核未发现限定范围内的剩余阻断，另外运行 CAS、权限、内部任务证明、Local／Redis 租约复查及迁移保护共 **12 项通过**。

## 真实运行证据

使用集成 API `8130`、前端 `3130`，以及只供验收的 HTTP runtime helper `3198`。helper 操作一个真实、独立的无登录态 Chromium 页面 `https://example.org/`；它不是生产节点运行时，也不使用用户浏览器 Profile。

Space：`1b5fc883-516a-47ca-b7bb-a258de1c783a`。

| 验收动作 | 实际结果 |
| --- | --- |
| 提交 snapshot 任务 | completed，读出真实页面标题 Example Domain |
| 以当前 revision 切换 human | 200，控制模式持久保存 |
| 使用旧 revision 切换 | 409 |
| human 模式提交新任务 | 409 |
| 直接调用预留实例的 capability HTTP 入口 | 409 |
| 检查被阻断请求的浏览器副作用 | runtime invocation 计数未增加 |
| 交回 agent 后再次提交 snapshot | completed，计数只增加一次 |
| 重放控制事件 | API 过程有两条 control_changed |
| 真实页面确认接管／交回，无 API 模拟 | 两次 200，人工模式提交禁用，页面异常 0 |

验收结束已关闭这个 Space、释放预留，保留任务和事件；helper 的运行时报告转为非 READY（CONFIG_DRIFT），并已停止该测试 helper。私有原始证据位于主工作区 `.git/agent-closeout-20260912/native-control-*-evidence.json`，不提交凭据或运行配置。

## 服务恢复

用户反馈服务中断时，Docker API `8031/health` 返回 200、主栈容器健康；本轮开发进程 `3130/8130` 已退出，验收搜索容器退出码 255（OOM 标记为 false）。恢复后 `3130/knowledge`、`8130/health`、`8180/healthz` 均返回 200，运行数据库 quick_check 为 ok。

Docker `3000` 使用既有 0.4.1 前端，`/knowledge` 返回 404；根路径返回正常登录重定向。该容器没有部署本轮代码，本记录不宣称完成 Docker 发布。

## 能力边界与路由

本阶段交付平台控制权与调度隔离。通用远程桌面、ego 内核快照、任意脚本批处理仍未实现。真实模型分析和整个数据 Agent 的开源收口是另外的验收项，本记录不替代它们。

后端由 Terra High 工作树负责，UI 独立工作树负责交互，Sol High 独立审查控制边界；主代理负责合并、实际运行和服务恢复。发生的返工是补齐直接调用与浏览器池旁路检查、迁移降级保护，以及修正 E2E 夹具造成的假登录失败。

使用方式见[浏览器任务空间与人工接管](../usage/browser-agent-spaces.md)，设计来源见[ADR 0048](../adr/0048-internalize-browser-agent-task-control.md)。
