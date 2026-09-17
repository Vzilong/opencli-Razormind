import { expect, test } from '@playwright/test'

const workspace = { id: 'launch-workspace', name: 'Launch research', slug: 'launch-research', active: true }
const workspacePreferenceKey = (authMethod, subject) => `opencli:studio-workspace:v2:${encodeURIComponent(JSON.stringify([authMethod, subject]))}`
const launchPreferenceKey = workspacePreferenceKey('test', 'launch-user')
const project = {
  id: 'launch-project', workspace_id: workspace.id, name: 'Search decision', slug: 'search-decision',
  description: 'Evidence for a technical decision', app_type: 'workflow', primary_workflow_id: 'launch-workflow',
  created_by_user_id: 'launch-user', archived: false, created_at: '2026-09-12T00:00:00Z', updated_at: '2026-09-12T00:00:00Z',
}

const reply = (data) => ({ contentType: 'application/json', body: JSON.stringify({ success: true, data }) })

async function fixtures(page, {
  readiness = { fetch_ready: true, search_ready: true, analysis_ready: true, missing: [] },
  workspaceList = [workspace],
  projectsByWorkspace = { [workspace.id]: [project] },
} = {}) {
  const requests = { starts: [], contexts: [] }
  let runPolls = 0
  await page.addInitScript(() => sessionStorage.setItem('opencli.bootstrapIdentityToken', 'launch-test-token'))
  await page.route('**/api/**', async (route) => {
    const request = route.request()
    const path = new URL(request.url()).pathname
    if (path.endsWith('/auth/me')) return route.fulfill(reply({ subject: 'launch-user', name: 'Launch Tester', auth_method: 'test', is_platform_admin: true }))
    if (path.endsWith('/governance/workspaces')) return route.fulfill(reply(workspaceList))
    const workspaceProjects = path.match(/^\/api\/v1\/governance\/workspaces\/([^/]+)\/projects$/)
    if (workspaceProjects) return route.fulfill(reply(projectsByWorkspace[workspaceProjects[1]] ?? []))
    if (path.endsWith('/chat/sessions')) return route.fulfill(reply([]))
    if (path.endsWith('/research/readiness')) return route.fulfill(reply(readiness))
    if (path.endsWith('/agent-data/capabilities')) return route.fulfill(reply({ records: true, context: true, research: true, mcp_path: '/mcp' }))
    if (path.endsWith('/research/runs') && request.method() === 'POST') {
      requests.starts.push(request.postDataJSON())
      return route.fulfill(reply({ id: 'run-1', workspace_id: workspace.id, project_id: project.id, template_id: request.postDataJSON().template_id, status: 'queued', created_at: '2026-09-12T00:00:00Z', updated_at: '2026-09-12T00:00:00Z', result: null, error: null }))
    }
    if (path.endsWith('/research/runs')) {
      runPolls += 1
      const status = runPolls > 1 ? 'partial' : 'running'
      return route.fulfill(reply([{
        id: 'run-1', workspace_id: workspace.id, project_id: project.id, template_id: 'research-brief', status,
        created_at: '2026-09-12T00:00:00Z', updated_at: '2026-09-12T00:00:03Z', error: null,
        result: status === 'partial' ? { summary: '仅找到一个可验证来源。', findings: [{ text: '官方资料列出部署限制。', source_ids: ['source-1'], evidence: [{ source_id: 'source-1', quote: 'Deployment constraints' }] }], sources: [{ id: 'source-1', url: 'https://example.test/docs', title: 'Official docs', fetched_at: '2026-09-12T00:00:02Z', content_hash: 'hash', excerpt: 'Deployment constraints', status: 'fetched' }], gaps: ['缺少第二个来源'], changes: [] } : null,
      }]))
    }
    if (path.endsWith('/agent-data/context')) {
      requests.contexts.push(new URL(request.url()).searchParams.get('q'))
      return route.fulfill(reply({ query: requests.contexts.at(-1), matches: [{ id: 'record-1', text: '项目接口采用 OAuth。', source: { title: '接口说明 v2' } }], gaps: ['未找到轮换策略'] }))
    }
    return route.fulfill(reply([]))
  })
  return requests
}

test('runs research using the project-scoped contract and renders partial evidence', async ({ page }) => {
  const requests = await fixtures(page)
  await page.goto('/launch')
  await expect(page.getByRole('heading', { name: '为你的 Agent 准备好数据' })).toBeVisible()
  await page.getByLabel('研究问题').fill('比较两个搜索方案')
  await page.getByRole('button', { name: '开始研究' }).click()
  await expect.poll(() => requests.starts.length).toBe(1)
  expect(requests.starts[0]).toMatchObject({ template_id: 'research-brief', question: '比较两个搜索方案', max_sources: 5 })
  await expect(page.getByText('不完整', { exact: true })).toBeVisible({ timeout: 6_000 })
  await expect(page.getByText('官方资料列出部署限制。')).toBeVisible()
  await expect(page.locator('blockquote').getByText('Deployment constraints', { exact: true })).toBeVisible()
  await expect(page.getByRole('link', { name: 'Official docs', exact: true })).toHaveAttribute('href', 'https://example.test/docs')
  await expect(page.getByText('缺少第二个来源')).toBeVisible()
})

test('explains denied research execution without requesting project creation rights', async ({ page }) => {
  await fixtures(page)
  await page.route('**/research/runs', async (route) => {
    if (route.request().method() === 'POST') return route.fulfill({ status: 403, contentType: 'application/json', body: JSON.stringify({ detail: 'Permission required: run_operations_agents' }) })
    return route.fallback()
  })
  await page.goto('/launch')
  await page.getByLabel('研究问题').fill('读取已有资料后开展研究')
  await page.getByRole('button', { name: '开始研究' }).click()
  await expect(page.getByText('当前成员没有运行研究任务的权限。请联系工作区管理员确认当前项目的授权。')).toBeVisible()
})

test('blocks a research submission when server readiness is incomplete', async ({ page }) => {
  await fixtures(page, { readiness: { fetch_ready: false, search_ready: false, analysis_ready: false, missing: ['search', 'analysis'] } })
  await page.goto('/launch')
  await expect(page.getByText('缺少：search、analysis')).toBeVisible()
  await expect(page.getByRole('button', { name: '开始研究' })).toBeDisabled()
})

test('requires search readiness when research has no reference URLs', async ({ page }) => {
  await fixtures(page, { readiness: { fetch_ready: true, search_ready: false, analysis_ready: true, missing: ['search'] } })
  await page.goto('/launch')
  await page.getByLabel('研究问题').fill('比较两个搜索方案')
  await expect(page.getByText('未提供参考 URL 时，需要搜索服务就绪后才能研究。')).toBeVisible()
  await expect(page.getByRole('button', { name: '开始研究' })).toBeDisabled()
  await page.getByLabel('参考 URL（可选）').fill('https://example.test/source')
  await expect(page.getByRole('button', { name: '开始研究' })).toBeEnabled()
})

test('allows explicit URL collection without a model and explains the partial result', async ({ page }) => {
  const requests = await fixtures(page, { readiness: { fetch_ready: true, search_ready: false, analysis_ready: false, missing: ['analysis'] } })
  await page.goto('/launch')
  await page.getByLabel('研究问题').fill('保存公开资料供稍后分析')
  await expect(page.getByRole('button', { name: '开始研究' })).toBeDisabled()
  await page.getByLabel('参考 URL（可选）').fill('https://example.test/docs')
  await expect(page.getByText('未配置分析模型；本次仅采集资料，结果会保留缺口并标为不完整。')).toBeVisible()
  await page.getByRole('button', { name: '仅采集资料', exact: true }).click()
  await expect.poll(() => requests.starts.length).toBe(1)
  expect(requests.starts[0].seed_urls).toEqual(['https://example.test/docs'])
})

test('reuses the request id when a submission response is lost and retried', async ({ page }) => {
  const requestIds = []
  let attempts = 0
  await page.addInitScript(() => sessionStorage.setItem('opencli.bootstrapIdentityToken', 'launch-retry-token'))
  await page.route('**/api/**', async (route) => {
    const request = route.request()
    const path = new URL(request.url()).pathname
    if (path.endsWith('/auth/me')) return route.fulfill(reply({ subject: 'launch-user', name: 'Launch Tester', is_platform_admin: true }))
    if (path.endsWith('/governance/workspaces')) return route.fulfill(reply([workspace]))
    if (path === `/api/v1/governance/workspaces/${workspace.id}/projects`) return route.fulfill(reply([project]))
    if (path.endsWith('/research/readiness')) return route.fulfill(reply({ fetch_ready: true, search_ready: true, analysis_ready: true, missing: [] }))
    if (path.endsWith('/agent-data/capabilities')) return route.fulfill(reply({ records: true, context: true, research: true, mcp_path: '/mcp' }))
    if (path.endsWith('/research/runs') && request.method() === 'POST') {
      requestIds.push(request.postDataJSON().request_id)
      attempts += 1
      if (attempts === 1) return route.fulfill({ status: 502, contentType: 'application/json', body: JSON.stringify({ detail: 'upstream timeout' }) })
      return route.fulfill(reply({ id: 'run-retry', workspace_id: workspace.id, project_id: project.id, template_id: 'research-brief', status: 'queued', created_at: '2026-09-12T00:00:00Z', updated_at: '2026-09-12T00:00:00Z', result: null, error: null }))
    }
    if (path.endsWith('/research/runs')) return route.fulfill(reply([]))
    return route.fulfill(reply([]))
  })
  await page.goto('/launch')
  await page.getByLabel('研究问题').fill('重试同一研究')
  await page.getByRole('button', { name: '开始研究' }).click()
  await expect.poll(() => requestIds.length).toBe(1)
  await page.getByRole('button', { name: '开始研究' }).click()
  await expect.poll(() => requestIds.length).toBe(2)
  expect(requestIds[0]).toBe(requestIds[1])
})

test('renders competitor changes and explains an unchanged baseline', async ({ page }) => {
  await page.addInitScript(() => sessionStorage.setItem('opencli.bootstrapIdentityToken', 'launch-watch-token'))
  await page.route('**/api/**', async (route) => {
    const request = route.request()
    const path = new URL(request.url()).pathname
    if (path.endsWith('/auth/me')) return route.fulfill(reply({ subject: 'launch-user', name: 'Launch Tester', is_platform_admin: true }))
    if (path.endsWith('/governance/workspaces')) return route.fulfill(reply([workspace]))
    if (path === `/api/v1/governance/workspaces/${workspace.id}/projects`) return route.fulfill(reply([project]))
    if (path.endsWith('/research/readiness')) return route.fulfill(reply({ fetch_ready: true, search_ready: true, analysis_ready: true, missing: [] }))
    if (path.endsWith('/agent-data/capabilities')) return route.fulfill(reply({ records: true, context: true, research: true, mcp_path: '/mcp' }))
    if (path.endsWith('/research/runs') && request.method() === 'POST') return route.fulfill(reply({ id: 'watch-1', workspace_id: workspace.id, project_id: project.id, template_id: 'competitor-watch', status: 'queued', created_at: '2026-09-12T00:00:00Z', updated_at: '2026-09-12T00:00:00Z', result: null, error: null }))
    if (path.endsWith('/research/runs')) return route.fulfill(reply([{ id: 'watch-1', workspace_id: workspace.id, project_id: project.id, template_id: 'competitor-watch', status: 'completed', created_at: '2026-09-12T00:00:00Z', updated_at: '2026-09-12T00:00:02Z', error: null, result: { summary: '检测到价格页更新。', findings: [], sources: [{ id: 'source-a', url: 'https://example.test/pricing', title: 'Pricing', fetched_at: '2026-09-12T00:00:02Z', content_hash: 'after-hash', excerpt: '', status: 'fetched', watch_status: 'changed', baseline_run_id: 'watch-baseline', change_excerpt: { before: '价格：10', after: '价格：12', diff: '-价格：10\n+价格：12' } }], gaps: [], changes: [{ source_id: 'source-a', url: 'https://example.test/pricing', baseline_run_id: 'watch-baseline', before_content_hash: 'before-hash', after_content_hash: 'after-hash', change_excerpt: { before: '价格：10', after: '价格：12', diff: '-价格：10\n+价格：12' } }] } }]))
    return route.fulfill(reply([]))
  })
  await page.goto('/launch')
  await page.getByRole('button', { name: /竞品变化跟踪/ }).click()
  await page.getByLabel('要跟踪的公开 URL').fill('https://example.test/pricing')
  await page.getByRole('button', { name: '建立或比较基线' }).click()
  await expect(page.getByText('页面变化与基线状态')).toBeVisible()
  await expect(page.getByText('变更摘要：-价格：10', { exact: false })).toHaveCount(1)
  await expect(page.getByText('变更前：价格：10')).toBeVisible()
  await expect(page.getByText('变更后：价格：12')).toBeVisible()
  await expect(page.getByText('基线任务：watch-baseline', { exact: false })).toBeVisible()
})

test('shows copyable Codex and Claude MCP configurations with environment references', async ({ page }) => {
  await fixtures(page)
  await page.goto('/launch')
  await expect(page.getByRole('link', { name: '查看所有项目' })).toHaveAttribute('href', `/studio?workspace=${workspace.id}`)
  await expect.poll(() => page.evaluate((storageKey) => localStorage.getItem(storageKey), launchPreferenceKey)).toBe(workspace.id)
  await expect(page.getByText('Codex MCP 配置', { exact: true })).toBeHidden()
  await page.getByText('查看 Agent 接入配置', { exact: true }).click()
  await expect(page.getByText('Codex MCP 配置', { exact: true })).toBeVisible()
  await expect(page.getByText('OPENCLI_MCP_CALLER_TOKEN', { exact: false }).first()).toBeVisible()
  await expect(page.getByText('env_vars = ["OPENCLI_ADMIN_API_URL", "API_AUTH_TOKEN", "OPENCLI_MCP_CALLER_TOKEN"]', { exact: false })).toBeVisible()
  await expect(page.getByText('query_project_context', { exact: false })).toBeVisible()
  await expect(page.getByRole('button', { name: '复制Codex MCP 配置' })).toBeVisible()
})

test('restores the last accessible workspace instead of resetting to the first workspace', async ({ page }) => {
  const rememberedWorkspace = { id: 'remembered-workspace', name: 'Remembered workspace', slug: 'remembered-workspace', active: true }
  const rememberedProject = { ...project, id: 'remembered-project', workspace_id: rememberedWorkspace.id, name: 'Remembered project' }
  await page.addInitScript(({ key, workspaceId }) => localStorage.setItem(key, workspaceId), { key: launchPreferenceKey, workspaceId: rememberedWorkspace.id })
  await fixtures(page, {
    workspaceList: [workspace, rememberedWorkspace],
    projectsByWorkspace: { [workspace.id]: [project], [rememberedWorkspace.id]: [rememberedProject] },
  })
  await page.goto('/launch')
  await expect(page).toHaveURL(new RegExp(`workspace=${rememberedWorkspace.id}`))
  await expect(page.getByRole('combobox', { name: '工作区', exact: true })).toContainText(rememberedWorkspace.name)
  await expect(page.getByRole('combobox', { name: '项目', exact: true })).toContainText(rememberedProject.name)
  await expect(page.getByRole('link', { name: '查看所有项目' })).toHaveAttribute('href', `/studio?workspace=${rememberedWorkspace.id}`)
  await page.evaluate((workspaceId) => {
    const url = new URL(window.location.href)
    url.searchParams.set('workspace', workspaceId)
    window.history.pushState(window.history.state, '', url)
    window.dispatchEvent(new PopStateEvent('popstate', { state: window.history.state }))
  }, workspace.id)
  await expect(page.getByRole('combobox', { name: '工作区', exact: true })).toContainText(workspace.name)
  await expect(page.getByRole('combobox', { name: '项目', exact: true })).toContainText(project.name)
  await expect(page.getByRole('link', { name: '查看所有项目' })).toHaveAttribute('href', `/studio?workspace=${workspace.id}`)
  await page.getByRole('combobox', { name: '工作区', exact: true }).click()
  await page.getByRole('option', { name: rememberedWorkspace.name, exact: true }).click()
  await expect(page).toHaveURL(new RegExp(`workspace=${rememberedWorkspace.id}`))
  await expect(page.getByRole('combobox', { name: '工作区', exact: true })).toContainText(rememberedWorkspace.name)
  await expect(page.getByRole('combobox', { name: '项目', exact: true })).toContainText(rememberedProject.name)
})

test('gives a platform admin an actionable first-workspace setup path', async ({ page }) => {
  let created = null
  await page.addInitScript(() => sessionStorage.setItem('opencli.bootstrapIdentityToken', 'launch-empty-token'))
  await page.route('**/api/**', async (route) => {
    const request = route.request()
    const path = new URL(request.url()).pathname
    if (path.endsWith('/auth/me')) return route.fulfill(reply({ subject: 'launch-admin', name: 'Launch Admin', is_platform_admin: true }))
    if (path.endsWith('/governance/workspaces')) return route.fulfill(reply([]))
    if (path.endsWith('/platform/workspaces') && request.method() === 'POST') {
      created = request.postDataJSON()
      return route.fulfill(reply({ id: 'first-workspace', name: created.name, slug: created.slug }))
    }
    if (path.endsWith('/chat/sessions')) return route.fulfill(reply([]))
    return route.fulfill(reply([]))
  })
  await page.goto('/launch')
  await expect(page.getByText('创建第一个工作区')).toBeVisible()
  await expect(page.getByText('先创建或选择一个项目，再检查可用能力。')).toBeVisible()
  await expect(page.getByRole('button', { name: '开始研究', exact: true })).toBeDisabled()
  await page.getByLabel('新工作区名称').fill('首次研究')
  await page.getByRole('button', { name: '创建工作区' }).click()
  await expect.poll(() => created?.first_admin_subject).toBe('launch-admin')
  expect(created.name).toBe('首次研究')
})

for (const scope of ['工作区', '项目']) {
  test(`distinguishes a failed ${scope} list from an empty scope and supports retry`, async ({ page }) => {
    await fixtures(page)
    let failing = true
    const path = scope === '工作区' ? '**/governance/workspaces' : `**/governance/workspaces/${workspace.id}/projects`
    await page.route(path, (route) => failing
      ? route.fulfill({ status: 503, contentType: 'application/json', body: JSON.stringify({ detail: 'Temporary list failure' }) })
      : route.fallback())
    await page.goto('/launch')
    const retry = page.getByRole('button', { name: `重试${scope}列表` })
    await expect(retry).toBeVisible({ timeout: 15_000 })
    await expect(page.getByText('创建第一个工作区')).toBeHidden()
    await expect(page.getByText('此工作区还没有项目。', { exact: false })).toBeHidden()
    await expect(page.getByRole('button', { name: '开始研究', exact: true })).toBeDisabled()
    failing = false
    await retry.click()
    await expect(retry).toBeHidden()
    await expect(page.getByRole('combobox', { name: '项目', exact: true })).toContainText(project.name)
  })
}

test('queries project context without starting a research run', async ({ page }) => {
  const requests = await fixtures(page)
  await page.goto('/launch')
  await page.getByRole('button', { name: /项目知识供给/ }).click()
  await page.getByLabel('项目资料问题').fill('接口如何鉴权？')
  await page.getByRole('button', { name: '查询资料' }).click()
  await expect(page.getByText('项目接口采用 OAuth。')).toBeVisible()
  expect(requests.contexts).toEqual(['接口如何鉴权？'])
  expect(requests.starts).toEqual([])
})
