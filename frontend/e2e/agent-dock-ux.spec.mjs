import { expect, test } from '@playwright/test'
import { readFile } from 'node:fs/promises'

const workspace = {
  id: 'workspace-a',
  name: 'Research',
  slug: 'research',
  active: true,
  created_at: '2026-09-01T00:00:00Z',
  updated_at: '2026-09-01T00:00:00Z',
}

test('switching a conversation URL invalidates delayed Agent requests in the same Workspace', async () => {
  const source = await readFile(new URL('../components/shell/global-agent-dock.tsx', import.meta.url), 'utf8')
  expect(source).toContain('}, [workspaceId, context.project_id, context.workflow_id, context.run_id, requestedConversationId, setActiveRequestId])')
  expect(source).toContain('const requestGeneration = requestGenerationRef.current')
  expect(source).toContain('requestGenerationRef.current !== requestGeneration')
})

async function useAgentApi(page, { holdSessions = false } = {}) {
  let releaseSessions
  const sessionsReady = new Promise((resolve) => {
    releaseSessions = resolve
  })
  let sentMessages = 0
  let failedTurn = null

  await page.addInitScript(() => {
    sessionStorage.setItem('opencli.bootstrapIdentityToken', 'test-token')
  })
  await page.route('**/api/v1/**', async (route) => {
    const url = new URL(route.request().url())
    const reply = (data, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(status >= 400 ? data : { data }) })
    if (url.pathname.endsWith('/auth/me')) return reply({ subject: 'test-user', name: 'Test User', username: 'test', is_platform_admin: true, auth_method: 'test' })
    if (url.pathname.endsWith('/governance/workspaces') || url.pathname.endsWith('/workspaces')) return reply([workspace])
    if (url.pathname.endsWith('/chat/options')) return reply({ runtimes: [{ id: 'opencli', name: 'OpenCLI Agent', available: true, installed: true, reason: null, modes: ['gui'], access_modes: ['provider'] }], providers: [], reasoning_efforts: [], provider_selection_allowed: true, default_route_ready: true })
    if (url.pathname.endsWith('/chat/sessions') && route.request().method() === 'GET') {
      if (holdSessions) await sessionsReady
      return reply([])
    }
    if (url.pathname.endsWith('/chat/sessions') && route.request().method() === 'POST') {
      return reply({ id: 'conversation-a', workspace_id: workspace.id, title: null, status: 'active', context_binding: {}, revision: 0 })
    }
    if (url.pathname.endsWith('/chat/sessions/conversation-a/messages')) {
      sentMessages += 1
      const body = route.request().postDataJSON()
      failedTurn = { sequence: sentMessages, request_id: body.request_id, user_content: body.content, status: 'failed', error_message: 'Agent service unavailable', tool_trace: [], context_binding: {} }
      return reply({ conversation_id: 'conversation-a', turn: failedTurn })
    }
    if (url.pathname.endsWith('/chat/sessions/conversation-a')) {
      return reply({ id: 'conversation-a', workspace_id: workspace.id, title: null, status: 'active', context_binding: {}, turns: failedTurn ? [failedTurn] : [] })
    }
    return reply({})
  })

  return { releaseSessions, sentMessages: () => sentMessages }
}

test('Alice chat stays inside the original application shell and preserves failed drafts', async ({ page }) => {
  const api = await useAgentApi(page)
  await page.goto('/launch?workspace=workspace-a')

  await expect(page.getByRole('link', { name: '插件中心', exact: true })).toBeVisible()
  await expect(page.getByRole('link', { name: '执行资源', exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Toggle Sidebar', exact: true })).toBeVisible()
  await expect(page.getByTestId('alice-chat').locator('aside')).toHaveCount(0)

  const input = page.getByLabel('给全局 Agent 的消息')
  await expect(input).toBeEnabled()
  await page.getByRole('button', { name: '收集网站中的产品信息', exact: true }).click()
  await expect(input).toHaveValue(/帮我收集这些网站的产品信息/)
  await expect(input).toBeFocused()
  await expect(page.getByTestId('harness-landing-suggestions')).toBeHidden()
  expect(api.sentMessages()).toBe(0)

  await page.getByRole('button', { name: '发送', exact: true }).click()
  await expect(page.getByTestId('alice-chat').getByRole('alert')).toContainText('Agent service unavailable')
  await expect(input).toHaveValue(/帮我收集这些网站的产品信息/)
  expect(api.sentMessages()).toBe(1)

  await page.getByRole('button', { name: '新建对话', exact: true }).click()
  await expect(input).toHaveValue('')
  await expect(page.getByTestId('harness-landing-suggestions')).toBeVisible()
  await expect(page.getByRole('link', { name: '插件中心', exact: true })).toBeVisible()
})

test('Agent dock keeps a failed draft, fills suggested work without sending, and expands for reading', async ({ page }) => {
  const api = await useAgentApi(page)
  await page.goto('/dashboard?workspace=workspace-a')
  await page.getByRole('button', { name: 'Agent', exact: true }).click()

  const input = page.getByLabel('给全局 Agent 的消息')
  const suggestion = page.getByRole('button', { name: '查看当前项目的工作流', exact: true })
  await expect(suggestion).toBeVisible()
  await suggestion.click()
  await expect(input).toHaveValue('查看当前项目的工作流')
  await expect(input).toBeFocused()
  expect(api.sentMessages()).toBe(0)

  await page.getByRole('button', { name: '发送', exact: true }).click()
  await expect(page.getByText('Agent service unavailable')).toBeVisible()
  await expect(input).toHaveValue('查看当前项目的工作流')
  expect(api.sentMessages()).toBe(1)

  const dialog = page.getByRole('dialog', { name: '全局 Agent' })
  await page.getByRole('button', { name: '展开 Agent 面板', exact: true }).click()
  await expect(page.getByRole('button', { name: '收起 Agent 面板', exact: true })).toBeVisible()
  await expect(dialog).toHaveClass(/lg:w-\[min\(720px/)
})

test('Agent dock disables composition while the session list is loading', async ({ page }) => {
  const api = await useAgentApi(page, { holdSessions: true })
  await page.goto('/dashboard?workspace=workspace-a')
  await page.getByRole('button', { name: 'Agent', exact: true }).click()

  const input = page.getByLabel('给全局 Agent 的消息')
  await expect(input).toBeDisabled()
  await expect(page.getByText('正在加载会话，暂不能发送。')).toBeVisible()

  api.releaseSessions()
  await expect(input).toBeEnabled()
})
