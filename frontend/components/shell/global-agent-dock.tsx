'use client'

import { useQuery, useQueryClient } from '@tanstack/react-query'
import dynamic from 'next/dynamic'
import Link from 'next/link'
import { Bot, Check, Loader2, PanelRightClose, PanelRightOpen, Plus, Search, Send, ShieldCheck, X } from 'lucide-react'
import { usePathname, useRouter, useSearchParams } from 'next/navigation'
import { FormEvent, KeyboardEvent, useCallback, useEffect, useMemo, useRef, useState } from 'react'

import { Button } from '@/components/ui/button'
import { ScrollArea } from '@/components/ui/scroll-area'
import { useAuth } from '@/components/auth/auth-provider'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Textarea } from '@/components/ui/textarea'
import {
  closeAgentConversation,
  cancelAgentConversationTurn,
  createAgentTerminal,
  getAgentTerminal,
  stopAgentTerminal,
  createAgentConversation,
  getAgentConversation,
  getAgentChatOptions,
  listAgentConversations,
  sendAgentConversationMessage,
  type AgentConversation,
  type AgentConversationContext,
  type AgentConversationDetail,
  type AgentConversationProposal,
  type AgentExecutionConfig,
  type AgentTerminalSession,
} from '@/lib/api/agent-conversations'
import { apiClient } from '@/lib/api/client'
import { proposalQueryKeys, recentAgentMessages } from '@/lib/agent-dock-state'
import { ROUTE_LABELS } from '@/lib/navigation'
import { useGovernedWorkspaces } from '@/lib/api/hooks'
import { buildRunUrl } from '@/lib/studio/run-navigation'
import { AliceChatSurface } from '@/components/experience/alice-chat/chat-surface'
import { AgentRuntimePicker } from '@/components/experience/alice-chat/agent-runtime-picker'
import { AgentLaunchSelectors } from '@/components/experience/alice-chat/agent-launch-selectors'
import { readWorkspacePreference, writeWorkspacePreference } from '@/lib/workspace-preference'

const NativeTerminal = dynamic(
  () => import('@/components/experience/alice-chat/terminal/native-terminal'),
  { ssr: false, loading: () => <div className="grid h-full min-h-[22rem] place-items-center text-sm text-muted-foreground"><Loader2 className="mr-2 inline size-4 animate-spin" aria-hidden />正在加载终端</div> },
)

type AgentMessage = {
  role: 'user' | 'assistant'
  content: string
}

type AgentProposal = AgentConversationProposal

type AgentReply = {
  type: 'message' | 'proposal'
  content?: string | null
  proposal?: AgentProposal | null
}

function restoreConversation(detail: AgentConversationDetail) {
  const restoredMessages: AgentMessage[] = []
  let restoredProposal: AgentProposal | null = null
  let restoredError: string | null = null

  for (const turn of [...detail.turns].sort((left, right) => left.sequence - right.sequence)) {
    if (turn.user_content) {
      restoredMessages.push({ role: 'user', content: turn.user_content })
    }
    const response = turn.response as AgentReply | null | undefined
    if (response?.type === 'proposal' && response.proposal) {
      restoredProposal = response.proposal
    } else if (response?.type === 'message') {
      const content = response.content?.trim()
      if (content) restoredMessages.push({ role: 'assistant', content })
    }
    restoredError = turn.status === 'failed' ? turn.error_message ?? 'Agent 暂时不可用' : null
  }

  const running = [...detail.turns].sort((left, right) => right.sequence - left.sequence).find((turn) => turn.status === 'running')
  return { messages: restoredMessages, proposal: restoredProposal, error: restoredError, running }
}

function matchesRequestedContext(detail: AgentConversationDetail, context: AgentConversationContext) {
  return (!context.project_id || detail.context_binding.project_id === context.project_id)
    && (!context.workflow_id || detail.context_binding.workflow_id === context.workflow_id)
    && (!context.run_id || detail.context_binding.run_id === context.run_id)
}

function matchesRequestedWorkspace(
  detail: AgentConversationDetail | AgentConversation,
  workspaceId: string,
  isStudioBridge: boolean,
) {
  return isStudioBridge
    ? detail.context_binding.studio_workspace_id === workspaceId
    : detail.workspace_id === workspaceId && !detail.context_binding.studio_workspace_id
}


export function GlobalAgentDock({
  open,
  onOpenChange,
  initialPrompt = '',
  presentation = 'dialog',
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  initialPrompt?: string
  presentation?: 'dialog' | 'page'
}) {
  const pathname = usePathname()
  const router = useRouter()
  const searchParams = useSearchParams()
  const queryClient = useQueryClient()
  const { identity } = useAuth()
  const navigationQuery = searchParams.toString()
  const navigationParams = useMemo(() => new URLSearchParams(navigationQuery), [navigationQuery])
  const authorizedWorkspaces = useGovernedWorkspaces()
  const requestedWorkspaceId = navigationParams.get('workspace')
  const requestedConversationId = navigationParams.get('conversation')
  const [preferredWorkspaceId, setPreferredWorkspaceId] = useState<string | null>(null)
  const [selectedWorkspaceId, setSelectedWorkspaceId] = useState<string | null>(null)
  const validPreferredWorkspaceId = authorizedWorkspaces.data?.some((workspace) => workspace.id === preferredWorkspaceId) ? preferredWorkspaceId : null
  const workspaceId = requestedWorkspaceId
    ?? selectedWorkspaceId
    ?? (authorizedWorkspaces.data?.length === 1 ? authorizedWorkspaces.data[0].id : null)
    ?? validPreferredWorkspaceId
  const authorizedWorkspace = workspaceId
    ? authorizedWorkspaces.data?.find((workspace) => workspace.id === workspaceId)
    : undefined
  const context: AgentConversationContext = useMemo(() => ({
    surface: ROUTE_LABELS[pathname] ?? pathname,
    project_id: navigationParams.get('project')
      ?? pathname.match(/^\/studio\/projects\/([^/]+)/)?.[1]
      ?? null,
    workflow_id: navigationParams.get('workflow'),
    run_id: navigationParams.get('run'),
    source_id: navigationParams.get('source')
      ?? pathname.match(/^\/sources\/([^/]+)/)?.[1]
      ?? null,
  }), [navigationParams, pathname])
  const hasScopedResultContext = Boolean(context.project_id || context.workflow_id || context.run_id)
  const isTrustedStudioContext = Boolean(
    requestedWorkspaceId
      && hasScopedResultContext
      && (identity?.auth_method === 'local' || identity?.auth_method === 'bootstrap')
      && identity?.is_platform_admin,
  )
  const studioStorageScopeReady = Boolean(
    isTrustedStudioContext
      && authorizedWorkspaces.isSuccess
      && authorizedWorkspaces.data?.length === 1,
  )
  const workspaceUsable = Boolean(authorizedWorkspace || studioStorageScopeReady)
  const workspaceScopeError = requestedWorkspaceId && authorizedWorkspaces.isSuccess && !workspaceUsable
    ? isTrustedStudioContext
      ? '当前项目 Agent 需要唯一的受管工作区，暂时无法保存会话。'
      : '该 Workspace 不在你的授权范围内。请选择一个可用 Workspace 后再继续。'
    : null
  const storageKey = workspaceId ? `opencli:agent-session:${workspaceId}` : null
  const [sessions, setSessions] = useState<AgentConversation[]>([])
  const [sessionId, setSessionId] = useState<string | null>(null)
  const [loadedWorkspaceId, setLoadedWorkspaceId] = useState<string | null>(null)
  const [messages, setMessages] = useState<AgentMessage[]>([])
  const [terminalSession, setTerminalSession] = useState<AgentTerminalSession | null>(null)
  const [terminalStopPending, setTerminalStopPending] = useState(false)
  const [input, setInput] = useState('')
  const [proposal, setProposal] = useState<AgentProposal | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [sending, setSending] = useState(false)
  const [activeRequestId, setActiveRequestState] = useState<string | null>(null)
  const activeRequestRef = useRef<string | null>(null)
  const setActiveRequestId = useCallback((requestId: string | null) => {
    activeRequestRef.current = requestId
    setActiveRequestState(requestId)
  }, [])
  const [stopState, setStopState] = useState<'idle' | 'requesting' | 'requested'>('idle')
  const processing = sending || Boolean(activeRequestId)
  const [confirming, setConfirming] = useState(false)
  const [loadingSessions, setLoadingSessions] = useState(false)
  const [loadingConversation, setLoadingConversation] = useState(false)
  const [closing, setClosing] = useState(false)
  const [sessionSearch, setSessionSearch] = useState('')
  const [completedResultHref, setCompletedResultHref] = useState<string | null>(null)
  const [expanded, setExpanded] = useState(false)
  const [draftExecution, setDraftExecution] = useState<AgentExecutionConfig>({ runtime_id: 'opencli', mode: 'gui' })
  const launchOptions = useQuery({
    queryKey: ['agent-chat-options', identity?.subject, workspaceId],
    queryFn: () => getAgentChatOptions(workspaceId as string),
    enabled: presentation === 'page' && open && Boolean(authorizedWorkspace),
    retry: false,
    staleTime: 30_000,
  })
  const inputRef = useRef<HTMLTextAreaElement>(null)
  const skipConversationLoadRef = useRef<string | null>(null)
  const activeWorkspaceRef = useRef<string | null>(null)
  const requestGenerationRef = useRef(0)

  useEffect(() => {
    setPreferredWorkspaceId(readWorkspacePreference(identity))
  }, [identity])

  useEffect(() => {
    if (!workspaceId || !workspaceUsable) return
    writeWorkspacePreference(identity, workspaceId)
  }, [identity, workspaceId, workspaceUsable])

  useEffect(() => {
    activeWorkspaceRef.current = workspaceId
    setDraftExecution({ runtime_id: 'opencli', mode: 'gui' })
  }, [workspaceId])

  useEffect(() => {
    requestGenerationRef.current += 1
    setSending(false)
    setActiveRequestId(null)
    setStopState('idle')
    setTerminalStopPending(false)
    setConfirming(false)
    setClosing(false)
  }, [workspaceId, context.project_id, context.workflow_id, context.run_id, requestedConversationId, setActiveRequestId])

  useEffect(() => {
    if (open && initialPrompt) setInput(initialPrompt)
  }, [initialPrompt, open])

  useEffect(() => {
    skipConversationLoadRef.current = null
    if (!open) return
    setLoadingSessions(false)
    setLoadingConversation(false)
    setLoadedWorkspaceId(null)
    setSessions([])
    setSessionId(null)
    setMessages([])
    setTerminalSession(null)
    setProposal(null)
    setCompletedResultHref(null)
    setError(null)
    if (!workspaceId || !storageKey || !workspaceUsable || workspaceScopeError) return

    let cancelled = false
    setLoadingSessions(true)
    void listAgentConversations(workspaceId, 20, hasScopedResultContext ? {
      project_id: context.project_id,
      workflow_id: context.workflow_id,
      run_id: context.run_id,
    } : undefined)
      .then((nextSessions) => {
        if (cancelled) return
        const storedId = window.localStorage.getItem(storageKey)
        const requestedSession = requestedConversationId ? nextSessions.find((session) => session.id === requestedConversationId) : undefined
        const storedSession = storedId ? nextSessions.find((session) => session.id === storedId) : undefined
        const storedMatchesContext = storedSession && (!hasScopedResultContext || (
          (!context.project_id || storedSession.context_binding.project_id === context.project_id)
          && (!context.workflow_id || storedSession.context_binding.workflow_id === context.workflow_id)
          && (!context.run_id || storedSession.context_binding.run_id === context.run_id)
        ))
        const selectedId = requestedConversationId
          ? requestedSession?.id ?? null
          : storedMatchesContext ? storedSession.id : null
        setSessions(nextSessions)
        setLoadedWorkspaceId(workspaceId)
        setSessionId(selectedId)
        if (requestedConversationId && !requestedSession) {
          return getAgentConversation(requestedConversationId)
            .then((detail) => {
              if (cancelled || activeWorkspaceRef.current !== workspaceId) return
              if (!matchesRequestedWorkspace(detail, workspaceId, isTrustedStudioContext) || !matchesRequestedContext(detail, context)) {
                setError('指定的 Agent 会话与当前 Workspace 或结果上下文不匹配。没有打开其他会话。')
                return
              }
              setSessions((current) => [detail, ...current.filter((session) => session.id !== detail.id)])
              setSessionId(detail.id)
              window.localStorage.setItem(storageKey, detail.id)
            })
            .catch(() => {
              if (!cancelled && activeWorkspaceRef.current === workspaceId) setError('指定的 Agent 会话不存在，或你无权访问。没有打开其他会话。')
            })
        } else if (requestedConversationId && requestedSession && !matchesRequestedWorkspace(requestedSession, workspaceId, isTrustedStudioContext)) {
          setSessionId(null)
          setError('指定的 Agent 会话不属于当前 Workspace。没有打开其他会话。')
        } else if (selectedId) window.localStorage.setItem(storageKey, selectedId)
        else window.localStorage.removeItem(storageKey)
      })
      .catch((reason) => {
        if (!cancelled) {
          setLoadedWorkspaceId(workspaceId)
          setError(reason instanceof Error ? reason.message : '会话列表暂时不可用')
        }
      })
      .finally(() => {
        if (!cancelled) setLoadingSessions(false)
      })
    return () => {
      cancelled = true
    }
  }, [context, hasScopedResultContext, isTrustedStudioContext, open, requestedConversationId, storageKey, workspaceId, workspaceScopeError, workspaceUsable])

  useEffect(() => {
    if (!open || !workspaceId || !sessionId || loadedWorkspaceId !== workspaceId || !workspaceUsable || workspaceScopeError) return
    if (skipConversationLoadRef.current === sessionId) {
      skipConversationLoadRef.current = null
      return
    }
    let cancelled = false
    setLoadingConversation(true)
    setMessages([])
    setTerminalSession(null)
    setProposal(null)
    setError(null)
    void getAgentConversation(sessionId)
      .then(async (detail) => {
        if (cancelled || activeWorkspaceRef.current !== workspaceId) return
        if (!matchesRequestedWorkspace(detail, workspaceId, isTrustedStudioContext) || (requestedConversationId && !matchesRequestedContext(detail, context))) {
          setSessionId(null)
          setError('指定的 Agent 会话与当前 Workspace 或结果上下文不匹配，无法恢复。')
          return
        }
        const restored = restoreConversation(detail)
        setActiveRequestId(restored.running?.request_id ?? null)
        setStopState(restored.running?.error_code === 'cancel_requested' ? 'requested' : 'idle')
        setMessages(restored.messages)
        setProposal(restored.proposal)
        setError(restored.error)
        if (detail.execution?.mode === 'terminal') {
          try {
            const nextTerminal = await getAgentTerminal(detail.id)
            if (!cancelled && activeWorkspaceRef.current === workspaceId) {
              setTerminalSession(nextTerminal)
              setTerminalStopPending(nextTerminal.status === 'stopping')
            }
          } catch (reason) {
            const status = reason instanceof Error && 'status' in reason ? reason.status : undefined
            if (!cancelled && status !== 404) {
              setError(reason instanceof Error ? reason.message : '终端会话恢复失败')
            }
          }
        }
      })
      .catch((reason) => {
        if (!cancelled) setError(reason instanceof Error ? reason.message : '会话恢复失败')
      })
      .finally(() => {
        if (!cancelled) setLoadingConversation(false)
      })
    return () => {
      cancelled = true
    }
  }, [context, isTrustedStudioContext, loadedWorkspaceId, open, requestedConversationId, sessionId, workspaceId, setActiveRequestId, workspaceScopeError, workspaceUsable])

  useEffect(() => {
    if (!open || !sessionId || !workspaceId || !workspaceUsable || !processing) return
    let cancelled = false
    let timer: ReturnType<typeof setTimeout> | undefined
    async function syncRunningTurn() {
      try {
        const detail = await getAgentConversation(sessionId as string)
        if (cancelled || activeWorkspaceRef.current !== workspaceId) return
        if (!matchesRequestedWorkspace(detail, workspaceId as string, isTrustedStudioContext)) {
          setError('运行状态与当前工作区不匹配。')
          return
        }
        const restored = restoreConversation(detail)
        const trackedTurn = detail.turns.find((turn) => turn.request_id === activeRequestRef.current)
        if (trackedTurn && trackedTurn.status !== 'running') {
          setActiveRequestId(null)
          setSending(false)
          setStopState('idle')
          setMessages(restored.messages)
          setProposal(restored.proposal)
          setError(restored.error)
          if (trackedTurn.status !== 'failed') setInput((current) => current.trim() === trackedTurn.user_content ? '' : current)
          return
        }
        if (restored.running && (!activeRequestRef.current || restored.running.request_id === activeRequestRef.current)) {
          setActiveRequestId(restored.running.request_id)
          if (restored.running.error_code === 'cancel_requested') setStopState('requested')
        }
        if (!sending) {
          setMessages(restored.messages)
          setProposal(restored.proposal)
        }
      } catch (reason) {
        if (cancelled) return
        const status = reason instanceof Error && 'status' in reason ? reason.status : undefined
        if (status === 403 || status === 404) {
          setError('无法访问当前会话，未确认运行是否停止。')
          return
        }
        setError('暂时无法同步运行状态；未确认停止，正在重试。')
      }
      if (!cancelled) timer = setTimeout(() => void syncRunningTurn(), 1000)
    }
    void syncRunningTurn()
    return () => {
      cancelled = true
      if (timer) clearTimeout(timer)
    }
  }, [activeRequestId, isTrustedStudioContext, open, processing, sending, sessionId, workspaceId, setActiveRequestId, workspaceUsable])

  useEffect(() => {
    if (!open || !sessionId || terminalSession?.status !== 'stopping') return
    let cancelled = false
    let timer: ReturnType<typeof setTimeout> | undefined
    const refreshTerminal = async (): Promise<void> => {
      try {
        const nextTerminal = await getAgentTerminal(sessionId)
        if (cancelled) return
        setTerminalSession(nextTerminal)
        if (nextTerminal.status === 'exited' && nextTerminal.cleanup_confirmed) {
          setTerminalStopPending(false)
          return
        }
      } catch (reason) {
        if (!cancelled) setError(reason instanceof Error ? reason.message : '终端停止状态暂时无法确认。')
      }
      if (!cancelled) timer = setTimeout(() => void refreshTerminal(), 1000)
    }
    timer = setTimeout(() => void refreshTerminal(), 1000)
    return () => {
      cancelled = true
      if (timer) clearTimeout(timer)
    }
  }, [open, sessionId, terminalSession?.status])

  async function stopReply() {
    if (!sessionId || !activeRequestId || stopState !== 'idle') return
    const generation = requestGenerationRef.current
    const cancelledRequestId = activeRequestId
    setStopState('requesting')
    try {
      const result = await cancelAgentConversationTurn(sessionId, cancelledRequestId)
      if (requestGenerationRef.current !== generation || activeRequestRef.current !== cancelledRequestId) return
      if (!result.accepted) throw new Error('服务端没有接受停止请求。')
      setStopState('requested')
    } catch (reason) {
      if (requestGenerationRef.current !== generation || activeRequestRef.current !== cancelledRequestId) return
      setStopState('idle')
      setError(reason instanceof Error ? reason.message : '停止请求失败；尚未确认停止。')
    }
  }

  async function stopTerminal() {
    if (!sessionId || !terminalSession || terminalStopPending || terminalSession.status === 'exited') return
    const generation = requestGenerationRef.current
    setTerminalStopPending(true)
    setTerminalSession((current) => current ? { ...current, status: 'stopping' } : current)
    setError(null)
    try {
      const stopped = await stopAgentTerminal(sessionId)
      if (requestGenerationRef.current !== generation) return
      setTerminalSession(stopped)
      setTerminalStopPending(!stopped.cleanup_confirmed)
    } catch (reason) {
      if (requestGenerationRef.current !== generation) return
      setError(reason instanceof Error ? reason.message : '停止请求已发送，但进程组清理尚未确认。')
    }
  }

  function startNewSession() {
    requestGenerationRef.current += 1
    setSessionId(null)
    setActiveRequestId(null)
    setStopState('idle')
    setMessages([])
    setTerminalSession(null)
    setTerminalStopPending(false)
    setProposal(null)
    setCompletedResultHref(null)
    setError(null)
    setInput('')
    if (requestedConversationId) {
      const params = new URLSearchParams(navigationQuery)
      params.delete('conversation')
      router.replace(`${pathname}?${params.toString()}`)
    }
    if (storageKey) window.localStorage.removeItem(storageKey)
  }

  function selectWorkspace(nextId: string) {
    if (!authorizedWorkspaces.data?.some((workspace) => workspace.id === nextId)) return
    const nextParams = new URLSearchParams()
    nextParams.set('workspace', nextId)
    if (presentation === 'dialog') nextParams.set('agent', '1')
    router.replace(`${presentation === 'page' ? '/launch' : '/studio'}?${nextParams.toString()}`)
    setSelectedWorkspaceId(nextId)
    requestGenerationRef.current += 1
    setSessionId(null)
    setMessages([])
    setTerminalSession(null)
    setTerminalStopPending(false)
    setProposal(null)
    setError(null)
    setInput('')
  }

  function selectSession(nextId: string) {
    requestGenerationRef.current += 1
    setError(null)
    setSessionId(nextId || null)
    setTerminalSession(null)
    setTerminalStopPending(false)
    setLoadingConversation(Boolean(nextId))
    setCompletedResultHref(null)
    setInput('')
    if (storageKey && nextId) window.localStorage.setItem(storageKey, nextId)
    else if (storageKey) window.localStorage.removeItem(storageKey)
    if (presentation === 'page' || requestedConversationId) {
      const params = new URLSearchParams(navigationQuery)
      if (nextId) params.set('conversation', nextId)
      else params.delete('conversation')
      router.replace(`${pathname}?${params.toString()}`)
    }
  }

  function fillSuggestion(suggestion: string) {
    setInput(suggestion)
    inputRef.current?.focus()
  }

  async function closeSession() {
    if (!sessionId || closing) return
    const requestGeneration = requestGenerationRef.current
    setClosing(true)
    setError(null)
    try {
      await closeAgentConversation(sessionId)
      if (requestGenerationRef.current !== requestGeneration) return
      const nextSessions = sessions.map((session) => (
        session.id === sessionId ? { ...session, status: 'closed' as const } : session
      ))
      const nextSelected = nextSessions.find(
        (session) => session.id !== sessionId && session.status === 'active',
      )?.id ?? null
      setSessions(nextSessions)
      setSessionId(nextSelected)
      setMessages([])
      setTerminalSession(null)
      setTerminalStopPending(false)
      setProposal(null)
      setCompletedResultHref(null)
      if (storageKey && nextSelected) window.localStorage.setItem(storageKey, nextSelected)
      else if (storageKey) window.localStorage.removeItem(storageKey)
      if (requestedConversationId) {
        const params = new URLSearchParams(navigationQuery)
        if (nextSelected) params.set('conversation', nextSelected)
        else params.delete('conversation')
        router.replace(`${pathname}?${params.toString()}`)
      }
    } catch (reason) {
      if (requestGenerationRef.current !== requestGeneration) return
      setError(reason instanceof Error ? reason.message : '关闭会话失败')
    } finally {
      if (requestGenerationRef.current === requestGeneration) setClosing(false)
    }
  }

  async function sendMessage(event?: FormEvent) {
    event?.preventDefault()
    const content = input.trim()
    if (!content || submissionBlockedReason) return

    setError(null)
    setSending(true)
    const requestGeneration = requestGenerationRef.current
    let requestId: string | null = null
    let activeSessionId = sessionId
    try {
      const requestWorkspaceId = workspaceId
      const messageContext = hasScopedResultContext ? context : selectedSession?.context_binding ?? context
      const terminalMode = presentation === 'page' && execution.mode === 'terminal'
      if (!activeSessionId) {
        const created = await createAgentConversation({
          workspace_id: workspaceId,
          title: presentation === 'page' ? content.slice(0, 80) : 'Global Agent session',
          context: messageContext,
          ...(presentation === 'page' ? { execution: draftExecution } : {}),
        })
        if (activeWorkspaceRef.current !== requestWorkspaceId || requestGenerationRef.current !== requestGeneration) return
        activeSessionId = created.id
        setSessions((current) => [created, ...current.filter((session) => session.id !== created.id)])
        setLoadedWorkspaceId(workspaceId)
        skipConversationLoadRef.current = created.id
        setSessionId(created.id)
        if (storageKey) window.localStorage.setItem(storageKey, created.id)
      }

      if (terminalMode) {
        const started = await createAgentTerminal(activeSessionId, { initial_input: content })
        if (activeWorkspaceRef.current !== requestWorkspaceId || requestGenerationRef.current !== requestGeneration) return
        setTerminalSession(started)
        setTerminalStopPending(started.status === 'stopping')
        setInput((current) => current === content ? '' : current)
        return
      }

      requestId = crypto.randomUUID()
      setActiveRequestId(requestId)
      const result = await sendAgentConversationMessage(activeSessionId, {
        request_id: requestId,
        content,
        context: messageContext,
      })
      if (activeWorkspaceRef.current !== requestWorkspaceId || requestGenerationRef.current !== requestGeneration || activeRequestRef.current !== requestId) return
      setSending(false)
      setActiveRequestId(null)
      setStopState('idle')
      setMessages((current) => [...current, { role: 'user', content }])
      if (result.turn.status === 'failed') {
        setError(result.turn.error_message ?? 'Agent 暂时不可用')
        return
      }
      const reply = result.turn.response as AgentReply | null | undefined
      if (reply?.type === 'proposal' && reply.proposal) {
        setProposal(reply.proposal)
      } else {
        setMessages((current) => [
          ...current,
          { role: 'assistant', content: reply?.content?.trim() || '没有返回内容。' },
        ])
      }
      setInput((current) => current === content ? '' : current)
    } catch (reason) {
      if (requestGenerationRef.current !== requestGeneration || (requestId && activeRequestRef.current !== requestId)) return
      setError(reason instanceof Error ? reason.message : 'Agent 暂时不可用')
      const status = reason instanceof Error && 'status' in reason ? reason.status : undefined
      if (requestId && typeof status === 'number' && [400, 403, 404, 409, 422].includes(status)) {
        try {
          if (!activeSessionId) return
          const detail = await getAgentConversation(activeSessionId)
          if (requestGenerationRef.current !== requestGeneration || activeRequestRef.current !== requestId) return
          if (!matchesRequestedWorkspace(detail, workspaceId as string, isTrustedStudioContext)) return
          const restored = restoreConversation(detail)
          setActiveRequestId(restored.running?.request_id ?? null)
          setStopState(restored.running?.error_code === 'cancel_requested' ? 'requested' : 'idle')
          setSending(false)
        } catch {
          return
        }
      }
    } finally {
      if (requestGenerationRef.current === requestGeneration && (!requestId || activeRequestRef.current === requestId)) {
        setSending(false)
      }
    }
  }

  async function confirmProposal() {
    if (!proposal || confirming) return
    setError(null)
    setConfirming(true)
    const proposalToConfirm = proposal
    const requestGeneration = requestGenerationRef.current
    try {
      const requestWorkspaceId = workspaceId
      const confirmation = await apiClient.post('/chat/confirm', { proposal })
      if (activeWorkspaceRef.current !== requestWorkspaceId || requestGenerationRef.current !== requestGeneration) return
      setMessages((current) => [
        ...current,
        { role: 'assistant', content: `已执行：${proposal.summary}` },
      ])
      const result = confirmation.data?.data as Record<string, unknown> | undefined
      const resultProjectId = typeof result?.project_id === 'string' ? result.project_id : typeof proposal.args.project_id === 'string' ? proposal.args.project_id : null
      const resultWorkflowId = typeof result?.workflow_id === 'string' ? result.workflow_id : typeof proposal.args.workflow_id === 'string' ? proposal.args.workflow_id : null
      const resultWorkspaceId = typeof result?.workspace_id === 'string' ? result.workspace_id : proposal.workspace_id ?? workspaceId
      if (resultWorkspaceId && resultProjectId && resultWorkflowId && (proposal.tool === 'create_project' || proposal.tool === 'update_workflow_draft')) {
        await queryClient.invalidateQueries({ queryKey: ['workspace-projects', resultWorkspaceId] })
        await queryClient.invalidateQueries({ queryKey: ['project-workflows', resultWorkspaceId, resultProjectId] })
        if (requestGenerationRef.current !== requestGeneration) return
        setCompletedResultHref(`/studio/workflow?workspace=${resultWorkspaceId}&project=${resultProjectId}&workflow=${resultWorkflowId}`)
      }
      setProposal(null)
      await Promise.all(proposalQueryKeys(proposalToConfirm).map((queryKey) =>
        queryClient.invalidateQueries({ queryKey }),
      ))
      if (sessionId && requestGenerationRef.current === requestGeneration) {
        const updated = await getAgentConversation(sessionId)
        if (requestGenerationRef.current === requestGeneration) {
          setSessions((current) => [updated, ...current.filter((session) => session.id !== updated.id)])
        }
      }
    } catch (reason) {
      if (requestGenerationRef.current !== requestGeneration) return
      const status = reason instanceof Error && 'status' in reason ? reason.status : undefined
      const message = reason instanceof Error ? reason.message : '操作执行失败'
      setError(
        status === 409
          ? `提案已失效或目标已变化：${message}。请拒绝后重新发起。`
          : message,
      )
    } finally {
      if (requestGenerationRef.current === requestGeneration) setConfirming(false)
    }
  }

  function handleKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key !== 'Enter' || event.shiftKey || event.nativeEvent.isComposing) return
    event.preventDefault()
    void sendMessage()
  }

  const visibleMessages = recentAgentMessages(messages)
  const selectedSession = sessions.find((session) => session.id === sessionId)
  const execution = selectedSession?.execution ?? draftExecution
  const interfaceMode = execution.mode ?? 'gui'
  const selectedRuntime = launchOptions.data?.runtimes.find((runtime) => runtime.id === execution.runtime_id)
  const availableModes = (selectedRuntime?.modes ?? ['gui']).filter((mode): mode is 'gui' | 'terminal' => mode === 'gui' || mode === 'terminal')
  const launchOptionsReady = Array.isArray(launchOptions.data?.runtimes) && Array.isArray(launchOptions.data?.providers)
  const launchBlockedReason = presentation === 'page'
    ? launchOptions.isFetching && !launchOptionsReady
      ? '正在检查 Agent 执行配置。'
      : !launchOptionsReady
        ? '无法读取 Agent 执行配置，请重试。'
        : !selectedRuntime?.available
          ? selectedRuntime?.reason || '当前运行时尚不可用，请检查执行配置。'
          : interfaceMode === 'terminal' && !selectedRuntime.modes.includes('terminal')
            ? '当前运行时不支持 TUI，请选择 Codex 或 OMP。'
          : execution.runtime_id === 'opencli' && !execution.provider_id && launchOptions.data?.default_route_ready === false
            ? '尚未配置可用模型连接，请先配置连接后重试。'
            : null
    : null
  const submissionBlockedReason = workspaceScopeError
    ?? (!workspaceId || !workspaceUsable
      ? '选择可用 Workspace 后即可发送。'
      : loadingSessions || loadingConversation
        ? '正在加载会话，暂不能发送。'
        : selectedSession?.status === 'closed'
          ? '当前会话已关闭，请新建或选择进行中的会话。'
          : presentation === 'dialog' && interfaceMode === 'terminal'
            ? '该 TUI 会话只能在启动页继续。'
          : processing
            ? stopState === 'idle' ? 'Agent 正在处理当前消息。' : '已请求停止，正在等待服务端确认；已完成的操作不会撤销。'
            : confirming
              ? '请先完成当前提案的确认。'
              : closing
                ? '正在关闭会话，暂不能发送。'
                : proposal
                  ? '请先确认或暂不执行当前提案。'
                  : launchBlockedReason)
  const inputDisabled = Boolean(submissionBlockedReason)
  const visibleSessions = sessions.filter((session) => {
    const query = sessionSearch.trim().toLowerCase()
    return !query || `${session.title ?? ''} ${session.id} ${session.context_binding.project_id ?? ''} ${session.context_binding.workflow_id ?? ''}`.toLowerCase().includes(query)
  })
  const canClose = Boolean(
    selectedSession?.status === 'active'
      && !processing
      && !confirming
      && !closing
      && (!terminalSession || (terminalSession.status === 'exited' && terminalSession.cleanup_confirmed)),
  )

  const conversation = (<>
            {visibleMessages.map((message, index) => (
              <div
                key={`${message.role}-${index}`}
                className={message.role === 'user'
                  ? 'ml-8 whitespace-pre-wrap break-words rounded-md bg-primary px-3 py-2 text-sm text-primary-foreground'
                  : 'mr-8 whitespace-pre-wrap break-words rounded-md border bg-muted/30 px-3 py-2 text-sm'}
              >
                {message.content}
              </div>
            ))}
            {selectedSession?.context_binding && (selectedSession.context_binding.project_id || selectedSession.context_binding.workflow_id || selectedSession.context_binding.run_id) ? (
              <nav className="flex flex-wrap gap-2 border-t pt-3 text-xs" aria-label="会话上下文链接">
                {selectedSession.context_binding.project_id ? <Link className="underline underline-offset-4" href={`/studio/projects/${selectedSession.context_binding.project_id}?workspace=${workspaceId}`}>项目</Link> : null}
                {selectedSession.context_binding.workflow_id && selectedSession.context_binding.project_id ? <Link className="underline underline-offset-4" href={`/studio/workflow?workspace=${workspaceId}&project=${selectedSession.context_binding.project_id}&workflow=${selectedSession.context_binding.workflow_id}`}>工作流草稿</Link> : null}
                {selectedSession.context_binding.run_id ? <Link className="underline underline-offset-4" href={buildRunUrl('operations', { workspace: workspaceId ?? undefined, project: selectedSession.context_binding.project_id ?? undefined, workflow: selectedSession.context_binding.workflow_id ?? undefined, run: selectedSession.context_binding.run_id }) ?? '/studio'}>运行</Link> : null}
              </nav>
            ) : null}
            {completedResultHref ? <Link href={completedResultHref} className="inline-flex min-h-11 items-center text-xs font-medium underline underline-offset-4">打开 Agent 保存的工作流草稿</Link> : null}
            {sending ? (
              <div className="flex items-center gap-2 text-xs text-muted-foreground" role="status">
                <Loader2 className="size-3.5 animate-spin" aria-hidden />
                Agent 正在处理
              </div>
            ) : null}
            {proposal ? (
              <div className="rounded-md border border-warning/40 bg-warning/10 p-3">
                <div className="text-sm font-medium">待确认操作</div>
                <p className="mt-1 text-xs text-muted-foreground">{proposal.summary}</p>
                <div className="mt-3 rounded-xs border bg-background/70 p-2 font-mono text-2xs">
                  {proposal.diff}
                </div>
                <div className="mt-2 space-y-1 font-mono text-3xs text-muted-foreground">
                  <div>工作项：{proposal.work_item_id ?? '未生成'}</div>
                  <div>工作区：{proposal.workspace_id ?? '未绑定'}</div>
                  <div>提案版本：{proposal.proposal_version ?? '未生成'}</div>
                </div>
                <div className="mt-3 flex justify-end gap-2">
                  <Button
                    variant="ghost"
                    size="sm"
                    disabled={confirming}
                    onClick={() => setProposal(null)}
                  >
                    <X aria-hidden />
                    暂不执行
                  </Button>
                  <Button
                    size="sm"
                    disabled={
                      confirming
                      || !proposal.work_item_id
                      || !proposal.workspace_id
                      || !proposal.proposal_version
                    }
                    onClick={() => void confirmProposal()}
                  >
                    {confirming ? <Loader2 className="animate-spin" aria-hidden /> : <Check aria-hidden />}
                    确认执行
                  </Button>
                </div>
              </div>
            ) : null}

  </>)
  const terminalConversation = terminalSession ? (
    terminalSession.status === 'failed' || terminalSession.status === 'lost'
      ? <div className="grid h-full min-h-[22rem] place-items-center rounded-md border p-6 text-center text-sm"><div><p className="font-medium">终端状态无法确认</p><p className="mt-2 text-muted-foreground">不会自动创建替代会话。请确认隔离节点状态后新建对话。</p></div></div>
      : <NativeTerminal
          conversationId={terminalSession.conversation_id}
          terminal={terminalSession}
          onStatus={(next) => {
            setTerminalSession((current) => current ? { ...current, ...next } : current)
            if (next.status === 'exited' && next.cleanup_confirmed) setTerminalStopPending(false)
          }}
        />
  ) : null
  const terminalHasConversation = interfaceMode === 'terminal' && Boolean(terminalSession)

  if (presentation === 'page') return (
    <AliceChatSurface
      workspaces={authorizedWorkspaces.data ?? []}
      workspaceId={workspaceId}
      sessions={sessions}
      sessionId={sessionId}
      sessionsLoading={loadingSessions}
      busy={processing || confirming || closing || loadingConversation}
      canClose={canClose}
      input={input}
      inputDisabled={!workspaceUsable || loadedWorkspaceId !== workspaceId || processing || confirming || closing || loadingSessions || loadingConversation || Boolean(workspaceScopeError)}
      inputRef={inputRef}
      blockedReason={submissionBlockedReason}
      error={error}
      hasConversation={terminalHasConversation || Boolean(messages.length || proposal || processing || completedResultHref)}
      conversation={terminalHasConversation ? terminalConversation : conversation}
      conversationRevision={terminalHasConversation ? terminalSession?.revision ?? 0 : messages.length + Number(Boolean(proposal))}
      canStop={interfaceMode === 'terminal' ? Boolean(terminalSession && terminalSession.status !== 'exited') : Boolean(sessionId && activeRequestId)}
      stopping={interfaceMode === 'terminal' ? terminalStopPending || terminalSession?.status === 'stopping' || terminalSession?.status === 'lost' : stopState !== 'idle'}
      onStop={() => interfaceMode === 'terminal' ? void stopTerminal() : void stopReply()}
      mode={interfaceMode}
      availableModes={availableModes}
      onMode={(mode) => setDraftExecution((current) => ({ ...current, mode }))}
      runtimeSelector={<AgentRuntimePicker runtimes={launchOptions.data?.runtimes ?? []} selectedId={execution.runtime_id} disabled={Boolean(sessionId) || sending || loadingSessions || loadingConversation} onSelect={(runtimeId) => {
        const runtime = launchOptions.data?.runtimes.find((candidate) => candidate.id === runtimeId)
        setDraftExecution({
          runtime_id: runtimeId,
          access_mode: runtimeId === 'opencli' ? 'provider' : 'native',
          mode: draftExecution.mode === 'terminal' && runtime?.modes.includes('terminal') ? 'terminal' : 'gui',
        })
      }} />}
      launchControls={execution.runtime_id === 'opencli' && launchOptionsReady && launchOptions.data ? <AgentLaunchSelectors options={launchOptions.data} execution={execution} disabled={Boolean(sessionId) || sending || loadingSessions || loadingConversation} onChange={setDraftExecution} /> : null}
      launchStatus={(!launchOptionsReady || !selectedRuntime?.available || (execution.runtime_id === 'opencli' && launchOptions.data?.default_route_ready === false)) && !launchOptions.isFetching ? <button type="button" className="mt-2 px-3 text-xs underline" onClick={() => void launchOptions.refetch()} disabled={!authorizedWorkspace}>重试执行配置</button> : sessionId ? <p className="mt-2 px-3 text-xs text-muted-foreground">执行配置已随会话保存。更换连接或模型请新建对话。</p> : null}
      onInput={setInput}
      onKeyDown={handleKeyDown}
      onSubmit={(event) => void sendMessage(event)}
      onSuggestion={fillSuggestion}
      onWorkspace={selectWorkspace}
      onSession={selectSession}
      onNew={startNewSession}
      onClose={() => void closeSession()}
    />
  )

  const Header = DialogHeader
  const Title = DialogTitle
  const Description = DialogDescription
  const content = (
      <>
        <Header className="border-b px-4 py-3">
          <Title className="flex items-center gap-2">
            <Bot className="size-4 text-primary" aria-hidden />
            全局 Agent
          </Title>
          <Description >
            当前上下文：{ROUTE_LABELS[pathname] ?? pathname}。读取可直接执行，写入操作先生成确认提案。
            未明确指定 Workspace 时，仅在后端能解析出唯一授权范围时允许确认写操作。
          </Description>
          {authorizedWorkspaces.data && (authorizedWorkspaces.data.length > 1 || workspaceScopeError) ? (
            <label className="mt-2 block text-xs font-medium">
              Workspace
              <select
                value={workspaceId ?? ''}
                onChange={(event) => selectWorkspace(event.target.value)}
                className="mt-1 min-h-11 w-full rounded-xs border bg-background px-2 text-sm font-normal"
                aria-label="选择 Agent Workspace"
              >
                <option value="">选择 Workspace</option>
                {authorizedWorkspaces.data.map((workspace) => <option key={workspace.id} value={workspace.id}>{workspace.name}</option>)}
              </select>
            </label>
          ) : null}
          <div className="flex items-center gap-2">
            <div className="min-w-0 flex-1">
              <label className="sr-only" htmlFor="agent-session-search">搜索 Agent 会话</label>
              <div className="relative">
                <Search className="pointer-events-none absolute left-2 top-2 size-3.5 text-muted-foreground" aria-hidden />
                <input id="agent-session-search" value={sessionSearch} onChange={(event) => setSessionSearch(event.target.value)} placeholder="搜索会话、项目或工作流" className="min-h-9 w-full rounded-xs border bg-background py-1 pl-7 pr-2 text-xs" />
              </div>
              <select
                value={sessionId ?? ''}
                onChange={(event) => selectSession(event.target.value)}
                disabled={loadingSessions || sending || confirming || closing || !workspaceId}
                aria-label="选择 Agent 会话"
                className="mt-1 min-h-9 w-full rounded-xs border bg-background px-2 text-xs"
              >
                <option value="">新会话</option>
                {visibleSessions.map((session) => (
                  <option key={session.id} value={session.id}>
                    {session.title || `会话 ${session.id.slice(0, 8)}`} · {session.context_binding.project_id ? `项目 ${session.context_binding.project_id.slice(0, 8)}` : '无项目'}（{session.status === 'active' ? '进行中' : '已关闭'}）
                  </option>
                ))}
              </select>
            </div>
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={startNewSession}
              disabled={sending || confirming || closing}
              aria-label="新建 Agent 会话"
            >
              <Plus aria-hidden />
              新建
            </Button>
            <Button
              type="button"
              variant="ghost"
              size="icon-sm"
              onClick={() => setExpanded((current) => !current)}
              aria-label={expanded ? '收起 Agent 面板' : '展开 Agent 面板'}
              title={expanded ? '收起 Agent 面板' : '展开 Agent 面板'}
            >
              {expanded ? <PanelRightClose aria-hidden /> : <PanelRightOpen aria-hidden />}
            </Button>
            <Button
              type="button"
              variant="ghost"
              size="icon-sm"
              onClick={() => void closeSession()}
              disabled={!canClose}
              aria-label="关闭当前 Agent 会话"
              title="关闭当前会话"
            >
              <X aria-hidden />
            </Button>
          </div>
        </Header>

        <ScrollArea className="min-h-0 flex-1">
          <div className="space-y-3 p-4" aria-live="polite">
            {workspaceScopeError ? <div className="rounded-md border border-destructive/40 bg-destructive/10 p-4 text-xs" role="alert">{workspaceScopeError}</div> : null}
            {!workspaceId && !workspaceScopeError ? (
              <div className="rounded-md border border-warning/40 bg-warning/10 p-4 text-xs" role="alert">
                当前 Workspace 不明确。请在这里选择一个已授权 Workspace；不会自动跨范围打开或创建会话。
              </div>
            ) : null}
            {loadingSessions || loadingConversation ? (
              <div className="flex items-center gap-2 text-xs text-muted-foreground" role="status">
                <Loader2 className="size-3.5 animate-spin" aria-hidden />
                {loadingSessions ? '正在加载会话' : '正在恢复会话'}
              </div>
            ) : null}
            {messages.length === 0 && !loadingConversation ? (
              <div className="rounded-md border border-dashed p-4">
                <div className="flex items-center gap-2 text-sm font-medium">
                  <ShieldCheck className="size-4 text-success" aria-hidden />
                  所有页面共用一个操作入口
                </div>
                <p className="mt-2 text-xs leading-5 text-muted-foreground">
                  可以查询数据源、调度、任务和模型连接；涉及启停、触发或配置变更时会先展示差异。
                </p>
                <div className="mt-3 flex flex-wrap gap-2" aria-label="常用 Agent 建议">
                  {['查看当前项目的工作流', '检查最近任务状态', '查看可用数据源和连接'].map((suggestion) => (
                    <Button
                      key={suggestion}
                      type="button"
                      variant="outline"
                      size="sm"
                      disabled={inputDisabled}
                      onClick={() => fillSuggestion(suggestion)}
                    >
                      {suggestion}
                    </Button>
                  ))}
                </div>
              </div>
            ) : null}
            {conversation}
            {error ? <p className="text-xs text-destructive" role="alert">{error}</p> : null}
          </div>
        </ScrollArea>

        <form className="border-t p-4" onSubmit={(event) => void sendMessage(event)}>
          <Textarea
            ref={inputRef}
            value={input}
            onChange={(event) => setInput(event.target.value)}
            onKeyDown={handleKeyDown}
            placeholder="告诉 Agent 你要查询或执行什么…"
            aria-label="给全局 Agent 的消息"
            className="max-h-36 min-h-20 resize-none rounded-xs"
            disabled={inputDisabled}
          />
          <div className="mt-2 flex items-center justify-between gap-3">
            <span className="text-3xs text-muted-foreground">{submissionBlockedReason ?? 'Enter 发送 · Shift+Enter 换行'}</span>
            <Button
              type="submit"
              size="sm"
              disabled={
                !input.trim()
                || inputDisabled
              }
            >
              {sending ? <Loader2 className="animate-spin" aria-hidden /> : <Send aria-hidden />}
              发送
            </Button>
          </div>
        </form>
      </>
  )
  return <Dialog open={open} onOpenChange={onOpenChange}><DialogContent aria-label="全局 Agent" className={`fixed bottom-4 right-4 top-auto left-auto flex h-[min(680px,calc(100vh-2rem))] w-[min(420px,calc(100vw-2rem))] max-w-none translate-x-0 translate-y-0 flex-col gap-0 overflow-hidden p-0 shadow-overlay sm:max-w-none ${expanded ? 'lg:w-[min(720px,calc(100vw-2rem))]' : 'lg:w-[min(420px,calc(100vw-2rem))]'}`}>{content}</DialogContent></Dialog>
}
