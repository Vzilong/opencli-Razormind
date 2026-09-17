import { expect, test } from '@playwright/test'

const workspace = { id: 'workspace-a', name: 'Research', slug: 'research', active: true }
const options = {
  runtimes: [
    { id: 'opencli', name: 'OpenCLI Agent', available: true, installed: true, reason: null, modes: ['gui'], access_modes: ['provider'] },
    { id: 'codex', name: 'Codex', available: false, installed: null, reason: '原生运行引擎尚未接入当前会话', modes: [], access_modes: [] },
  ],
  providers: [{ id: 'provider-a', name: 'Test connection', default_model: 'model-a', models: [{ id: 'model-a', name: 'Model A' }, { id: 'model-b', name: 'Model B' }] }],
  reasoning_efforts: [],
  provider_selection_allowed: true,
  default_route_ready: true,
}

async function mockChat(page, { failOptions = false, existing = [], holdOlder = false, catalogue = options, cancelFailure = false, interruptMessage = false, interruptBeforePersistence = false, holdMessage = false, holdCancellation = false, rejectMessage = false } = {}) {
  const sessions = [...existing]
  const turns = new Map(existing.map((session) => [session.id, session.turns || []]))
  const requests = []
  const cancellations = []
  let persistPending = () => {}
  let releaseMessage
  let releaseCancellation
  const messageReady = new Promise((resolve) => { releaseMessage = resolve })
  const cancellationReady = new Promise((resolve) => { releaseCancellation = resolve })
  let releaseOlder
  const olderReady = new Promise((resolve) => { releaseOlder = resolve })
  await page.addInitScript(() => sessionStorage.setItem('opencli.bootstrapIdentityToken', 'test-token'))
  await page.route('**/api/v1/**', async (route) => {
    const request = route.request()
    const path = new URL(request.url()).pathname
    const reply = (data, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(status >= 400 ? data : { data }) })
    if (path.endsWith('/auth/me')) return reply({ subject: 'test-user', name: 'Test User', is_platform_admin: true, auth_method: 'test' })
    if (path.endsWith('/governance/workspaces') || path.endsWith('/workspaces')) return reply([workspace])
    if (path.endsWith('/chat/options')) return failOptions ? reply({ detail: 'Options unavailable' }, 503) : reply(catalogue)
    if (path.endsWith('/cancel')) {
      cancellations.push(request.postDataJSON())
      if (holdCancellation) await cancellationReady
      return cancelFailure ? reply({ detail: '取消服务暂不可用' }, 503) : reply({ accepted: true })
    }
    if (path.endsWith('/chat/sessions') && request.method() === 'GET') return reply(sessions)
    if (path.endsWith('/chat/sessions') && request.method() === 'POST') {
      const body = request.postDataJSON()
      requests.push(body)
      const created = { id: 'session-created', workspace_id: workspace.id, title: body.title, status: 'active', context_binding: body.context, execution: body.execution, revision: 0 }
      sessions.push(created)
      return reply(created)
    }
    const messageMatch = path.match(/\/chat\/sessions\/([^/]+)\/messages$/)
    if (messageMatch) {
      if (rejectMessage) return reply({ detail: 'Selected provider is disabled' }, 409)
      const body = request.postDataJSON()
      const turn = { sequence: 1, request_id: body.request_id, status: 'completed', user_content: body.content, response: { type: 'message', content: '测试服务已接收会话配置。' }, context_binding: body.context, tool_trace: [] }
      if (interruptBeforePersistence) {
        persistPending = () => turns.set(messageMatch[1], [{ ...turn, status: 'running', response: null }])
        return route.abort('connectionreset')
      }
      if (interruptMessage) {
        turns.set(messageMatch[1], [{ ...turn, status: 'running', response: null }])
        return route.abort('connectionreset')
      }
      if (holdMessage) {
        turns.set(messageMatch[1], [{ ...turn, status: 'running', response: null }])
        await messageReady
        return reply({ conversation_id: messageMatch[1], turn })
      }
      turns.set(messageMatch[1], [turn])
      return reply({ conversation_id: messageMatch[1], turn })
    }
    const detailMatch = path.match(/\/chat\/sessions\/([^/]+)$/)
    if (detailMatch) {
      if (holdOlder && detailMatch[1] === 'older') {
        await olderReady
        return reply({ id: 'older', workspace_id: workspace.id, title: 'Older conversation', status: 'active', context_binding: {}, execution: { runtime_id: 'opencli' }, turns: [] })
      }
      const session = sessions.find((item) => item.id === detailMatch[1])
      const savedTurns = turns.get(session?.id) || []
      const parameters = new URL(request.url()).searchParams
      const limit = Number(parameters.get('limit') || 50)
      const visibleTurns = parameters.get('latest') === 'true' ? savedTurns.slice(-limit) : savedTurns.slice(0, limit)
      return session ? reply({ ...session, turns: visibleTurns }) : reply({ detail: 'not found' }, 404)
    }
    return reply({})
  })
  return {
    requests, releaseOlder, cancellations, releaseMessage, releaseCancellation,
    persistPending: () => persistPending(),
    finishCancellation(sessionId) {
      turns.set(sessionId, (turns.get(sessionId) || []).map((turn) => ({ ...turn, status: 'failed', error_code: 'cancelled', error_message: '已停止当前回复；已完成的操作不会撤销。' })))
    },
  }
}

test('runtime catalogue searches and does not pretend an unconnected native CLI is available', async ({ page }) => {
  await mockChat(page)
  await page.goto('/launch?workspace=workspace-a')
  await page.getByRole('button', { name: '选择 Agent runtime' }).click()
  await page.getByRole('menuitem', { name: '其他 · All agent runtimes' }).click()
  const dialog = page.getByRole('dialog', { name: '全部 Agent 运行时' })
  await expect(dialog).toBeVisible()
  await page.getByRole('textbox', { name: '搜索运行时' }).fill('codex')
  await expect(dialog.getByText('Codex', { exact: true })).toBeVisible()
  await expect(dialog.getByText('原生运行引擎尚未接入当前会话')).toBeVisible()
  await expect(dialog.getByRole('button', { name: 'Codex', exact: true })).toHaveCount(0)
  await expect(dialog.getByRole('region', { name: '安装状态未确认' })).toBeVisible()
  await expect(dialog.getByRole('button', { name: '复制 Codex 安装命令' })).toHaveCount(0)
  await page.getByRole('textbox', { name: '搜索运行时' }).fill('no-matching-runtime')
  await expect(dialog.getByText('没有匹配的运行时')).toBeVisible()
  await page.keyboard.press('Escape')
  await expect(page.getByRole('link', { name: '插件中心', exact: true })).toBeVisible()
})

test('connection and model choice creates a durable session without modifying global defaults', async ({ page }) => {
  const api = await mockChat(page)
  await page.goto('/launch?workspace=workspace-a')
  await page.getByRole('button', { name: 'AI access：模型连接' }).click()
  await page.getByRole('menuitemradio', { name: 'Test connection', exact: true }).click()
  await page.getByRole('button', { name: '模型与推理设置' }).click()
  await page.getByRole('menuitemradio', { name: 'Model B', exact: true }).click()
  await page.getByLabel('给全局 Agent 的消息').fill('验证执行配置持久化')
  await page.getByRole('button', { name: '发送', exact: true }).click()
  await expect(page.getByText('测试服务已接收会话配置。')).toBeVisible()
  expect(api.requests[0].execution).toEqual({ runtime_id: 'opencli', mode: 'gui', provider_id: 'provider-a', model_id: 'model-b' })
  await page.reload()
  await expect(page.getByRole('button', { name: 'AI access：模型连接' })).toContainText('Test connection')
  await expect(page.getByRole('button', { name: '模型与推理设置' })).toContainText('Model B')
  await expect(page.getByRole('button', { name: '模型与推理设置' })).toBeDisabled()
  await expect(page.getByText('测试服务已接收会话配置。')).toBeVisible()
})

test('failed options load blocks sending with retry instead of assuming defaults', async ({ page }) => {
  const api = await mockChat(page, { failOptions: true })
  await page.goto('/launch?workspace=workspace-a')
  await expect(page.getByText('无法读取 Agent 执行配置，请重试。')).toBeVisible()
  await expect(page.getByRole('button', { name: '重试执行配置' })).toBeVisible()
  await page.getByLabel('给全局 Agent 的消息').fill('配置恢复前保留这份草稿')
  await expect(page.getByRole('button', { name: '发送', exact: true })).toBeDisabled()
  expect(api.requests).toEqual([])
})

test('older deep-linked session blocks input until exact restoration completes', async ({ page }) => {
  const api = await mockChat(page, { holdOlder: true })
  await page.goto('/launch?workspace=workspace-a&conversation=older')
  await expect(page.getByText('正在加载会话，暂不能发送。')).toBeVisible()
  await expect(page.getByLabel('给全局 Agent 的消息')).toBeDisabled()
  api.releaseOlder()
  await expect(page.getByRole('heading', { name: 'Older conversation' })).toBeVisible()
  await expect(page.getByLabel('给全局 Agent 的消息')).toBeEnabled()
  expect(api.requests).toEqual([])
})

test('choosing session B updates a deep link to A and survives reload', async ({ page }) => {
  const existing = ['session-a', 'session-b'].map((id) => ({ id, workspace_id: workspace.id, title: id, status: 'active', context_binding: {}, execution: { runtime_id: 'opencli' }, revision: 0 }))
  await mockChat(page, { existing })
  await page.goto('/launch?workspace=workspace-a&conversation=session-a')
  await expect(page.getByLabel('给全局 Agent 的消息')).toBeEnabled()
  await page.getByRole('button', { name: '会话历史', exact: true }).click()
  await page.getByRole('menuitem', { name: 'session-b', exact: true }).click()
  await expect(page).toHaveURL(/conversation=session-b/)
  await page.reload()
  await expect(page.getByRole('heading', { name: 'session-b' })).toBeVisible()
})

for (const [caseName, invalidCatalogue] of [
  ['non-array runtimes', { ...options, runtimes: {} }],
  ['null runtime', { ...options, runtimes: [null] }],
  ['missing models', { ...options, providers: [{ id: 'provider-a', name: 'Broken', default_model: null }] }],
]) {
  test(`invalid options are rejected without crashing: ${caseName}`, async ({ page }) => {
    const errors = []
    page.on('pageerror', (error) => errors.push(error.message))
    await mockChat(page, { catalogue: invalidCatalogue })
    await page.goto('/launch?workspace=workspace-a')
    await expect(page.getByText('无法读取 Agent 执行配置，请重试。')).toBeVisible()
    await expect(page.getByRole('button', { name: '重试执行配置' })).toBeVisible()
    expect(errors).toEqual([])
  })
}

test('unconfigured default routing keeps the draft without creating a doomed turn', async ({ page }) => {
  const api = await mockChat(page, { catalogue: { ...options, providers: [], default_route_ready: false } })
  await page.goto('/launch?workspace=workspace-a')
  await expect(page.getByText('尚未配置可用模型连接，请先配置连接后重试。')).toBeVisible()
  await page.getByLabel('给全局 Agent 的消息').fill('先写好需求，稍后配置连接')
  await expect(page.getByRole('button', { name: '发送', exact: true })).toBeDisabled()
  expect(api.requests).toEqual([])
})

const runningSession = {
  id: 'running-session', workspace_id: workspace.id, title: 'Running conversation', status: 'active', context_binding: {}, execution: { runtime_id: 'opencli' }, revision: 1,
  turns: [{ sequence: 1, request_id: 'running-request', status: 'running', user_content: '继续执行中的工作', context_binding: {}, tool_trace: [] }],
}

test('restored running conversation stops only after the server reports cancellation', async ({ page }) => {
  const api = await mockChat(page, { existing: [runningSession] })
  await page.goto('/launch?workspace=workspace-a&conversation=running-session')
  await expect(page.getByText('继续执行中的工作')).toBeVisible()
  await page.getByRole('button', { name: '停止回复', exact: true }).click()
  await expect(page.getByRole('button', { name: '正在停止回复', exact: true })).toBeDisabled()
  await expect(page.getByLabel('给全局 Agent 的消息')).toBeDisabled()
  expect(api.cancellations).toEqual([{ request_id: 'running-request' }])
  await expect(page.getByText('已停止当前回复；已完成的操作不会撤销。')).toHaveCount(0)
  api.finishCancellation('running-session')
  await expect(page.getByText('已停止当前回复；已完成的操作不会撤销。')).toBeVisible()
  await expect(page.getByLabel('给全局 Agent 的消息')).toBeEnabled()
  await expect(page.getByRole('button', { name: '发送', exact: true })).toBeVisible()
})

test('running turn beyond the first history page stays visible and stoppable', async ({ page }) => {
  const history = Array.from({ length: 51 }, (_, index) => ({ ...runningSession.turns[0], sequence: index + 1, request_id: `old-${index}`, user_content: `旧消息 ${index}`, status: 'completed' }))
  await mockChat(page, { existing: [{ ...runningSession, turns: [...history, { ...runningSession.turns[0], sequence: 52 }] }] })
  await page.goto('/launch?workspace=workspace-a&conversation=running-session')
  await expect(page.getByRole('button', { name: '停止回复', exact: true })).toBeEnabled()
  await expect(page.getByLabel('给全局 Agent 的消息')).toBeDisabled()
  await expect(page.getByText(runningSession.turns[0].user_content, { exact: true })).toBeVisible()
})

test('rejected stop does not pretend work has stopped', async ({ page }) => {
  await mockChat(page, { existing: [runningSession], cancelFailure: true })
  await page.goto('/launch?workspace=workspace-a&conversation=running-session')
  await page.getByRole('button', { name: '停止回复', exact: true }).click()
  await expect(page.getByText('取消服务暂不可用')).toBeVisible()
  await page.waitForResponse((response) => response.request().method() === 'GET' && new URL(response.url()).pathname.endsWith('/chat/sessions/running-session'))
  await expect(page.getByText('取消服务暂不可用')).toBeVisible()
  await expect(page.getByRole('button', { name: '停止回复', exact: true })).toBeEnabled()
  await expect(page.getByLabel('给全局 Agent 的消息')).toBeDisabled()
})

test('a disconnected message request does not unlock a server-side running turn', async ({ page }) => {
  await mockChat(page, { interruptMessage: true })
  await page.goto('/launch?workspace=workspace-a')
  await page.getByLabel('给全局 Agent 的消息').fill('测试连接中断后继续检查真实运行状态')
  await page.getByRole('button', { name: '发送', exact: true }).click()
  await expect(page.getByRole('button', { name: '停止回复', exact: true })).toBeEnabled()
  await expect(page.getByLabel('给全局 Agent 的消息')).toBeDisabled()
  await page.reload()
  await expect(page.getByRole('button', { name: '停止回复', exact: true })).toBeEnabled()
  await expect(page.getByLabel('给全局 Agent 的消息')).toBeDisabled()
})

test('chat controls remain inside a narrow viewport', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  await mockChat(page, { catalogue: { ...options, runtimes: detectedRuntimes } })
  await page.goto('/launch?workspace=workspace-a')
  await expect(page.getByLabel('给全局 Agent 的消息')).toBeVisible()
  const size = await page.evaluate(() => ({ documentWidth: document.documentElement.scrollWidth, viewportWidth: innerWidth }))
  expect(size.documentWidth).toBeLessThanOrEqual(size.viewportWidth)
  await page.getByRole('button', { name: '选择 Agent runtime' }).click()
  await page.getByRole('menuitem', { name: '其他 · All agent runtimes' }).click()
  const bounds = await page.getByRole('dialog', { name: '全部 Agent 运行时' }).boundingBox()
  expect(bounds.x).toBeGreaterThanOrEqual(0)
  expect(bounds.x + bounds.width).toBeLessThanOrEqual(390)
})

test('an empty poll after a disconnected POST remains unresolved until its turn appears', async ({ page }) => {
  const api = await mockChat(page, { interruptBeforePersistence: true })
  await page.goto('/launch?workspace=workspace-a')
  await page.getByLabel('给全局 Agent 的消息').fill('晚于连接中断才持久化的请求')
  await page.getByRole('button', { name: '发送', exact: true }).click()
  await page.waitForResponse((response) => response.request().method() === 'GET' && new URL(response.url()).pathname.endsWith('/chat/sessions/session-created'))
  await expect(page.getByLabel('给全局 Agent 的消息')).toBeDisabled()
  api.persistPending()
  await expect(page.getByRole('button', { name: '停止回复', exact: true })).toBeEnabled()
  api.finishCancellation('session-created')
  await expect(page.getByLabel('给全局 Agent 的消息')).toBeEnabled()
})

test('persisted cancellation unlocks composition before a delayed message response arrives', async ({ page }) => {
  const api = await mockChat(page, { holdMessage: true })
  await page.goto('/launch?workspace=workspace-a')
  await page.getByLabel('给全局 Agent 的消息').fill('等待消息响应时先确认停止')
  await page.getByRole('button', { name: '发送', exact: true }).click()
  await page.getByRole('button', { name: '停止回复', exact: true }).click()
  api.finishCancellation('session-created')
  await expect(page.getByText('已停止当前回复；已完成的操作不会撤销。')).toBeVisible()
  await expect(page.getByLabel('给全局 Agent 的消息')).toBeEnabled()
  const lateResponse = page.waitForResponse((response) => new URL(response.url()).pathname.endsWith('/messages'))
  api.releaseMessage()
  await lateResponse
  await expect(page.getByText('测试服务已接收会话配置。', { exact: true })).toHaveCount(0)
  await expect(page.getByText('已停止当前回复；已完成的操作不会撤销。')).toBeVisible()
})

test('late cancellation acceptance for turn A cannot mark turn B as stopping', async ({ page }) => {
  const api = await mockChat(page, { existing: [runningSession], holdCancellation: true, holdMessage: true })
  await page.goto('/launch?workspace=workspace-a&conversation=running-session')
  await page.getByRole('button', { name: '停止回复', exact: true }).click()
  await expect.poll(() => api.cancellations.length).toBe(1)
  api.finishCancellation('running-session')
  await expect(page.getByLabel('给全局 Agent 的消息')).toBeEnabled()
  await page.getByLabel('给全局 Agent 的消息').fill('后续新的请求 B')
  await page.getByRole('button', { name: '发送', exact: true }).click()
  await expect(page.getByRole('button', { name: '停止回复', exact: true })).toBeEnabled()
  const lateResponse = page.waitForResponse((response) => new URL(response.url()).pathname.endsWith('/cancel'))
  api.releaseCancellation()
  await lateResponse
  await expect(page.getByRole('button', { name: '停止回复', exact: true })).toBeEnabled()
  await expect(page.getByLabel('给全局 Agent 的消息')).toBeDisabled()
  api.releaseMessage()
})

test('a definitive rejection before turn creation preserves an editable draft', async ({ page }) => {
  await mockChat(page, { rejectMessage: true })
  await page.goto('/launch?workspace=workspace-a')
  const input = page.getByLabel('给全局 Agent 的消息')
  await input.fill('已选连接在发送前被停用')
  await page.getByRole('button', { name: '发送', exact: true }).click()
  await expect(page.getByText('Selected provider is disabled')).toBeVisible()
  await expect(input).toBeEnabled()
  await expect(input).toHaveValue('已选连接在发送前被停用')
  await expect(page.getByRole('button', { name: '停止回复', exact: true })).toHaveCount(0)
})

const detectedRuntimes = [
  { ...options.runtimes[1], id: 'claude', name: 'Claude Code', installed: true },
  { ...options.runtimes[1], installed: true },
  { ...options.runtimes[1], id: 'omp', name: 'Oh My Pi', installed: true },
  { ...options.runtimes[1], id: 'opencode', name: 'opencode', installed: true },
  { ...options.runtimes[1], id: 'cursor', name: 'Cursor Agent', installed: false },
  { ...options.runtimes[1], id: 'agy', name: 'Antigravity', installed: false },
  { ...options.runtimes[1], id: 'grok', name: 'Grok Build', installed: false },
  { ...options.runtimes[1], id: 'pi', name: 'Pi', installed: false },
  options.runtimes[0],
]

test('Alice catalogue separates installation from execution and selects an installed runtime without sending', async ({ page }) => {
  const api = await mockChat(page, { catalogue: { ...options, runtimes: detectedRuntimes } })
  await page.goto('/launch?workspace=workspace-a')
  await page.getByLabel('给全局 Agent 的消息').fill('保留草稿并检查 Codex 配置')
  await page.getByRole('button', { name: '选择 Agent runtime' }).click()
  const menu = page.getByRole('menu')
  await expect(menu.getByRole('menuitem').allTextContents()).resolves.toEqual(['Codex', 'Claude Code', 'Oh My Pi', 'opencode', 'OpenCLI Agent', '其他 · All agent runtimes'])
  await menu.getByRole('menuitem', { name: '其他 · All agent runtimes' }).click()
  const dialog = page.getByRole('dialog', { name: '全部 Agent 运行时' })
  await expect(dialog.getByRole('region', { name: '已安装运行时' }).getByRole('button').allTextContents()).resolves.toEqual(['Claude Code', 'Codex', 'Oh My Pi', 'opencode', 'OpenCLI Agent'])
  await expect(dialog.getByRole('region', { name: '未安装运行时' }).getByText('Cursor Agent', { exact: true })).toBeVisible()
  await expect(dialog.locator('[data-agent-runtime-icon="codex"]')).toHaveCount(1)
  await dialog.getByRole('button', { name: 'Codex', exact: true }).click()
  await expect(dialog).toBeHidden()
  await expect(page.getByRole('button', { name: '选择 Agent runtime' })).toContainText('Codex')
  await expect(page.getByLabel('给全局 Agent 的消息')).toHaveValue('保留草稿并检查 Codex 配置')
  await expect(page.getByText('原生运行引擎尚未接入当前会话')).toBeVisible()
  await expect(page.getByRole('button', { name: '发送', exact: true })).toBeDisabled()
  await expect(page.getByRole('button', { name: 'AI access：模型连接' })).toHaveCount(0)
  expect(api.requests).toEqual([])
})

for (const [runtimeId, runtimeName] of [['codex', 'Codex'], ['omp', 'Oh My Pi']]) {
  test(`ready ${runtimeId} uses its native account without an OpenCLI provider and restores after reload`, async ({ page }) => {
    const catalogue = { ...options, providers: [], default_route_ready: false, runtimes: detectedRuntimes.map((runtime) => runtime.id === runtimeId ? { ...runtime, available: true, reason: null, modes: ['gui'], access_modes: ['native'] } : runtime) }
    const api = await mockChat(page, { catalogue })
    await page.goto('/launch?workspace=workspace-a')
    await page.getByRole('button', { name: '选择 Agent runtime' }).click()
    await page.getByRole('menuitem', { name: runtimeName, exact: true }).click()
      await page.getByLabel('给全局 Agent 的消息').fill('验证原生执行')
      await expect(page.getByRole('button', { name: '重试执行配置' })).toHaveCount(0)
    await expect(page.getByRole('button', { name: '发送', exact: true })).toBeEnabled()
    await page.getByRole('button', { name: '发送', exact: true }).click()
    await expect(page.getByText('测试服务已接收会话配置。')).toBeVisible()
    expect(api.requests[0].execution).toEqual({ runtime_id: runtimeId, mode: 'gui', access_mode: 'native' })
    await page.reload()
    await expect(page.getByRole('button', { name: '选择 Agent runtime' })).toContainText(runtimeName)
    await expect(page.getByText('测试服务已接收会话配置。')).toBeVisible()
    await expect(page.getByRole('button', { name: 'AI access：模型连接' })).toHaveCount(0)
  })
}

test('an unavailable native runtime can refresh after its isolated node reconnects', async ({ page }) => {
  const native = { id: 'codex', name: 'Codex', installed: true, available: false, reason: '隔离节点未连接', modes: ['gui'], access_modes: ['native'] }
  await mockChat(page, { catalogue: { ...options, providers: [], default_route_ready: false, runtimes: [options.runtimes[0], native] } })
  await page.goto('/launch?workspace=workspace-a')
  await page.getByRole('button', { name: '选择 Agent runtime' }).click()
  await page.getByRole('menuitem', { name: 'Codex', exact: true }).click()
  await page.getByLabel('给全局 Agent 的消息').fill('节点恢复后继续发送')
  await expect(page.getByRole('button', { name: '发送', exact: true })).toBeDisabled()
  native.available = true
  native.reason = null
  await page.getByRole('button', { name: '重试执行配置' }).click()
  await expect(page.getByRole('button', { name: '发送', exact: true })).toBeEnabled()
  await expect(page.getByLabel('给全局 Agent 的消息')).toHaveValue('节点恢复后继续发送')
  await expect(page.getByRole('button', { name: '重试执行配置' })).toHaveCount(0)
})

test('missing runtime install command can be copied but is never executed', async ({ page }) => {
  await page.addInitScript(() => {
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: async (text) => { window.copiedRuntimeCommand = text } } })
  })
  const api = await mockChat(page, { catalogue: { ...options, runtimes: detectedRuntimes } })
  await page.goto('/launch?workspace=workspace-a')
  await page.getByRole('button', { name: '选择 Agent runtime' }).click()
  await page.getByRole('menuitem', { name: '其他 · All agent runtimes' }).click()
  await page.getByLabel('搜索运行时').fill('Cursor')
  await page.getByRole('button', { name: '复制 Cursor Agent 安装命令' }).click()
  await expect(page.getByText('已复制', { exact: true })).toBeVisible()
  expect(await page.evaluate(() => window.copiedRuntimeCommand)).toBe('curl https://cursor.com/install -fsS | bash')
  await expect(page.getByRole('link', { name: '打开 Cursor Agent 安装文档' })).toHaveAttribute('href', 'https://cursor.com/docs/cli/overview')
  expect(api.requests).toEqual([])
})

test('clipboard failure retains visible installation guidance', async ({ page }) => {
  await page.addInitScript(() => {
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: async () => { throw new Error('clipboard denied') } } })
  })
  await mockChat(page, { catalogue: { ...options, runtimes: detectedRuntimes } })
  await page.goto('/launch?workspace=workspace-a')
  await page.getByRole('button', { name: '选择 Agent runtime' }).click()
  await page.getByRole('menuitem', { name: '其他 · All agent runtimes' }).click()
  await page.getByLabel('搜索运行时').fill('Pi')
  await page.getByRole('button', { name: '复制 Pi 安装命令' }).click()
  await expect(page.getByRole('status')).toHaveText('无法复制，请手动选择上方命令。')
  await expect(page.getByText('npm install -g @earendil-works/pi-coding-agent', { exact: true })).toBeVisible()
})

test('a persisted conversation cannot change runtime through the installed catalogue', async ({ page }) => {
  await mockChat(page, { catalogue: { ...options, runtimes: detectedRuntimes }, existing: [{ id: 'locked-session', workspace_id: workspace.id, title: 'Locked execution', status: 'active', context_binding: {}, execution: { runtime_id: 'opencli' } }] })
  await page.goto('/launch?workspace=workspace-a&conversation=locked-session')
  await expect(page.getByRole('heading', { name: 'Locked execution' })).toBeVisible()
  await page.getByRole('button', { name: '选择 Agent runtime' }).click()
  await expect(page.getByRole('menuitem', { name: 'Codex', exact: true })).toBeDisabled()
  await page.getByRole('menuitem', { name: '其他 · All agent runtimes' }).click()
  await expect(page.getByRole('dialog').getByRole('button', { name: 'Codex', exact: true })).toBeDisabled()
  await expect(page.getByText('当前会话的执行配置已固定；新建对话后可重新选择。')).toBeVisible()
})
