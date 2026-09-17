import { apiClient } from './client'
import type { ApiResponse } from './types'
import { z } from 'zod'

export type AgentConversationStatus = 'active' | 'closed'
export type AgentConversationTurnStatus = 'running' | 'completed' | 'proposal' | 'failed'

export type AgentExecutionConfig = {
  runtime_id: string
  mode?: 'gui' | 'terminal'
  access_mode?: 'provider' | 'native'
  binding_revision?: string | null
  provider_id?: string | null
  model_id?: string | null
}

export type AgentChatRuntime = {
  id: string
  name: string
  available: boolean
  installed: boolean | null
  reason: string | null
  modes: string[]
  access_modes: string[]
}

export type AgentChatOptions = {
  runtimes: AgentChatRuntime[]
  providers: Array<{
    id: string
    name: string
    default_model: string | null
    models: Array<{ id: string; name: string }>
  }>
  reasoning_efforts: string[]
  provider_selection_allowed: boolean
  default_route_ready: boolean
}

const chatOptionsSchema = z.object({
  runtimes: z.array(z.object({
    id: z.string().min(1),
    name: z.string().min(1),
    available: z.boolean(),
    installed: z.boolean().nullable(),
    reason: z.string().nullable(),
    modes: z.array(z.string()),
    access_modes: z.array(z.string()),
  })),
  providers: z.array(z.object({
    id: z.string().min(1),
    name: z.string(),
    default_model: z.string().nullable(),
    models: z.array(z.object({ id: z.string().min(1), name: z.string() })),
  })),
  reasoning_efforts: z.array(z.string()),
  provider_selection_allowed: z.boolean(),
  default_route_ready: z.boolean(),
})

export const getAgentChatOptions = (workspaceId: string) =>
  apiClient.get<ApiResponse<AgentChatOptions>>('/chat/options', { params: { workspace_id: workspaceId } })
    .then((response) => {
      const parsed = chatOptionsSchema.safeParse(response.data.data)
      if (!parsed.success) throw new Error('Agent 执行配置格式无效，请重试或联系管理员。')
      return parsed.data
    })

export type AgentConversationRequestContext = {
  project_id?: string | null
  workflow_id?: string | null
  run_id?: string | null
  source_id?: string | null
  surface?: string | null
}

export type AgentConversationContext = AgentConversationRequestContext & {
  /** Server-stamped marker for a durable session opened from Studio. */
  readonly studio_workspace_id?: string | null
}

export type AgentConversationProposal = {
  tool: string
  args: Record<string, unknown>
  summary: string
  diff: string
  work_item_id?: string | null
  workspace_id?: string | null
  proposal_version?: string | null
}

export type AgentConversationResponse = {
  type: 'message' | 'proposal'
  content?: string | null
  proposal?: AgentConversationProposal | null
}

export type AgentConversation = {
  id: string
  workspace_id: string
  title?: string | null
  status: AgentConversationStatus
  created_by_user_id?: string
  context_binding: AgentConversationContext
  execution?: AgentExecutionConfig
  revision: number
  created_at: string
  updated_at: string
}

export type AgentConversationTurn = {
  id: string
  conversation_id?: string
  workspace_id?: string
  sequence: number
  request_id: string
  user_content: string
  response?: AgentConversationResponse | null
  context_binding: AgentConversationContext
  tool_trace: Array<Record<string, unknown>>
  status: AgentConversationTurnStatus
  error_code?: string | null
  error_message?: string | null
  created_at?: string
  updated_at?: string
}

export type AgentConversationDetail = AgentConversation & {
  turns: AgentConversationTurn[]
}

export type CreateAgentConversationInput = {
  workspace_id?: string | null
  title?: string | null
  context: AgentConversationRequestContext
  execution?: AgentExecutionConfig
}

export type SendAgentConversationMessageInput = {
  request_id: string
  content: string
  context: AgentConversationRequestContext
}

export type AgentConversationListFilters = {
  project_id?: string | null
  workflow_id?: string | null
  run_id?: string | null
}

export type AgentConversationMessageResult = {
  conversation_id: string
  turn: AgentConversationTurn
}

export type AgentTerminalSessionStatus = 'starting' | 'active' | 'stopping' | 'exited' | 'failed' | 'lost'

export type AgentTerminalSession = {
  id: string
  conversation_id: string
  workspace_id: string
  runtime_id: 'codex' | 'omp'
  status: AgentTerminalSessionStatus
  exit_code: number | null
  cleanup_confirmed: boolean
  revision: number
  created_at: string
  updated_at: string
}

export type AgentTerminalTicket = {
  ticket: string
  expires_in: number
}

export const listAgentConversations = (
  workspaceId: string,
  limit = 20,
  filters?: AgentConversationListFilters,
) =>
  apiClient
    .get<ApiResponse<AgentConversation[]>>('/chat/sessions', {
      params: {
        workspace_id: workspaceId,
        limit,
        ...(filters?.project_id ? { project_id: filters.project_id } : {}),
        ...(filters?.workflow_id ? { workflow_id: filters.workflow_id } : {}),
        ...(filters?.run_id ? { run_id: filters.run_id } : {}),
      },
    })
    .then((response) => response.data.data)

export const createAgentConversation = (input: CreateAgentConversationInput) =>
  apiClient
    .post<ApiResponse<AgentConversation>>('/chat/sessions', input)
    .then((response) => response.data.data)

export const getAgentConversation = (conversationId: string, afterSequence = 0, limit = 50) =>
  apiClient
    .get<ApiResponse<AgentConversationDetail>>(`/chat/sessions/${conversationId}`, {
      params: { after_sequence: afterSequence, limit, latest: afterSequence === 0 },
    })
    .then((response) => response.data.data)

export const sendAgentConversationMessage = (
  conversationId: string,
  input: SendAgentConversationMessageInput,
) =>
  apiClient
    .post<ApiResponse<AgentConversationMessageResult>>(
      `/chat/sessions/${conversationId}/messages`,
      input,
    )
    .then((response) => response.data.data)

export const closeAgentConversation = (conversationId: string) =>
  apiClient
    .post<ApiResponse<AgentConversation>>(`/chat/sessions/${conversationId}/close`)
    .then((response) => response.data.data)

export const cancelAgentConversationTurn = (conversationId: string, requestId: string) =>
  apiClient.post<ApiResponse<{ accepted: boolean }>>(`/chat/sessions/${conversationId}/cancel`, { request_id: requestId })
    .then((response) => response.data.data)

export const createAgentTerminal = (
  conversationId: string,
  input: { initial_input: string; cols?: number; rows?: number },
) => apiClient
  .post<ApiResponse<AgentTerminalSession>>(`/chat/sessions/${conversationId}/terminal`, input)
  .then((response) => response.data.data)

export const getAgentTerminal = (conversationId: string) => apiClient
  .get<ApiResponse<AgentTerminalSession>>(`/chat/sessions/${conversationId}/terminal`)
  .then((response) => response.data.data)

export const stopAgentTerminal = (conversationId: string) => apiClient
  .post<ApiResponse<AgentTerminalSession>>(`/chat/sessions/${conversationId}/terminal/stop`)
  .then((response) => response.data.data)

export const createAgentTerminalTicket = (conversationId: string) => apiClient
  .post<ApiResponse<AgentTerminalTicket>>(`/chat/sessions/${conversationId}/terminal/ticket`)
  .then((response) => response.data.data)
