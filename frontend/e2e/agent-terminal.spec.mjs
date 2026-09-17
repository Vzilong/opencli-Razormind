import { expect, test } from '@playwright/test'

const workspace = {
  id: 'workspace-a',
  name: 'Terminal workspace',
  slug: 'terminal-workspace',
  active: true,
  created_at: '2026-09-17T00:00:00Z',
  updated_at: '2026-09-17T00:00:00Z',
}

const terminal = {
  id: 'terminal-a',
  conversation_id: 'conversation-a',
  workspace_id: workspace.id,
  runtime_id: 'codex',
  status: 'active',
  exit_code: null,
  cleanup_confirmed: false,
  revision: 1,
  created_at: '2026-09-17T00:00:00Z',
  updated_at: '2026-09-17T00:00:00Z',
}

test('native TUI starts, restores replay, requires takeover, and stops', async ({ page }) => {
  let conversation = null
  let terminalStart = null
  let stopCount = 0
  let ticketCount = 0

  await page.addInitScript(() => {
    sessionStorage.setItem('opencli.bootstrapIdentityToken', 'terminal-test-token')
    sessionStorage.setItem('opencli:terminal-controller:terminal-a', 'corrupt-controller')
    window.__terminalTestFrames = []
    window.__terminalTestUrls = []
    const BrowserWebSocket = window.WebSocket

    class TestWebSocket extends EventTarget {
      static CONNECTING = 0
      static OPEN = 1
      static CLOSING = 2
      static CLOSED = 3

      constructor(url) {
        super()
        this.url = url
        window.__terminalTestUrls.push(String(url))
        window.__terminalTestTransport = () => this.emit({ type: 'transport', status: 'reconnecting', recoverable: true })
        this.readyState = TestWebSocket.CONNECTING
        this.binaryType = 'blob'
        queueMicrotask(() => {
          this.readyState = TestWebSocket.OPEN
          this.dispatchEvent(new Event('open'))
          const locked = localStorage.getItem('opencli:e2e-terminal-mode') === 'locked'
          this.emit({
            type: locked ? 'locked' : 'attached',
            controls: !locked,
            status: 'active',
            exit_code: null,
            cleanup_complete: false,
            replay_truncated: false,
          })
          this.emit({ type: 'snapshot_begin', sequence: 1 })
          this.dispatchEvent(new MessageEvent('message', {
            data: new TextEncoder().encode('TUI-REPLAY-OK\r\n').buffer,
          }))
          this.emit({ type: 'snapshot_end', sequence: 1 })
        })
      }

      emit(message) {
        this.dispatchEvent(new MessageEvent('message', { data: JSON.stringify(message) }))
      }

      send(payload) {
        window.__terminalTestFrames.push(
          typeof payload === 'string' ? payload : `binary:${payload.byteLength}`,
        )
        if (typeof payload !== 'string') return
        const message = JSON.parse(payload)
        if (message.type === 'takeover') {
          this.emit({ type: 'takeover', controls: true })
        }
      }

      close(code = 1000, reason = '') {
        if (this.readyState === TestWebSocket.CLOSED) return
        this.readyState = TestWebSocket.CLOSED
        this.dispatchEvent(new CloseEvent('close', { code, reason, wasClean: true }))
      }
    }

    class RoutedWebSocket {
      static CONNECTING = 0
      static OPEN = 1
      static CLOSING = 2
      static CLOSED = 3

      constructor(url, protocols) {
        if (String(url).includes('/api/v1/chat/terminal/ws')) return new TestWebSocket(url)
        return protocols === undefined
          ? new BrowserWebSocket(url)
          : new BrowserWebSocket(url, protocols)
      }
    }

    window.WebSocket = RoutedWebSocket
  })

  await page.route('**/api/v1/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    const path = url.pathname
    const reply = (data, status = 200) => route.fulfill({
      status,
      contentType: 'application/json',
      body: JSON.stringify({ success: status < 400, data }),
    })

    if (path.endsWith('/auth/me')) return reply({ subject: 'terminal-admin', name: 'Terminal Admin', username: 'admin', is_platform_admin: true, auth_method: 'test' })
    if (path.endsWith('/governance/workspaces')) return reply([workspace])
    if (path.endsWith('/workspaces')) return reply([workspace])
    if (path.endsWith('/chat/options')) return reply({
      runtimes: [
        { id: 'opencli', name: 'OpenCLI', available: true, installed: true, reason: null, modes: ['gui'], access_modes: ['provider'] },
        { id: 'codex', name: 'Codex', available: true, installed: true, reason: null, modes: ['gui', 'terminal'], access_modes: ['native'] },
        { id: 'omp', name: 'Oh My Pi', available: true, installed: true, reason: null, modes: ['gui', 'terminal'], access_modes: ['native'] },
      ],
      providers: [],
      reasoning_efforts: [],
      provider_selection_allowed: false,
      default_route_ready: false,
    })
    if (path.endsWith('/chat/sessions') && request.method() === 'GET') return reply(conversation ? [conversation] : [])
    if (path.endsWith('/chat/sessions') && request.method() === 'POST') {
      const body = request.postDataJSON()
      conversation = {
        id: 'conversation-a',
        workspace_id: workspace.id,
        title: body.title,
        status: 'active',
        context_binding: body.context,
        execution: body.execution,
        revision: 0,
        created_at: '2026-09-17T00:00:00Z',
        updated_at: '2026-09-17T00:00:00Z',
      }
      return reply(conversation)
    }
    if (path.endsWith('/chat/sessions/conversation-a') && request.method() === 'GET') return reply({ ...conversation, turns: [] })
    if (path.endsWith('/chat/sessions/conversation-a/terminal') && request.method() === 'POST') {
      terminalStart = request.postDataJSON()
      return reply(terminal)
    }
    if (path.endsWith('/chat/sessions/conversation-a/terminal') && request.method() === 'GET') return reply(terminal)
    if (path.endsWith('/chat/sessions/conversation-a/terminal/ticket')) {
      ticketCount += 1
      return reply({ ticket: `signed-terminal-ticket-${ticketCount}`, expires_in: 60 })
    }
    if (path.endsWith('/chat/sessions/conversation-a/terminal/stop')) {
      stopCount += 1
      return reply({ ...terminal, status: 'exited', exit_code: -9, cleanup_confirmed: true, revision: 2 })
    }
    return reply([])
  })

  await page.goto('/launch?workspace=workspace-a')
  await page.getByRole('button', { name: '选择 Agent runtime' }).click()
  await page.getByText('Codex', { exact: true }).last().click()
  await page.getByRole('button', { name: '界面模式：GUI' }).click()
  await page.getByRole('menuitemradio', { name: 'TUI' }).click()
  await page.keyboard.press('Escape')
  await page.getByLabel('给全局 Agent 的消息').fill('只回复 TUI-E2E-OK')
  await page.getByRole('button', { name: '发送' }).click()

  await expect(page.getByTestId('native-terminal')).toBeVisible()
  await expect(page.getByRole('status')).toHaveText('已连接')
  const terminalGeometry = await page.getByTestId('native-terminal').evaluate((root) => {
    const body = root.querySelector('.opencli-native-terminal-body')
    const host = root.querySelector('.opencli-native-terminal-host')
    const xterm = root.querySelector('.xterm')
    if (!(body instanceof HTMLElement) || !(host instanceof HTMLElement) || !(xterm instanceof HTMLElement)) return null
    return {
      bodyPadding: getComputedStyle(body).padding,
      hostPadding: getComputedStyle(host).padding,
      xtermPadding: getComputedStyle(xterm).padding,
      hostWidth: host.getBoundingClientRect().width,
      xtermWidth: xterm.getBoundingClientRect().width,
    }
  })
  expect(terminalGeometry).not.toBeNull()
  expect(terminalGeometry.bodyPadding).toBe('8px 8px 4px')
  expect(terminalGeometry.hostPadding).toBe('0px')
  expect(terminalGeometry.xtermPadding).toBe('0px')
  expect(terminalGeometry.xtermWidth).toBeLessThanOrEqual(terminalGeometry.hostWidth + 0.5)
  expect(conversation.execution).toMatchObject({ runtime_id: 'codex', mode: 'terminal', access_mode: 'native' })
  expect(terminalStart).toEqual({ initial_input: '只回复 TUI-E2E-OK' })
  await expect.poll(() => page.evaluate(() => new URL(window.__terminalTestUrls[0]).searchParams.get('controller'))).toMatch(/^[0-9a-f-]{36}$/)

  await page.evaluate(() => window.__terminalTestTransport())
  await expect.poll(() => ticketCount).toBeGreaterThan(1)
  await expect(page.getByRole('status')).toHaveText('已连接')

  await page.evaluate(() => localStorage.setItem('opencli:e2e-terminal-mode', 'locked'))
  await page.reload()
  await expect(page.getByRole('status')).toHaveText('只读 · 其他窗口正在控制')
  await page.getByRole('button', { name: '接管' }).click()
  await expect(page.getByRole('status')).toHaveText('已连接')
  await expect.poll(() => page.evaluate(() => window.__terminalTestFrames.some((frame) => frame.includes('takeover')))).toBe(true)

  await page.getByRole('button', { name: '停止终端' }).click()
  await expect.poll(() => stopCount).toBe(1)
  await expect(page.getByRole('button', { name: '停止终端' })).toBeHidden()
})
