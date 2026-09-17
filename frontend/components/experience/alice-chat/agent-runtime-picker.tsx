'use client'

import { useState } from 'react'
import { Check, ChevronDown, Copy, ExternalLink, Search } from 'lucide-react'

import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuSeparator, DropdownMenuTrigger } from '@/components/ui/dropdown-menu'
import type { AgentChatRuntime } from '@/lib/api/agent-conversations'
import { AGENT_INSTALL } from './agent-install'
import { AgentRuntimeIcon } from './agent-runtime-icon'

const DEFAULT_PRIMARY_IDS = ['pi', 'codex', 'claude', 'grok']

function InstallationGuidance({ runtime }: { runtime: AgentChatRuntime }) {
  const hint = AGENT_INSTALL[runtime.id]
  const [copyState, setCopyState] = useState<'idle' | 'copied' | 'failed'>('idle')

  async function copyCommand() {
    if (!hint) return
    try {
      await navigator.clipboard.writeText(hint.command)
      setCopyState('copied')
    } catch {
      setCopyState('failed')
    }
  }

  return <div className="flex w-full min-w-0 items-start gap-2 rounded-md px-2.5 py-2 text-[13px] leading-[18px] text-muted-foreground">
    <AgentRuntimeIcon runtimeId={runtime.id} className="mt-0.5 size-4 shrink-0" />
    <div className="min-w-0 flex-1">
      <div className="flex min-w-0 items-center gap-2"><span className="min-w-0 truncate font-medium">{runtime.name}</span><span className="shrink-0 text-[10px]">未安装</span></div>
      {hint ? <>
        <p className="mt-0.5 select-text truncate font-mono text-[11px] leading-[15px]" title={hint.command}>{hint.command}</p>
        <div className="mt-1.5 flex flex-wrap gap-1.5">
          <Button type="button" variant="outline" size="xs" onClick={() => void copyCommand()} aria-label={'复制 ' + runtime.name + ' 安装命令'}><Copy className="size-3" aria-hidden />{copyState === 'copied' ? '已复制' : '复制 ' + runtime.name + ' 安装命令'}</Button>
          <a href={hint.url} target="_blank" rel="noreferrer" aria-label={'打开 ' + runtime.name + ' 安装文档'} className="inline-flex h-6 items-center gap-1 rounded-md border border-border bg-background px-2 text-xs text-foreground hover:bg-muted"><ExternalLink className="size-3" aria-hidden />安装文档</a>
        </div>
        {copyState === 'failed' ? <p role="status" className="mt-1 text-xs">无法复制，请手动选择上方命令。</p> : null}
      </> : null}
    </div>
  </div>
}

export function AgentRuntimePicker({ runtimes, selectedId, disabled, onSelect }: {
  runtimes: AgentChatRuntime[]
  selectedId: string
  disabled: boolean
  onSelect: (runtimeId: string) => void
}) {
  const [menuOpen, setMenuOpen] = useState(false)
  const [catalogOpen, setCatalogOpen] = useState(false)
  const [query, setQuery] = useState('')
  const selected = runtimes.find((runtime) => runtime.id === selectedId)
  const installedNative = runtimes.filter((runtime) => runtime.id !== 'opencli' && runtime.installed === true)
  const primaryIds = [...new Set([...DEFAULT_PRIMARY_IDS, ...installedNative.map((runtime) => runtime.id)])]
    .filter((runtimeId) => installedNative.some((runtime) => runtime.id === runtimeId)).slice(0, 4)
  const primary = primaryIds.flatMap((runtimeId) => installedNative.filter((runtime) => runtime.id === runtimeId))
  const currentOutsidePrimary = selected && !primaryIds.includes(selected.id)
  const ordered = [...runtimes.filter((runtime) => runtime.id !== 'opencli'), ...runtimes.filter((runtime) => runtime.id === 'opencli')]
  const matches = ordered.filter((runtime) => (runtime.id + ' ' + runtime.name).toLowerCase().includes(query.trim().toLowerCase()))
  const installed = matches.filter((runtime) => runtime.installed === true)
  const missing = matches.filter((runtime) => runtime.installed === false)
  const unknown = matches.filter((runtime) => runtime.installed === null)

  function choose(runtime: AgentChatRuntime) {
    if (disabled || runtime.installed !== true) return
    onSelect(runtime.id)
    setMenuOpen(false)
    setCatalogOpen(false)
    setQuery('')
  }

  function menuItem(runtime: AgentChatRuntime) {
    return <DropdownMenuItem key={runtime.id} disabled={disabled || runtime.installed !== true} onClick={() => choose(runtime)} className={'min-h-9 gap-2 px-2.5 text-[12px] ' + (runtime.id === selectedId ? 'bg-muted text-foreground' : 'text-foreground')}>
      <AgentRuntimeIcon runtimeId={runtime.id} className="size-3.5 shrink-0" />
      <span className="min-w-0 flex-1 truncate">{runtime.name}</span>
      {runtime.id === selectedId ? <Check size={14} aria-hidden /> : null}
    </DropdownMenuItem>
  }

  return <>
    <DropdownMenu open={menuOpen} onOpenChange={setMenuOpen}>
      <DropdownMenuTrigger className="alice-picker max-w-[190px]" aria-label="选择 Agent runtime" disabled={!runtimes.length}>
        <AgentRuntimeIcon runtimeId={selectedId} className="size-3.5 shrink-0" /><span className="truncate">{selected?.name || '选择 Agent'}</span><ChevronDown size={12} aria-hidden />
      </DropdownMenuTrigger>
      <DropdownMenuContent side="top" align="start" sideOffset={6} className="alice-chat w-[min(16rem,calc(100vw-2rem))] rounded-xl border border-border/70 bg-secondary p-1 shadow-lg ring-0">
        {primary.map(menuItem)}
        {currentOutsidePrimary ? <>
          {primary.length ? <DropdownMenuSeparator /> : null}
          <p className="px-2.5 py-1 text-[11px] font-medium text-muted-foreground">当前运行时</p>
          {menuItem(selected)}
        </> : null}
        <DropdownMenuSeparator />
        <DropdownMenuItem onClick={() => { setMenuOpen(false); setCatalogOpen(true) }} className="min-h-9 px-2.5 text-[12px]">其他 · All agent runtimes</DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
    <Dialog open={catalogOpen} onOpenChange={(open) => { setCatalogOpen(open); if (!open) setQuery('') }}>
      <DialogContent className="alice-chat alice-runtime-dialog flex max-h-[min(40rem,calc(100dvh-2rem))] w-full max-w-[calc(100%-2rem)] flex-col gap-3 overflow-hidden rounded-2xl bg-popover shadow-lg sm:max-w-lg" overlayClassName="bg-black/60 supports-backdrop-filter:backdrop-blur-sm">
        <DialogHeader>
          <DialogTitle className="text-base leading-6">全部 Agent 运行时</DialogTitle>
          <DialogDescription className="text-[13px] leading-5">已安装的运行时可选择；未安装项保留安装指引。能否执行由当前会话的接入状态决定。</DialogDescription>
        </DialogHeader>
        <label className="relative block">
          <Search size={14} aria-hidden className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-muted-foreground" />
          <input autoFocus aria-label="搜索运行时" placeholder="搜索运行时…" value={query} onChange={(event) => setQuery(event.target.value)} className="h-8 w-full rounded-md border bg-background pl-8 pr-3 text-[13px] outline-none focus-visible:ring-2 focus-visible:ring-ring" />
        </label>
        <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain pr-0.5">
          {!matches.length ? <p className="px-1 py-6 text-center text-xs text-muted-foreground">没有匹配的运行时</p> : null}
          <div className="flex flex-col gap-4 pb-1">
            {installed.length ? <section aria-label="已安装运行时">
              <h3 className="px-2.5 pb-1 text-[11px] font-medium leading-[15px] text-muted-foreground">已安装</h3>
              {installed.map((runtime) => <button type="button" key={runtime.id} onClick={() => choose(runtime)} disabled={disabled} className={'flex min-h-11 w-full min-w-0 items-start gap-2 rounded-md px-2.5 py-2 text-left text-[13px] leading-[18px] transition-colors hover:bg-muted disabled:opacity-50 ' + (runtime.id === selectedId ? 'bg-muted/50 text-foreground' : 'text-foreground')}>
                <AgentRuntimeIcon runtimeId={runtime.id} className="mt-0.5 size-4 shrink-0" /><span className="min-w-0 flex-1 truncate font-medium">{runtime.name}</span>{runtime.id === selectedId ? <Check size={14} className="mt-0.5" aria-hidden /> : null}
              </button>)}
            </section> : null}
            {missing.length ? <section aria-label="未安装运行时">
              <h3 className="px-2.5 pb-1 text-[11px] font-medium leading-[15px] text-muted-foreground">未安装</h3>
              {missing.map((runtime) => <InstallationGuidance key={runtime.id} runtime={runtime} />)}
            </section> : null}
            {unknown.length ? <section aria-label="安装状态未确认">
              <h3 className="px-2.5 pb-1 text-[11px] font-medium leading-[15px] text-muted-foreground">安装状态未确认</h3>
              {unknown.map((runtime) => <div key={runtime.id} className="flex items-start gap-2 px-2.5 py-2 text-[13px] leading-[18px] text-muted-foreground"><AgentRuntimeIcon runtimeId={runtime.id} className="mt-0.5 size-4 shrink-0" /><div><span className="font-medium">{runtime.name}</span><p className="mt-0.5 text-xs">{runtime.reason || '请管理员检查运行时安装状态。'}</p></div></div>)}
            </section> : null}
          </div>
        </div>
        {disabled ? <p className="text-xs text-muted-foreground">当前会话的执行配置已固定；新建对话后可重新选择。</p> : null}
        <div className="flex justify-end"><Button variant="outline" onClick={() => setCatalogOpen(false)}>关闭</Button></div>
      </DialogContent>
    </Dialog>
  </>
}
