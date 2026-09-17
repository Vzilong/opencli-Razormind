'use client'

import { FitAddon } from '@xterm/addon-fit'
import { WebLinksAddon } from '@xterm/addon-web-links'
import { Terminal as Xterm } from '@xterm/xterm'
import { useEffect, useRef, useState } from 'react'
import { Loader2, RotateCcw } from 'lucide-react'
import '@xterm/xterm/css/xterm.css'

import { createAgentTerminalTicket, type AgentTerminalSession } from '@/lib/api/agent-conversations'
import { attachWebglRenderer } from './renderer'
import { parseTerminalControl, type TerminalStatus } from './protocol'
import { resolveTerminalTheme } from './terminal-appearance'
import { installTerminalKeyboardController } from './terminal-keyboard-controller'
import { TerminalKittyKeyboardModeTracker } from './terminal-kitty-keyboard-mode-tracker'
import { TERMINAL_FONT_FAMILY } from './terminalInput'
import './terminal.css'

type NativeTerminalProps = {
  conversationId: string
  terminal: AgentTerminalSession
  onStatus?: (terminal: Pick<AgentTerminalSession, 'status' | 'exit_code' | 'cleanup_confirmed'>) => void
}

const RECONNECT_BASE_MS = 500
const RECONNECT_MAX_MS = 10_000
const RECONNECT_MAX_ATTEMPTS = 12
const TERMINAL_INPUT_CHUNK_SIZE = 64 * 1024
const CANONICAL_UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i

function terminalControllerId(terminalId: string): string {
  const key = `opencli:terminal-controller:${terminalId}`
  try {
    const stored = window.sessionStorage.getItem(key)
    if (stored && CANONICAL_UUID.test(stored)) return stored
    const created = crypto.randomUUID()
    window.sessionStorage.setItem(key, created)
    return created
  } catch {
    return crypto.randomUUID()
  }
}

function terminalSocketUrl(ticket: string, controllerId: string): string {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  const configuredUrl = process.env.NEXT_PUBLIC_AGENT_TERMINAL_WS_URL?.trim()
  const useNativeDevBackend = process.env.NODE_ENV === 'development'
    && ['127.0.0.1', 'localhost'].includes(window.location.hostname)
    && window.location.port === '3010'
  const url = new URL(
    configuredUrl
      || `${protocol}//${window.location.hostname}${useNativeDevBackend ? ':8031' : window.location.port ? `:${window.location.port}` : ''}/api/v1/chat/terminal/ws`,
  )
  url.searchParams.set('ticket', ticket)
  url.searchParams.set('controller', controllerId)
  return url.toString()
}

function safeFit(fit: FitAddon): void {
  try {
    fit.fit()
  } catch {
    // The container can briefly have zero size during route transitions.
  }
}

export default function NativeTerminal({ conversationId, terminal, onStatus }: NativeTerminalProps) {
  const hostRef = useRef<HTMLDivElement>(null)
  const connectRef = useRef<(() => void) | null>(null)
  const takeoverRef = useRef<(() => void) | null>(null)
  const onStatusRef = useRef(onStatus)
  onStatusRef.current = onStatus
  const [status, setStatus] = useState<TerminalStatus>('connecting')
  const [replayTruncated, setReplayTruncated] = useState(false)
  const [exitCode, setExitCode] = useState<number | null>(terminal.exit_code)

  useEffect(() => {
    const host = hostRef.current
    if (!host) return

    const term = new Xterm({
      theme: resolveTerminalTheme(),
      fontFamily: TERMINAL_FONT_FAMILY,
      fontSize: 13,
      lineHeight: 1.2,
      cursorBlink: true,
      allowProposedApi: true,
      scrollback: 10_000,
      macOptionIsMeta: false,
      convertEol: false,
      vtExtensions: { kittyKeyboard: true },
      windowOptions: {
        getWinSizePixels: true,
        getCellSizePixels: true,
        getWinSizeChars: true,
      },
    })
    const fit = new FitAddon()
    term.loadAddon(fit)
    term.loadAddon(new WebLinksAddon())
    term.open(host)

    const kittyMode = new TerminalKittyKeyboardModeTracker()
    let decoder = new TextDecoder()
    let socket: WebSocket | null = null
    let reconnectTimer: ReturnType<typeof setTimeout> | undefined
    let initTimer: ReturnType<typeof setTimeout> | undefined
    let resizeObserver: ResizeObserver | null = null
    let webgl: ReturnType<typeof attachWebglRenderer> = null
    let attempts = 0
    let destroyed = false
    let replaying = true
    let replayGeneration = 0
    let replayBoundaryEnded = false
    let pendingReplayWrites = 0
    let writable = false
    let lastCols = term.cols
    let lastRows = term.rows
    const controllerId = terminalControllerId(terminal.id)
    const encoder = new TextEncoder()

    const setWritable = (value: boolean): void => {
      writable = value
    }
    const canWrite = (): boolean => writable && !replaying && socket?.readyState === WebSocket.OPEN
    const sendInput = (data: string): void => {
      if (!canWrite()) return
      const payload = encoder.encode(data)
      for (let offset = 0; offset < payload.byteLength; offset += TERMINAL_INPUT_CHUNK_SIZE) {
        socket?.send(payload.slice(offset, offset + TERMINAL_INPUT_CHUNK_SIZE))
      }
    }
    const keyboard = installTerminalKeyboardController({
      terminalElement: term.element,
      hasSelection: () => term.hasSelection(),
      isKittyKeyboardActive: () => kittyMode.flags > 0,
      sendInput,
      resetKittyProtocol: () => {
        kittyMode.reset()
        queueMicrotask(() => {
          if (!destroyed) term.write('\x1b[<99u\x1b[=0u')
        })
      },
    })
    term.attachCustomKeyEventHandler(keyboard.handle)

    const finishReplay = (): void => {
      if (!replaying || !replayBoundaryEnded || pendingReplayWrites > 0) return
      replaying = false
      if (writable) term.focus()
    }

    const write = (data: Uint8Array): void => {
      kittyMode.scan(decoder.decode(data, { stream: true }))
      const generation = replayGeneration
      if (replaying) pendingReplayWrites += 1
      term.write(data, () => {
        if (generation === replayGeneration && replaying) {
          pendingReplayWrites = Math.max(0, pendingReplayWrites - 1)
          finishReplay()
        }
      })
    }

    const sendResize = (force = false): void => {
      safeFit(fit)
      const cols = Math.max(1, Math.min(1000, term.cols))
      const rows = Math.max(1, Math.min(1000, term.rows))
      if (!force && cols === lastCols && rows === lastRows) return
      lastCols = cols
      lastRows = rows
      if (socket?.readyState === WebSocket.OPEN) {
        socket.send(JSON.stringify({ type: 'resize', cols, rows }))
      }
    }
    const handleWindowResize = (): void => sendResize()

    const scheduleReconnect = (): void => {
      if (destroyed) return
      if (attempts >= RECONNECT_MAX_ATTEMPTS) {
        setStatus('closed')
        return
      }
      attempts += 1
      setStatus('reconnecting')
      reconnectTimer = setTimeout(
        () => void connect(),
        Math.min(RECONNECT_BASE_MS * 2 ** (attempts - 1), RECONNECT_MAX_MS),
      )
    }

    const connect = async (): Promise<void> => {
      if (destroyed) return
      setStatus(attempts ? 'reconnecting' : 'connecting')
      setWritable(false)
      replaying = true
      replayGeneration += 1
      replayBoundaryEnded = false
      pendingReplayWrites = 0
      kittyMode.reset()
      decoder = new TextDecoder()
      try {
        socket?.close()
        const { ticket } = await createAgentTerminalTicket(conversationId)
        if (destroyed) return
        const nextSocket = new WebSocket(terminalSocketUrl(ticket, controllerId))
        nextSocket.binaryType = 'arraybuffer'
        socket = nextSocket
        nextSocket.addEventListener('open', () => sendResize(true))
        nextSocket.addEventListener('message', (event) => {
          if (destroyed || socket !== nextSocket) return
          if (typeof event.data !== 'string') {
            if (event.data instanceof ArrayBuffer) write(new Uint8Array(event.data))
            return
          }
          const control = parseTerminalControl(event.data)
          if (!control) return
          switch (control.type) {
            case 'attached':
            case 'locked':
              attempts = 0
              setWritable(control.controls && control.status === 'active')
              setStatus(control.status === 'exited' ? 'closed' : control.status === 'stopping' ? 'stopping' : control.controls ? 'connected' : 'locked')
              setReplayTruncated(control.replay_truncated)
              setExitCode(control.exit_code)
              break
            case 'snapshot_begin':
              replaying = true
              replayBoundaryEnded = false
              pendingReplayWrites = 0
              term.reset()
              break
            case 'snapshot_end':
              replayBoundaryEnded = true
              finishReplay()
              break
            case 'control_granted':
            case 'takeover':
              setWritable(control.controls)
              if (control.controls) {
                setStatus('connected')
                finishReplay()
              }
              break
            case 'kicked':
              setWritable(false)
              setStatus('kicked')
              break
            case 'exit':
              setWritable(false)
              setExitCode(control.exit_code)
              setStatus('closed')
              onStatusRef.current?.({ status: 'exited', exit_code: control.exit_code, cleanup_confirmed: control.cleanup_complete })
              break
            case 'transport':
              setWritable(false)
              setStatus('reconnecting')
              nextSocket.close(1012, 'Agent transport reconnecting')
              break
          }
        })
        nextSocket.addEventListener('close', (event) => {
          if (socket !== nextSocket) return
          socket = null
          setWritable(false)
          if (destroyed) return
          if (event.code === 4403) setStatus('error')
          else if (event.code === 4404 || event.code === 4400) setStatus('closed')
          else scheduleReconnect()
        })
      } catch {
        scheduleReconnect()
      }
    }
    connectRef.current = () => {
      attempts = 0
      if (reconnectTimer) clearTimeout(reconnectTimer)
      void connect()
    }
    takeoverRef.current = () => {
      if (socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify({ type: 'takeover' }))
    }

    const dataSubscription = term.onData(sendInput)
    const binarySubscription = term.onBinary((data) => {
      if (!canWrite()) return
      const bytes = new Uint8Array(data.length)
      for (let index = 0; index < data.length; index += 1) bytes[index] = data.charCodeAt(index) & 0xff
      socket?.send(bytes)
    })

    let initAttempts = 0
    const initialize = (): void => {
      if (destroyed) return
      if ((host.clientWidth < 50 || host.clientHeight < 30) && initAttempts < 40) {
        initAttempts += 1
        initTimer = setTimeout(initialize, 25)
        return
      }
      webgl = attachWebglRenderer(term)
      safeFit(fit)
      lastCols = Math.max(1, Math.min(1000, term.cols))
      lastRows = Math.max(1, Math.min(1000, term.rows))
      resizeObserver = new ResizeObserver(() => sendResize())
      resizeObserver.observe(host)
      window.addEventListener('resize', handleWindowResize)
      void connect()
    }
    initTimer = setTimeout(initialize, 0)

    return () => {
      destroyed = true
      connectRef.current = null
      takeoverRef.current = null
      if (reconnectTimer) clearTimeout(reconnectTimer)
      if (initTimer) clearTimeout(initTimer)
      resizeObserver?.disconnect()
      window.removeEventListener('resize', handleWindowResize)
      dataSubscription.dispose()
      binarySubscription.dispose()
      keyboard.dispose()
      socket?.close()
      webgl?.dispose()
      term.dispose()
    }
  }, [conversationId, terminal.id])

  return (
    <section className="opencli-native-terminal flex h-full min-h-[22rem] w-full flex-col overflow-hidden rounded-md border bg-background" data-testid="native-terminal">
      <header className="flex min-h-10 shrink-0 items-center gap-2 border-b px-3 text-xs">
        <span className={`size-2 rounded-full ${status === 'connected' ? 'bg-success' : status === 'error' ? 'bg-destructive' : 'bg-warning'}`} aria-hidden />
        <span className="font-medium">{terminal.runtime_id === 'codex' ? 'Codex' : 'OMP'} TUI</span>
        <span className="text-muted-foreground" role="status">{status === 'connected' ? '已连接' : status === 'locked' ? '只读 · 其他窗口正在控制' : status === 'kicked' ? '控制权已被其他窗口接管' : status === 'stopping' ? '正在停止 / 未确认' : status === 'reconnecting' ? '正在重连' : status === 'connecting' ? '正在连接' : status === 'error' ? '无权附着' : `已退出${exitCode === null ? '' : ` · ${exitCode}`}`}</span>
        {replayTruncated ? <span className="ml-auto text-warning">回放已截断</span> : null}
        {(status === 'locked' || status === 'kicked') ? <button type="button" className="ml-auto inline-flex min-h-8 items-center gap-1 rounded-md border px-2" onClick={() => takeoverRef.current?.()}><RotateCcw size={13} aria-hidden />接管</button> : null}
        {status === 'closed' && exitCode === null ? <button type="button" className="ml-auto inline-flex min-h-8 items-center gap-1 rounded-md border px-2" onClick={() => connectRef.current?.()}><RotateCcw size={13} aria-hidden />重试</button> : null}
        {(status === 'connecting' || status === 'reconnecting') ? <Loader2 className="ml-auto size-3.5 animate-spin" aria-hidden /> : null}
      </header>
      <div className="opencli-native-terminal-body min-h-0 flex-1">
        <div ref={hostRef} className="opencli-native-terminal-host" />
      </div>
    </section>
  )
}
