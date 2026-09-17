'use client'

import { useEffect, useLayoutEffect, useRef, useState, type FormEvent, type KeyboardEvent, type ReactNode, type RefObject } from 'react'
import { ArrowUp, Bot, CalendarClock, ChevronDown, Database, FileSearch, History, Inbox, LayoutGrid, Loader2, MessageSquare, MoreHorizontal, Plus, RefreshCw, Search, Square, SquarePen, SquareTerminal } from 'lucide-react'

import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuRadioGroup, DropdownMenuRadioItem, DropdownMenuTrigger } from '@/components/ui/dropdown-menu'
import type { AgentConversation } from '@/lib/api/agent-conversations'
import { ComposerShell } from './composer-shell'
import './chat-surface.css'

const EXAMPLE_GROUPS = [
  [
    { title: '收集网站中的产品信息', prompt: '帮我收集这些网站的产品信息，保留名称、价格、发布日期和原文链接。先检查可用来源与能力，再告诉我下一步需要提供什么。', icon: FileSearch },
    { title: '看看最近的数据有什么变化', prompt: '查看当前工作区最近的数据，按来源总结新增和变化，注明数据所属运行和时间；没有数据就明确告诉我。', icon: Database },
    { title: '检查哪些工作需要我处理', prompt: '检查当前工作区需要我处理的事项，包括失败运行、登录失效和待确认操作。先列出实际发现的问题，不自动重试或修改配置。', icon: Inbox },
  ],
  [
    { title: '让一项工作每天自动更新', prompt: '我想让已有工作每天更新。先帮我找出当前工作区可用的工作，确认频率、时区和交付目标后再提出修改。', icon: CalendarClock },
    { title: '检查已有的数据来源', prompt: '查看当前工作区已配置的数据来源及连接状态，说明哪些可以使用、哪些需要我补充信息。', icon: Search },
    { title: '调整已有工作的采集要求', prompt: '帮我调整已有工作的采集要求。先确认目标工作和当前规则，再展示需要我确认的变更。', icon: SquarePen },
  ],
]

type ChatSurfaceProps = {
  workspaces: Array<{ id: string; name: string }>
  workspaceId: string | null
  sessions: AgentConversation[]
  sessionId: string | null
  sessionsLoading: boolean
  busy: boolean
  canClose: boolean
  input: string
  inputDisabled: boolean
  inputRef: RefObject<HTMLTextAreaElement | null>
  blockedReason: string | null
  error: string | null
  hasConversation: boolean
  conversation: ReactNode
  runtimeSelector: ReactNode
  launchControls: ReactNode
  launchStatus: ReactNode
  mode: 'gui' | 'terminal'
  availableModes: Array<'gui' | 'terminal'>
  conversationRevision: number
  canStop: boolean
  stopping: boolean
  onStop: () => void
  onMode: (mode: 'gui' | 'terminal') => void
  onInput: (value: string) => void
  onKeyDown: (event: KeyboardEvent<HTMLTextAreaElement>) => void
  onSubmit: (event?: FormEvent) => void
  onSuggestion: (value: string) => void
  onWorkspace: (id: string) => void
  onSession: (id: string) => void
  onNew: () => void
  onClose: () => void
}

export function AliceChatSurface(props: ChatSurfaceProps) {
  const [examplePage, setExamplePage] = useState(0)
  const scrollRef = useRef<HTMLDivElement>(null)
  const selectedSession = props.sessions.find((session) => session.id === props.sessionId)
  const selectedWorkspace = props.workspaces.find((workspace) => workspace.id === props.workspaceId)
  const showStarterIntents = props.input.trim().length === 0 && !props.busy
  const examples = EXAMPLE_GROUPS[examplePage]
  const terminalActive = props.mode === 'terminal' && props.hasConversation

  useEffect(() => {
    if (!props.inputDisabled) props.inputRef.current?.focus()
  }, [props.inputDisabled, props.inputRef, props.sessionId])

  useEffect(() => {
    if (props.hasConversation) scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight })
  }, [props.conversationRevision, props.hasConversation])

  useLayoutEffect(() => {
    const textarea = props.inputRef.current
    if (!textarea) return
    textarea.style.setProperty('height', 'auto')
    textarea.style.setProperty('height', `${Math.min(168, Math.max(68, textarea.scrollHeight))}px`)
    textarea.style.setProperty('overflow-y', textarea.scrollHeight > 168 ? 'auto' : 'hidden')
  }, [props.input, props.inputRef])

  return (
    <div className="alice-chat flex h-full min-h-0 w-full overflow-hidden bg-background text-foreground" data-testid="alice-chat">
      <section className="@container/harness flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden" aria-label="工作对话">
        <header className="flex h-10 shrink-0 items-center justify-between border-b border-border px-3">
          <h2 className="truncate text-[13px] font-semibold">{selectedSession?.title || '新对话'}</h2>
          <div className="flex shrink-0 items-center gap-1">
            <DropdownMenu>
              <DropdownMenuTrigger className="alice-icon" aria-label="会话历史" disabled={props.busy || props.sessionsLoading}><History size={16} /></DropdownMenuTrigger>
              <DropdownMenuContent align="end" className="max-h-80 w-64 overflow-y-auto">
                {props.sessions.length ? props.sessions.map((session) => <DropdownMenuItem key={session.id} onClick={() => props.onSession(session.id)}><span className="truncate">{session.title || '新对话'}</span>{session.status === 'closed' ? <span className="ml-auto shrink-0 text-xs">已关闭</span> : null}</DropdownMenuItem>) : <DropdownMenuItem disabled>暂无会话</DropdownMenuItem>}
              </DropdownMenuContent>
            </DropdownMenu>
            <button type="button" className="alice-icon" aria-label="新建对话" onClick={props.onNew} disabled={props.busy || props.sessionsLoading}><Plus size={16} /></button>
            {props.sessionId ? <DropdownMenu><DropdownMenuTrigger className="alice-icon" aria-label="会话操作"><MoreHorizontal size={16} /></DropdownMenuTrigger><DropdownMenuContent align="end"><DropdownMenuItem onClick={props.onClose} disabled={!props.canClose}>关闭当前会话</DropdownMenuItem></DropdownMenuContent></DropdownMenu> : null}
          </div>
        </header>
        <div ref={scrollRef} data-testid="harness-landing-scroll" className={`flex min-h-0 flex-1 justify-start overflow-x-hidden overscroll-contain ${terminalActive ? 'overflow-hidden p-3' : 'overflow-y-auto px-5 py-8 @min-[42rem]/harness:px-8 @min-[42rem]/harness:py-10'}`}>
          {props.hasConversation ? <div className={terminalActive ? 'h-full min-h-0 w-full' : 'mx-auto w-full max-w-[46rem] space-y-4'} aria-live="polite">{props.conversation}</div> : <div data-testid="harness-landing-stack" className="mx-auto my-auto w-full max-w-[42rem]">
            <header className="flex flex-col items-center text-center">
              <span className="grid h-11 w-11 select-none place-items-center" aria-hidden><Bot size={40} strokeWidth={1.3} /></span>
              <h1 className="mt-3 max-w-[38rem] text-balance text-[24px] font-semibold leading-[30px] tracking-[-0.018em] @min-[42rem]/harness:text-[28px] @min-[42rem]/harness:leading-[34px]">你想让 OpenCLI 做什么？</h1>
            </header>
            <div data-testid="harness-landing-suggestions" data-state={showStarterIntents ? 'visible' : 'hidden'} className={showStarterIntents ? 'mt-7' : 'hidden'} inert={!showStarterIntents}>
              <div className="flex h-7 items-center justify-between px-1"><span className="text-[12px] font-medium text-muted-foreground">试试这些工作</span><button type="button" onClick={() => setExamplePage((current) => (current + 1) % EXAMPLE_GROUPS.length)} className="alice-icon" aria-label="换一组建议"><RefreshCw size={14} aria-hidden /></button></div>
              <div role="group" aria-label="建议工作">{examples.map(({ title, prompt, icon: Icon }) => <button key={title} type="button" onClick={() => props.onSuggestion(prompt)} disabled={props.busy} className="group flex min-h-11 w-full items-center gap-3 border-b border-border/70 px-1 text-left outline-none transition-colors hover:text-foreground focus-visible:ring-1 focus-visible:ring-ring disabled:opacity-40"><Icon size={17} aria-hidden className="shrink-0 text-muted-foreground" /><span className="min-w-0 flex-1 text-[14px] font-medium leading-5 text-muted-foreground group-hover:text-foreground">{title}</span></button>)}</div>
            </div>
          </div>}
        </div>
        {terminalActive ? <div className="flex shrink-0 items-center gap-3 border-t px-4 py-2 text-xs text-muted-foreground">
          <SquareTerminal size={14} aria-hidden />
          <span className="min-w-0 flex-1">终端输入直接发送到当前 PTY；断开页面不会停止进程。</span>
          {props.canStop ? <button type="button" onClick={props.onStop} disabled={props.stopping} className="inline-flex min-h-8 items-center gap-2 rounded-md border px-3 text-foreground disabled:opacity-50">{props.stopping ? <Loader2 className="size-3.5 animate-spin" aria-hidden /> : <Square size={12} fill="currentColor" aria-hidden />}{props.stopping ? '正在停止 / 未确认' : '停止终端'}</button> : null}
          {props.error ? <span role="alert" className="text-destructive">{props.error}</span> : null}
        </div> : <div className="shrink-0 px-3 pb-3 @min-[42rem]/harness:px-6 @min-[42rem]/harness:pb-5">
          <form className="mx-auto w-full max-w-[46rem]" onSubmit={props.onSubmit}>
            <ComposerShell context={<>
              <DropdownMenu><DropdownMenuTrigger className="alice-picker max-w-[17rem]" disabled={props.busy || !props.workspaces.length} aria-label={`工作区：${selectedWorkspace?.name || '选择工作区'}`}><MessageSquare size={14} aria-hidden /><span className="truncate">{selectedWorkspace?.name || '选择工作区'}</span><ChevronDown size={12} aria-hidden /></DropdownMenuTrigger><DropdownMenuContent side="top" className="min-w-52"><DropdownMenuRadioGroup value={props.workspaceId || ''} onValueChange={props.onWorkspace}>{props.workspaces.map((workspace) => <DropdownMenuRadioItem value={workspace.id} key={workspace.id}>{workspace.name}</DropdownMenuRadioItem>)}</DropdownMenuRadioGroup></DropdownMenuContent></DropdownMenu>
              {props.runtimeSelector}
              <DropdownMenu><DropdownMenuTrigger className="alice-picker" disabled={Boolean(props.sessionId) || props.busy} aria-label={`界面模式：${props.mode === 'terminal' ? 'TUI' : 'GUI'}`}>{props.mode === 'terminal' ? <SquareTerminal size={14} aria-hidden /> : <LayoutGrid size={14} aria-hidden />}<span>{props.mode === 'terminal' ? 'TUI' : 'GUI'}</span><ChevronDown size={12} aria-hidden /></DropdownMenuTrigger><DropdownMenuContent side="top"><DropdownMenuRadioGroup value={props.mode} onValueChange={(value) => props.onMode(value as 'gui' | 'terminal')}><DropdownMenuRadioItem value="gui">GUI</DropdownMenuRadioItem><DropdownMenuRadioItem value="terminal" disabled={!props.availableModes.includes('terminal')}>TUI{props.availableModes.includes('terminal') ? '' : ' · 当前运行时不可用'}</DropdownMenuRadioItem></DropdownMenuRadioGroup></DropdownMenuContent></DropdownMenu>
            </>} controls={props.launchControls} action={props.canStop ? <button type="button" onClick={props.onStop} disabled={props.stopping} aria-label={props.stopping ? '正在停止回复' : '停止回复'} title="请求停止当前回复，已完成的操作不会撤销" className="grid size-8 shrink-0 place-items-center rounded-full bg-foreground text-background disabled:opacity-50">{props.stopping ? <Loader2 className="size-4 animate-spin" aria-hidden /> : <Square size={14} fill="currentColor" aria-hidden />}</button> : <button type="submit" disabled={!props.input.trim() || Boolean(props.blockedReason)} aria-label="发送" aria-busy={props.busy} title={props.blockedReason || '发送'} className="grid size-8 shrink-0 place-items-center rounded-full bg-foreground text-background transition-colors hover:opacity-90 disabled:cursor-not-allowed disabled:bg-muted disabled:text-muted-foreground/55">{props.busy ? <Loader2 className="size-4 animate-spin" aria-hidden /> : <ArrowUp size={16} aria-hidden />}</button>}>
              <textarea ref={props.inputRef} value={props.input} onChange={(event) => props.onInput(event.target.value)} onKeyDown={props.onKeyDown} disabled={props.inputDisabled} placeholder="描述任务、问题，或需要做出的决定…" aria-label="给全局 Agent 的消息" rows={1} className="block min-h-[68px] max-h-[168px] w-full resize-none bg-transparent px-1.5 py-1.5 text-[14px] leading-[21px] text-foreground outline-none placeholder:text-muted-foreground/70" />
            </ComposerShell>
            {props.launchStatus}
            {props.blockedReason ? <p role="status" className="mt-2 px-3 text-xs text-muted-foreground">{props.blockedReason}</p> : null}
            {props.error ? <p role="alert" className="mt-2 rounded-lg border border-destructive/30 px-3 py-2 text-xs text-destructive">{props.error}</p> : null}
          </form>
        </div>}
      </section>
    </div>
  )
}
