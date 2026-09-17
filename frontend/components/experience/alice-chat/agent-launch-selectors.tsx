'use client'

import Link from 'next/link'
import { useState } from 'react'
import { ChevronDown, Cpu, KeyRound, Settings } from 'lucide-react'

import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuRadioGroup, DropdownMenuRadioItem, DropdownMenuSeparator, DropdownMenuTrigger } from '@/components/ui/dropdown-menu'
import type { AgentChatOptions, AgentExecutionConfig } from '@/lib/api/agent-conversations'

export function AgentLaunchSelectors({ options, execution, disabled, onChange }: {
  options: AgentChatOptions
  execution: AgentExecutionConfig
  disabled: boolean
  onChange: (execution: AgentExecutionConfig) => void
}) {
  const provider = options.providers.find((item) => item.id === execution.provider_id)
  const selectedModel = provider?.models.find((model) => model.id === execution.model_id)
  const [accessOpen, setAccessOpen] = useState(false)
  const [modelOpen, setModelOpen] = useState(false)

  return <>
    <DropdownMenu open={accessOpen} onOpenChange={setAccessOpen}>
      <DropdownMenuTrigger className="alice-picker max-w-[190px]" aria-label="AI access：模型连接" disabled={disabled}>
        <KeyRound size={14} aria-hidden /><span className="truncate">{provider?.name || (execution.provider_id ? '已保存的连接' : '工作区默认连接')}</span><ChevronDown size={12} aria-hidden />
      </DropdownMenuTrigger>
      <DropdownMenuContent side="top" align="start" className="w-64">
        <p className="px-2 py-2 text-xs text-muted-foreground">仅设置当前新会话，不修改系统默认连接或泄露凭据。</p>
        <DropdownMenuRadioGroup value={execution.provider_id || ''} onValueChange={(providerId) => onChange({ ...execution, provider_id: providerId || null, model_id: null })}>
          <DropdownMenuRadioItem value="" onClick={() => setAccessOpen(false)}>工作区默认连接</DropdownMenuRadioItem>
          {options.providers.map((item) => <DropdownMenuRadioItem key={item.id} value={item.id} onClick={() => setAccessOpen(false)}>{item.name}</DropdownMenuRadioItem>)}
        </DropdownMenuRadioGroup>
        {options.provider_selection_allowed !== false ? <>
          <DropdownMenuSeparator />
          <DropdownMenuItem render={<Link href="/providers" />}><Settings size={14} aria-hidden />管理模型与连接</DropdownMenuItem>
        </> : <p className="px-2 py-2 text-xs text-muted-foreground">当前角色使用默认连接；新增连接请联系工作区管理员。</p>}
      </DropdownMenuContent>
    </DropdownMenu>
    <DropdownMenu open={modelOpen} onOpenChange={setModelOpen}>
      <DropdownMenuTrigger className="alice-picker max-w-[240px]" aria-label="模型与推理设置" disabled={disabled}>
        <Cpu size={14} aria-hidden /><span className="truncate">{selectedModel?.name || execution.model_id || '默认模型'}</span><ChevronDown size={12} aria-hidden />
      </DropdownMenuTrigger>
      <DropdownMenuContent side="top" align="start" className="max-h-80 w-72">
        <p className="px-2 py-2 text-xs text-muted-foreground">模型</p>
        <DropdownMenuRadioGroup value={execution.model_id || ''} onValueChange={(modelId) => onChange({ ...execution, model_id: modelId || null })}>
          <DropdownMenuRadioItem value="" onClick={() => setModelOpen(false)}>默认模型{provider?.default_model ? ` · ${provider.default_model}` : ''}</DropdownMenuRadioItem>
          {provider?.models.map((model) => <DropdownMenuRadioItem key={model.id} value={model.id} onClick={() => setModelOpen(false)}>{model.name}</DropdownMenuRadioItem>)}
        </DropdownMenuRadioGroup>
        {!provider ? <p className="px-2 py-2 text-xs text-muted-foreground">选择模型连接后，显示该连接的可用模型。</p> : null}
        <DropdownMenuSeparator />
        <div className="px-2 py-2 text-xs text-muted-foreground">推理强度：当前聊天执行器使用模型默认值，尚不支持单独覆盖。</div>
      </DropdownMenuContent>
    </DropdownMenu>
  </>
}
