'use client'

import { Suspense, useEffect, useRef, useState } from 'react'
import { usePathname, useSearchParams } from 'next/navigation'

import { AppRouteTransition } from '@/components/motion/app-route-transition'
import { AppHeader } from '@/components/shell/app-header'
import { AppSidebar } from '@/components/shell/app-sidebar'
import { CommandPalette } from '@/components/shell/command-palette'
import { GlobalAgentDock } from '@/components/shell/global-agent-dock'
import { WorkTabs } from '@/components/shell/work-tabs'
import { SidebarInset, SidebarProvider } from '@/components/ui/sidebar'
import { useAuth } from '@/components/auth/auth-provider'

export function AppShell({ children }: { children: React.ReactNode }) {
  const { status } = useAuth()
  const pathname = usePathname()
  const [commandOpen, setCommandOpen] = useState(false)
  const [agentOpen, setAgentOpen] = useState(false)
  useEffect(() => {
    if (status !== 'authenticated') {
      setCommandOpen(false)
      setAgentOpen(false)
    }
  }, [status])
  return (
    <SidebarProvider>
      <AppSidebar />
      <SidebarInset className={pathname === '/launch' ? 'h-dvh min-w-0 overflow-hidden' : 'min-w-0'}>
        <AppHeader
          onOpenAgent={() => pathname === '/launch' ? document.querySelector<HTMLTextAreaElement>('[aria-label="给全局 Agent 的消息"]')?.focus() : setAgentOpen(true)}
          onOpenCommand={() => setCommandOpen(true)}
        />
        <Suspense fallback={null}><WorkTabs /></Suspense>
        <div className={pathname === '/launch' ? 'relative z-0 min-h-0 flex-1 overflow-hidden bg-background' : 'relative z-0 flex-1 overflow-auto overflow-x-clip bg-background [scrollbar-gutter:stable]'}>
          <AppRouteTransition>{children}</AppRouteTransition>
        </div>
      </SidebarInset>
      <CommandPalette open={commandOpen} onOpenChange={setCommandOpen} />
      <Suspense fallback={null}><AgentUrlIntent enabled={status === 'authenticated'} onOpen={() => setAgentOpen(true)} /></Suspense>
      <Suspense fallback={null}>
        {pathname !== '/launch' ? <GlobalAgentDock open={agentOpen} onOpenChange={setAgentOpen} /> : null}
      </Suspense>
    </SidebarProvider>
  )
}

function AgentUrlIntent({ enabled, onOpen }: { enabled: boolean; onOpen: () => void }) {
  const searchParams = useSearchParams()
  const openedIntent = useRef<string | null>(null)
  useEffect(() => {
    if (!enabled) {
      openedIntent.current = null
      return
    }
    const intent = searchParams.get('agent') === '1'
      ? `${searchParams.get('conversation') ?? ''}:${searchParams.get('workspace') ?? ''}`
      : null
    if (intent && intent !== openedIntent.current) onOpen()
    openedIntent.current = intent
  }, [enabled, onOpen, searchParams])
  return null
}
