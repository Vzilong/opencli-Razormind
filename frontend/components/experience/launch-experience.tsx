'use client'

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { AlertCircle, ArrowRight, CheckCircle2, CircleDashed, Clock3, ExternalLink, FileSearch, FolderKanban, Link2, LoaderCircle, Plus, RadioTower, SearchCheck, Waypoints, XCircle } from 'lucide-react'
import Link from 'next/link'
import { useRouter, useSearchParams } from 'next/navigation'
import { useEffect, useMemo, useRef, useState } from 'react'
import { toast } from 'sonner'

import { PageContainer } from '@/components/shell/page-container'
import { useAuth } from '@/components/auth/auth-provider'
import { Badge } from '@/components/ui/badge'
import { Button, buttonVariants } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Textarea } from '@/components/ui/textarea'
import { apiClient } from '@/lib/api/client'
import { useGovernedWorkspaces, useGovernedWorkspaceProjects } from '@/lib/api/hooks'
import type { ApiResponse, ProjectBootstrapResult, ProjectSummary } from '@/lib/api/types'
import ButtonCopy from '@/components/smoothui/button-copy'
import { cn } from '@/lib/utils'
import { studioGraphForTemplate, studioSlug } from '@/lib/workflow/studio-templates'
import { readWorkspacePreference, workspacePreferenceStorageKey, writeWorkspacePreference } from '@/lib/workspace-preference'

type Scenario = 'research' | 'watch' | 'context'
type RunStatus = 'queued' | 'running' | 'completed' | 'partial' | 'failed'

type Readiness = { fetch_ready: boolean; search_ready: boolean; analysis_ready: boolean; missing: string[] }
type ChangeExcerpt = { before: string; after: string; diff: string }
type ResearchSource = { id: string; url: string; title: string; fetched_at: string; content_hash: string; excerpt: string; status: string; watch_status?: 'baseline-established' | 'unchanged' | 'changed' | 'new-source' | 'fetch-failed'; baseline_run_id?: string | null; change_excerpt?: ChangeExcerpt | null }
type WatchChange = { source_id: string; url: string; baseline_run_id: string; before_content_hash: string | null; after_content_hash: string; change_excerpt: ChangeExcerpt }
type ResearchFinding = { text: string; source_ids: string[]; evidence?: Array<{ source_id: string; quote: string }> }
type ResearchResult = { summary: string; findings: ResearchFinding[]; sources: ResearchSource[]; gaps: string[]; changes: WatchChange[] }
type ResearchRun = { id: string; workspace_id: string; project_id: string; template_id: 'research-brief' | 'competitor-watch'; status: RunStatus; created_at: string; updated_at: string; result: ResearchResult | null; error: string | null }
type AgentCapabilities = { records: boolean; context: boolean; research: boolean; mcp_path: string }
type ProjectContext = { query: string; matches: Array<{ id: string; text: string; source: Record<string, unknown> }>; gaps: string[] }
type ScopedRunRequest = { workspaceId: string; projectId: string; scenario: 'research' | 'watch'; question: string; urls: string; requestId: string }
type ScopedContextRequest = { workspaceId: string; projectId: string; query: string }

const SCENARIOS: Array<{ id: Scenario; title: string; detail: string; icon: typeof FileSearch }> = [
  { id: 'research', title: '公开资料研究', detail: '提出问题，获得带引用的研究成果', icon: FileSearch },
  { id: 'watch', title: '竞品变化跟踪', detail: '限定 URL · 先建立基线，再比较变化', icon: RadioTower },
  { id: 'context', title: '项目知识供给', detail: '查询项目中已有的资料与研究成果', icon: SearchCheck },
]

function apiPath(workspaceId: string, projectId: string, suffix: string) {
  return `/workspaces/${encodeURIComponent(workspaceId)}/projects/${encodeURIComponent(projectId)}${suffix}`
}

function splitUrls(value: string) {
  return value.split(/[\n,]/).map((url) => url.trim()).filter(Boolean)
}

function errorText(error: unknown, operation = '执行此操作') {
  if (error && typeof error === 'object' && 'status' in error && error.status === 403) {
    return `当前成员没有${operation}的权限。请联系工作区管理员确认当前项目的授权。`
  }
  return error instanceof Error ? error.message : '请求失败，请稍后重试。'
}

export function LaunchExperience() {
  const queryClient = useQueryClient()
  const router = useRouter()
  const workspaces = useGovernedWorkspaces()
  const { identity } = useAuth()
  const searchParams = useSearchParams()
  const searchParamsKey = searchParams.toString()
  const requestedWorkspaceId = searchParams.get('workspace')
  const preferenceScope = workspacePreferenceStorageKey(identity)
  const previousPreferenceScope = useRef(preferenceScope)
  const pendingWorkspaceSelection = useRef<string | null>(null)
  const [workspaceId, setWorkspaceId] = useState<string | null>(null)
  const projects = useGovernedWorkspaceProjects(workspaceId)
  const [projectId, setProjectId] = useState<string>('')
  const [scenario, setScenario] = useState<Scenario>('research')
  const [question, setQuestion] = useState('')
  const [urls, setUrls] = useState('')
  const [contextQuery, setContextQuery] = useState('')
  const [newProjectName, setNewProjectName] = useState('')
  const [showCreate, setShowCreate] = useState(false)
  const [newWorkspaceName, setNewWorkspaceName] = useState('')
  const [contextResult, setContextResult] = useState<{ scope: string; data: ProjectContext } | null>(null)
  const pendingRequestIds = useRef(new Map<string, string>())

  function selectWorkspace(nextWorkspaceId: string | null) {
    if (nextWorkspaceId) {
      pendingWorkspaceSelection.current = nextWorkspaceId
      writeWorkspacePreference(identity, nextWorkspaceId)
      const params = new URLSearchParams(searchParamsKey)
      params.set('workspace', nextWorkspaceId)
      router.replace(`/launch?${params.toString()}`, { scroll: false })
    }
    setWorkspaceId(nextWorkspaceId)
    setProjectId('')
  }

  useEffect(() => {
    if (previousPreferenceScope.current === preferenceScope) return
    previousPreferenceScope.current = preferenceScope
    pendingWorkspaceSelection.current = null
    setWorkspaceId(null)
    setProjectId('')
  }, [preferenceScope])

  useEffect(() => {
    if (!workspaces.isSuccess) return
    const availableWorkspaces = workspaces.data ?? []
    if (!availableWorkspaces.length) {
      if (workspaceId) setWorkspaceId(null)
      return
    }
    const requestedWorkspace = availableWorkspaces.find((workspace) => workspace.id === requestedWorkspaceId)
    if (pendingWorkspaceSelection.current && requestedWorkspace?.id !== pendingWorkspaceSelection.current) return
    if (requestedWorkspace && requestedWorkspace.id !== workspaceId) {
      pendingWorkspaceSelection.current = null
      setWorkspaceId(requestedWorkspace.id)
      return
    }
    if (workspaceId && availableWorkspaces.some((workspace) => workspace.id === workspaceId)) return
    const preferredWorkspaceId = readWorkspacePreference(identity)
    const nextWorkspace = requestedWorkspace
      ?? availableWorkspaces.find((workspace) => workspace.id === preferredWorkspaceId)
      ?? availableWorkspaces[0]
    setWorkspaceId(nextWorkspace.id)
  }, [identity, requestedWorkspaceId, workspaceId, workspaces.data, workspaces.isSuccess])
  useEffect(() => {
    if (workspaceId && workspaces.data?.some((workspace) => workspace.id === workspaceId)) {
      writeWorkspacePreference(identity, workspaceId)
      if (requestedWorkspaceId !== workspaceId) {
        const params = new URLSearchParams(searchParamsKey)
        params.set('workspace', workspaceId)
        router.replace(`/launch?${params.toString()}`, { scroll: false })
      }
    }
  }, [identity, requestedWorkspaceId, router, searchParamsKey, workspaceId, workspaces.data])
  useEffect(() => {
    if (projects.isSuccess && !projects.data.some((project) => project.id === projectId)) setProjectId(projects.data[0]?.id ?? '')
  }, [projectId, projects.data, projects.isSuccess])
  useEffect(() => {
    setContextResult(null)
  }, [workspaceId, projectId])

  const selectedProject = projects.data?.find((project) => project.id === projectId) ?? null
  const hasProject = Boolean(workspaceId && selectedProject && !workspaces.isError && !projects.isError)
  const readiness = useQuery({
    queryKey: ['launch-readiness', workspaceId, projectId],
    queryFn: async () => (await apiClient.get<ApiResponse<Readiness>>(apiPath(workspaceId as string, projectId, '/research/readiness'))).data.data,
    enabled: hasProject && scenario !== 'context',
    retry: false,
  })
  const capabilities = useQuery({
    queryKey: ['launch-capabilities', workspaceId, projectId],
    queryFn: async () => (await apiClient.get<ApiResponse<AgentCapabilities>>(apiPath(workspaceId as string, projectId, '/agent-data/capabilities'))).data.data,
    enabled: hasProject,
    retry: false,
  })
  const runs = useQuery({
    queryKey: ['launch-research-runs', workspaceId, projectId],
    queryFn: async () => (await apiClient.get<ApiResponse<ResearchRun[]>>(apiPath(workspaceId as string, projectId, '/research/runs'))).data.data,
    enabled: hasProject,
    retry: false,
    refetchInterval: (query) => query.state.data?.some((run) => run.status === 'queued' || run.status === 'running') ? 2_500 : false,
  })
  const createProject = useMutation({
    mutationFn: async ({ workspaceId: targetWorkspaceId, name }: { workspaceId: string; name: string }) => {
      if (!name) throw new Error('请输入项目名称。')
      return (await apiClient.post<ApiResponse<ProjectBootstrapResult>>(`/governance/workspaces/${encodeURIComponent(targetWorkspaceId)}/projects/bootstrap`, {
        project: { name, slug: `${studioSlug(name)}-${Date.now().toString(36)}`, description: '首发研究与资料项目', app_type: 'workflow' },
        workflow: { name: `${name} 工作流`, description: '从首发入口创建的高级编排草稿', graph: studioGraphForTemplate('blank', name) },
      })).data.data
    },
    onSuccess: (data, variables) => {
      queryClient.setQueryData<ProjectSummary[]>(['governance-workspace-projects', variables.workspaceId], (current = []) => [...current.filter((project) => project.id !== data.project.id), data.project])
      void queryClient.invalidateQueries({ queryKey: ['governance-workspace-projects', variables.workspaceId] })
      if (workspaceId === variables.workspaceId) setProjectId(data.project.id)
      setShowCreate(false)
      setNewProjectName('')
      toast.success('项目已创建，现在可以运行研究或查询项目资料。')
    },
  })
  const createWorkspace = useMutation({
    mutationFn: async ({ name, subject }: { name: string; subject: string }) => {
      const slug = `${studioSlug(name)}-${Date.now().toString(36)}`
      return (await apiClient.post<ApiResponse<{ id: string; name: string; slug: string }>>('/platform/workspaces', {
        name,
        slug,
        first_admin_subject: subject,
        first_admin_display_name: identity?.name ?? undefined,
      })).data.data
    },
    onSuccess: (created) => {
      void queryClient.invalidateQueries({ queryKey: ['governance-workspaces'] })
      selectWorkspace(created.id)
      setNewWorkspaceName('')
      toast.success('工作区已创建。接下来创建第一个项目即可开始研究。')
    },
  })
  const startRun = useMutation({
    mutationFn: async (request: ScopedRunRequest) => {
      const seed_urls = splitUrls(request.urls)
      const template_id = request.scenario === 'watch' ? 'competitor-watch' : 'research-brief'
      const payload = { template_id, question: request.question, seed_urls, max_sources: request.scenario === 'watch' ? Math.min(6, Math.max(1, seed_urls.length)) : 5, request_id: request.requestId }
      return (await apiClient.post<ApiResponse<ResearchRun>>(apiPath(request.workspaceId, request.projectId, '/research/runs'), payload)).data.data
    },
    onSuccess: (run, request) => {
      const requestKey = `${request.workspaceId}:${request.projectId}:${request.scenario}:${request.question}:${request.urls}`
      pendingRequestIds.current.delete(requestKey)
      queryClient.setQueryData<ResearchRun[]>(['launch-research-runs', request.workspaceId, request.projectId], (current = []) => [run, ...current.filter((item) => item.id !== run.id)])
      void queryClient.invalidateQueries({ queryKey: ['launch-research-runs', request.workspaceId, request.projectId] })
      toast.success('任务已提交，页面会显示服务端报告的实际状态。')
    },
  })
  const queryContext = useMutation({
    mutationFn: async (request: ScopedContextRequest) => {
      return (await apiClient.get<ApiResponse<ProjectContext>>(apiPath(request.workspaceId, request.projectId, '/agent-data/context'), { params: { q: request.query, limit: 8 } })).data.data
    },
    onSuccess: (data, request) => setContextResult({ scope: `${request.workspaceId}:${request.projectId}`, data }),
  })

  const terminalRuns = useMemo(() => (runs.data ?? []).filter((run) => run.status === 'completed' || run.status === 'partial' || run.status === 'failed'), [runs.data])
  const currentScenario = SCENARIOS.find((item) => item.id === scenario)!

  return <PageContainer eyebrow="Launch · evidence upstream" title="为你的 Agent 准备好数据" description="从一个项目开始：研究公开资料、跟踪限定页面变化，或取用已有项目知识。高级工作流仍可在项目中编辑。" className="max-w-none">
    <section className="grid gap-3 lg:grid-cols-3" aria-label="首发场景">
      {SCENARIOS.map((item) => {
        const Icon = item.icon
        const selected = scenario === item.id
        return <button key={item.id} type="button" onClick={() => setScenario(item.id)} aria-pressed={selected} className={cn('rounded-xl border p-4 text-left transition-colors focus-visible:outline-none focus-visible:ring-3 focus-visible:ring-ring/50', selected ? 'border-primary bg-primary/[0.05]' : 'bg-card hover:bg-muted/50')}>
          <Icon className="size-5 text-primary" aria-hidden />
          <div className="mt-4 font-medium">{item.title}</div>
          <p className="mt-1 text-xs leading-5 text-muted-foreground">{item.detail}</p>
        </button>
      })}
    </section>

    <section className="grid gap-4 xl:grid-cols-[minmax(0,1.3fr)_minmax(20rem,0.7fr)]" aria-label="首发任务设置">
      <Card className="border-primary/20">
        <CardHeader>
          <div className="flex flex-wrap items-center justify-between gap-2"><div><p className="eyebrow-mono">01 · scope</p><CardTitle className="mt-1">选择项目与场景</CardTitle></div><Badge variant="outline">{currentScenario.title}</Badge></div>
          <CardDescription>运行和查询都在当前工作区与项目作用域内进行。</CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="grid gap-3 sm:grid-cols-2">
            <label className="space-y-2 text-sm"><span>工作区</span><Select value={workspaceId ?? ''} onValueChange={(value) => selectWorkspace(value || null)} disabled={workspaces.isLoading || !workspaces.data?.length}><SelectTrigger className="w-full min-h-11"><SelectValue>{workspaces.data?.find((workspace) => workspace.id === workspaceId)?.name ?? '选择工作区'}</SelectValue></SelectTrigger><SelectContent>{(workspaces.data ?? []).map((workspace) => <SelectItem key={workspace.id} value={workspace.id}>{workspace.name}</SelectItem>)}</SelectContent></Select></label>
            <label className="space-y-2 text-sm"><span>项目</span><Select value={projectId} onValueChange={(value) => setProjectId(value || '')} disabled={!workspaceId || projects.isLoading || !projects.data?.length}><SelectTrigger className="w-full min-h-11"><SelectValue>{selectedProject?.name ?? '选择项目'}</SelectValue></SelectTrigger><SelectContent>{(projects.data ?? []).map((project) => <SelectItem key={project.id} value={project.id}>{project.name}</SelectItem>)}</SelectContent></Select></label>
          </div>
          {workspaces.isError ? <div role="alert" className="space-y-2 rounded-lg border border-destructive/30 p-4 text-sm"><p>工作区列表加载失败。{errorText(workspaces.error, '查看工作区')}</p><Button type="button" variant="outline" disabled={workspaces.isFetching} onClick={() => void workspaces.refetch()}>{workspaces.isFetching ? '正在重试…' : '重试工作区列表'}</Button></div> : workspaces.isSuccess && !workspaces.data.length ? <EmptyWorkspaceSetup isPlatformAdmin={Boolean(identity?.is_platform_admin)} name={newWorkspaceName} setName={setNewWorkspaceName} pending={createWorkspace.isPending} onCreate={() => { const name = newWorkspaceName.trim(); if (!name || !identity?.subject) return toast.error('请输入工作区名称。'); void createWorkspace.mutateAsync({ name, subject: identity.subject }).catch((error) => toast.error(errorText(error))) }} /> : null}
          {workspaceId && projects.isError ? <div role="alert" className="space-y-2 rounded-lg border border-destructive/30 p-4 text-sm"><p>项目列表加载失败。{errorText(projects.error, '查看项目')}</p><Button type="button" variant="outline" disabled={projects.isFetching} onClick={() => void projects.refetch()}>{projects.isFetching ? '正在重试…' : '重试项目列表'}</Button></div> : workspaceId && projects.isSuccess && !projects.data.length ? <div className="rounded-lg border border-dashed p-4 text-sm text-muted-foreground">此工作区还没有项目。创建一个项目后，研究成果和资料都将由该项目授权范围管理。</div> : null}
          <div className="flex flex-wrap items-center gap-2"><Button type="button" variant="outline" onClick={() => setShowCreate((value) => !value)} disabled={!workspaceId || workspaces.isError || projects.isError}><Plus className="size-4" />创建项目</Button>{hasProject && selectedProject ? <Link href={`/studio/projects/${selectedProject.id}?workspace=${workspaceId}`} className={cn(buttonVariants({ variant: 'ghost' }))}>打开高级工作流<ArrowRight className="size-4" /></Link> : null}</div>
          {showCreate ? <form className="flex flex-col gap-2 rounded-lg border bg-muted/30 p-3 sm:flex-row" onSubmit={(event) => { event.preventDefault(); const name = newProjectName.trim(); if (!workspaceId || !name) return toast.error('请输入项目名称。'); void createProject.mutateAsync({ workspaceId, name }).catch((error) => toast.error(errorText(error))) }}><Input aria-label="新项目名称" value={newProjectName} onChange={(event) => setNewProjectName(event.target.value)} placeholder="例如：搜索方案评估" disabled={createProject.isPending} /><Button type="submit" disabled={createProject.isPending}>{createProject.isPending ? <LoaderCircle className="size-4 animate-spin" /> : <Plus className="size-4" />}{createProject.isPending ? '正在创建' : '创建并选择'}</Button><p className="basis-full text-xs text-muted-foreground">创建项目需要当前工作区的配置管理权限。</p></form> : null}
        </CardContent>
      </Card>

      <ReadinessCard hasProject={hasProject} scenario={scenario} readiness={readiness} capabilities={capabilities} />
    </section>

    {scenario === 'context' ? <ContextPanel canQuery={Boolean(hasProject && capabilities.data?.context)} query={contextQuery} setQuery={setContextQuery} mutation={queryContext} result={contextResult?.scope === `${workspaceId}:${projectId}` ? contextResult.data : null} onQuery={() => { if (!hasProject || !workspaceId || !projectId) return; const query = contextQuery.trim(); if (!query) return toast.error('请输入要查询的项目资料问题。'); void queryContext.mutateAsync({ workspaceId, projectId, query }).catch((error) => toast.error(errorText(error))) }} /> : <RunPanel hasProject={hasProject} scenario={scenario} question={question} setQuestion={setQuestion} urls={urls} setUrls={setUrls} readiness={readiness.data} readinessError={readiness.isError ? errorText(readiness.error) : null} disabled={!hasProject || startRun.isPending || readiness.isLoading || readiness.isError || (!readiness.data?.analysis_ready && !splitUrls(urls).length) || !readiness.data?.fetch_ready || (scenario === 'research' && !splitUrls(urls).length && !readiness.data?.search_ready)} startRun={startRun} onStart={() => { if (!hasProject || !workspaceId || !projectId) return; const trimmedQuestion = question.trim(); const seedUrls = splitUrls(urls); if (scenario === 'research' && !trimmedQuestion) return toast.error('请输入研究问题。'); if (scenario === 'watch' && !seedUrls.length) return toast.error('请至少输入一个要跟踪的公开 URL。'); if (scenario === 'research' && !seedUrls.length && !readiness.data?.search_ready) return toast.error('未提供参考 URL 时，需要先配置搜索服务。'); const key = `${workspaceId}:${projectId}:${scenario}:${trimmedQuestion}:${urls}`; const requestId = pendingRequestIds.current.get(key) ?? crypto.randomUUID(); pendingRequestIds.current.set(key, requestId); void startRun.mutateAsync({ workspaceId, projectId, scenario, question: trimmedQuestion, urls, requestId }).catch((error) => toast.error(errorText(error, '运行研究任务'))) }} />}

    <section className="grid gap-4 xl:grid-cols-[minmax(0,1.3fr)_minmax(20rem,0.7fr)]" aria-label="任务成果与下游接入">
      <RunResults runs={runs} terminalRuns={terminalRuns} />
      <DownstreamGuide workspaceId={workspaceId} projectId={projectId} capabilities={capabilities.data} />
    </section>
  </PageContainer>
}

function ReadinessCard({ hasProject, scenario, readiness, capabilities }: { hasProject: boolean; scenario: Scenario; readiness: ReturnType<typeof useQuery<Readiness>>; capabilities: ReturnType<typeof useQuery<AgentCapabilities>> }) {
  const checks = scenario === 'context'
    ? [{ label: '项目资料查询', ready: capabilities.data?.context, missing: '项目知识查询接口尚不可用' }, { label: '记录访问', ready: capabilities.data?.records, missing: '项目记录接口尚不可用' }]
    : [{ label: '网页读取', ready: readiness.data?.fetch_ready, missing: '网页读取未就绪' }, { label: '搜索服务', ready: readiness.data?.search_ready, missing: '搜索服务未就绪' }, { label: '分析能力', ready: readiness.data?.analysis_ready, missing: '分析能力未就绪' }]
  const query = scenario === 'context' ? capabilities : readiness
  return <Card>
    <CardHeader><p className="eyebrow-mono">02 · readiness</p><CardTitle className="mt-1">能力状态</CardTitle><CardDescription>查看当前项目可用的能力，以及开始前需要补充的配置。</CardDescription></CardHeader>
    <CardContent className="space-y-3">
      {!hasProject ? <p role="status" className="rounded-md border border-dashed p-4 text-sm text-muted-foreground">先创建或选择一个项目，再检查可用能力。</p> : query.isError ? (
        <div role="alert" className="space-y-3 rounded-md border border-destructive/30 bg-destructive/5 p-3">
          <p className="text-sm text-destructive">暂时无法检查项目能力。请重试；如果仍然失败，请确认你能访问这个项目。</p>
          <Button type="button" variant="outline" size="sm" disabled={query.isFetching} onClick={() => void query.refetch()}>{query.isFetching ? '正在重试…' : '重新检查能力'}</Button>
        </div>
      ) : checks.map((check) => <div key={check.label} className="flex items-start justify-between gap-3 rounded-md border p-3 text-sm"><div><div className="font-medium">{check.label}</div>{check.ready === false ? <div className="mt-1 text-xs text-muted-foreground">{check.missing}</div> : null}</div><span className="inline-flex shrink-0 items-center gap-1.5 text-xs text-muted-foreground">{check.ready ? <CheckCircle2 className="size-4 text-success" aria-hidden /> : <CircleDashed className="size-4" aria-hidden />}{check.ready === undefined ? '检查中' : check.ready ? '已就绪' : '未就绪'}</span></div>)}
      {hasProject && scenario !== 'context' && readiness.data?.missing.length ? <div className="rounded-md bg-muted p-3 text-xs text-muted-foreground">缺少：{readiness.data.missing.join('、')}</div> : null}
      {hasProject && scenario !== 'context' && readiness.data && (!readiness.data.analysis_ready || !readiness.data.search_ready) ? <Link href="/providers" className={cn(buttonVariants({ variant: 'outline', size: 'sm' }), 'w-full')}>检查模型与连接<ArrowRight className="size-3.5" aria-hidden /></Link> : null}
    </CardContent>
  </Card>
}

function EmptyWorkspaceSetup({ isPlatformAdmin, name, setName, pending, onCreate }: { isPlatformAdmin: boolean; name: string; setName: (value: string) => void; pending: boolean; onCreate: () => void }) {
  if (!isPlatformAdmin) return <div className="rounded-lg border border-dashed p-4 text-sm text-muted-foreground">你的身份还没有可访问的工作区。请让平台管理员创建工作区，并把你加入后再继续。</div>
  return <form className="space-y-3 rounded-lg border border-primary/30 bg-primary/[0.03] p-4" onSubmit={(event) => { event.preventDefault(); onCreate() }}><div><p className="font-medium">创建第一个工作区</p><p className="mt-1 text-xs text-muted-foreground">这是首次安装的入口。创建后你会自动成为该工作区管理员，再创建项目开始研究。</p></div><div className="flex flex-col gap-2 sm:flex-row"><Input aria-label="新工作区名称" value={name} onChange={(event) => setName(event.target.value)} placeholder="例如：产品研究团队" disabled={pending} /><Button type="submit" disabled={pending}>{pending ? <LoaderCircle className="size-4 animate-spin" /> : <Plus className="size-4" />}创建工作区</Button></div></form>
}

function RunPanel({ hasProject, scenario, question, setQuestion, urls, setUrls, readiness, readinessError, disabled, startRun, onStart }: { hasProject: boolean; scenario: 'research' | 'watch'; question: string; setQuestion: (value: string) => void; urls: string; setUrls: (value: string) => void; readiness?: Readiness; readinessError: string | null; disabled: boolean; startRun: ReturnType<typeof useMutation<ResearchRun, unknown, ScopedRunRequest>>; onStart: () => void }) {
  const watch = scenario === 'watch'
  const captureOnly = Boolean(readiness?.fetch_ready && !readiness.analysis_ready && splitUrls(urls).length)
  const blockedReason = !hasProject ? '先创建或选择一个项目，即可检查能力并开始研究。' : readinessError ?? (!readiness ? '正在检查当前项目的可用能力…' : !readiness.fetch_ready ? '网页读取能力尚未就绪。' : !readiness.analysis_ready && !splitUrls(urls).length ? '未配置分析模型。提供公开 URL 后仍可仅采集资料。' : !watch && !splitUrls(urls).length && !readiness.search_ready ? '未提供参考 URL 时，需要搜索服务就绪后才能研究。' : null)
  return <Card className="border-primary/20"><CardHeader><p className="eyebrow-mono">03 · run</p><CardTitle className="mt-1">{watch ? '跟踪指定页面' : '提出研究问题'}</CardTitle><CardDescription>{watch ? '首次运行会建立每个 URL 的成功基线；后续运行才可能报告有证据的变化。' : '最多读取 5 个来源。结果会保留来源、发现和未解问题。'}</CardDescription></CardHeader><CardContent><form className="space-y-4" onSubmit={(event) => { event.preventDefault(); onStart() }}>
    <label className="block space-y-2 text-sm"><span>{watch ? '比较说明（可选）' : '研究问题'}</span><Textarea value={question} onChange={(event) => setQuestion(event.target.value)} placeholder={watch ? '例如：关注价格、功能与发布说明的变化' : '例如：比较两个全文搜索方案的部署要求、中文支持和官方限制'} disabled={startRun.isPending} required={!watch} /></label>
    <label className="block space-y-2 text-sm"><span>{watch ? '要跟踪的公开 URL' : '参考 URL（可选）'}</span><Textarea value={urls} onChange={(event) => setUrls(event.target.value)} placeholder={watch ? '每行一个 URL，最多 6 个来源' : '每行一个 URL；留空则由已配置的搜索能力查找来源'} disabled={startRun.isPending} required={watch} /></label>
    {blockedReason ? <div role="status" className="flex gap-2 rounded-lg border border-warning/30 bg-warning/5 p-3 text-xs text-muted-foreground"><AlertCircle className="size-4 shrink-0 text-warning" />{blockedReason}</div> : null}
    {captureOnly ? <p role="status" className="text-sm text-warning">未配置分析模型；本次仅采集资料{watch ? '并比较页面变化' : ''}，结果会保留缺口并标为不完整。</p> : null}
    <Button type="submit" className="min-h-11" disabled={disabled}>{startRun.isPending ? <><LoaderCircle className="size-4 animate-spin" />正在提交</> : <><ArrowRight className="size-4" />{watch ? '建立或比较基线' : captureOnly ? '仅采集资料' : '开始研究'}</>}</Button>
  </form></CardContent></Card>
}

function ContextPanel({ canQuery, query, setQuery, mutation, result, onQuery }: { canQuery: boolean; query: string; setQuery: (value: string) => void; mutation: ReturnType<typeof useMutation<ProjectContext, unknown, ScopedContextRequest>>; result: ProjectContext | null; onQuery: () => void }) {
  return <Card className="border-primary/20"><CardHeader><p className="eyebrow-mono">03 · retrieve</p><CardTitle className="mt-1">查询项目知识</CardTitle><CardDescription>只检索当前项目被授权的资料与成果；查询不会重新调用分析模型。</CardDescription></CardHeader><CardContent className="space-y-4"><form className="flex flex-col gap-2 sm:flex-row" onSubmit={(event) => { event.preventDefault(); onQuery() }}><Input aria-label="项目资料问题" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="例如：当前项目有哪些接口鉴权资料？" disabled={mutation.isPending} /><Button type="submit" disabled={!canQuery || mutation.isPending}>{mutation.isPending ? <LoaderCircle className="size-4 animate-spin" /> : <SearchCheck className="size-4" />}查询资料</Button></form>{!canQuery ? <p className="text-xs text-muted-foreground">选择项目并确认“项目资料查询”已就绪后，才能查询。</p> : null}{result ? <div className="space-y-3 rounded-lg border p-4"><div className="text-sm font-medium">{result.query}</div>{result.matches.length ? result.matches.map((match) => <div key={match.id} className="rounded-md bg-muted/50 p-3 text-sm"><p>{match.text}</p><p className="mt-2 break-all font-mono text-xs text-muted-foreground">来源：{sourceLabel(match.source)}</p></div>) : <p className="text-sm text-muted-foreground">没有找到匹配资料。</p>}{result.gaps.length ? <div className="text-xs text-warning">资料缺口：{result.gaps.join('、')}</div> : null}</div> : null}</CardContent></Card>
}

function RunResults({ runs, terminalRuns }: { runs: ReturnType<typeof useQuery<ResearchRun[]>>; terminalRuns: ResearchRun[] }) {
  if (runs.isError) return <Card><CardHeader><CardTitle>成果与任务状态</CardTitle></CardHeader><CardContent><p role="alert" className="text-sm text-destructive">无法读取任务列表：{errorText(runs.error)}</p></CardContent></Card>
  const displayed = runs.data?.slice(0, 5) ?? []
  return <Card><CardHeader><div className="flex items-center justify-between gap-3"><div><p className="eyebrow-mono">Results · citations</p><CardTitle className="mt-1">成果与任务状态</CardTitle></div>{runs.isFetching ? <LoaderCircle className="size-4 animate-spin text-muted-foreground" /> : null}</div><CardDescription>任务进展会自动更新。完成后可在这里阅读成果、查看引用和待补充的资料。</CardDescription></CardHeader><CardContent className="space-y-3">{!displayed.length && !runs.isLoading ? <div className="rounded-lg border border-dashed p-4 text-sm text-muted-foreground">还没有研究任务。提交后，进展和成果会保留在这里。</div> : displayed.map((run) => <RunCard key={run.id} run={run} />)}{terminalRuns.length > 5 ? <p className="text-xs text-muted-foreground">显示最近 5 个任务。</p> : null}</CardContent></Card>
}

function RunCard({ run }: { run: ResearchRun }) {
  const state = run.status === 'completed' ? { label: '已完成', icon: CheckCircle2, className: 'text-success' } : run.status === 'partial' ? { label: '不完整', icon: AlertCircle, className: 'text-warning' } : run.status === 'failed' ? { label: '失败', icon: XCircle, className: 'text-destructive' } : run.status === 'running' ? { label: '运行中', icon: LoaderCircle, className: 'text-primary' } : { label: '已排队', icon: Clock3, className: 'text-muted-foreground' }
  const Icon = state.icon
  return <article className="rounded-lg border p-4"><div className="flex flex-wrap items-center justify-between gap-2"><div className="flex items-center gap-2 text-sm font-medium"><Icon className={cn('size-4', state.className, run.status === 'running' && 'animate-spin')} />{run.template_id === 'research-brief' ? '公开资料研究' : '竞品变化跟踪'}</div><Badge variant="outline" className={state.className}>{state.label}</Badge></div><p className="mt-2 font-mono text-xs text-muted-foreground">{run.id} · {new Date(run.updated_at).toLocaleString()}</p>{run.error ? <p className="mt-3 rounded-md bg-destructive/5 p-3 text-sm text-destructive">{run.error}</p> : null}{run.result ? <div className="mt-3 space-y-3 text-sm"><p>{run.result.summary || '服务端没有返回研究摘要。'}</p>{run.template_id === 'competitor-watch' ? <WatchChanges changes={run.result.changes} sources={run.result.sources} /> : null}{run.result.findings.length ? <div className="space-y-2">{run.result.findings.map((finding, index) => <div key={`${finding.text}-${index}`} className="rounded-md bg-muted/50 p-3"><p>{finding.text}</p><FindingEvidence finding={finding} sources={run.result!.sources} /></div>)}</div> : null}{run.result.sources.length ? <div className="space-y-2"><div className="text-xs font-medium text-muted-foreground">来源</div>{run.result.sources.map((source) => <a key={source.id} href={source.url} target="_blank" rel="noreferrer" className="flex items-start gap-2 rounded-md border p-3 text-xs hover:bg-muted"><ExternalLink className="mt-0.5 size-3.5 shrink-0" /><span><span className="font-medium">{source.title || source.url}</span><span className="mt-1 block break-all text-muted-foreground">{source.url} · {source.status}</span>{source.excerpt ? <span className="mt-1 block text-muted-foreground">{source.excerpt}</span> : null}</span></a>)}</div> : null}{run.result.gaps.length ? <p className="rounded-md bg-warning/5 p-3 text-xs text-warning">未解决：{run.result.gaps.join('、')}</p> : null}</div> : null}</article>
}

function FindingEvidence({ finding, sources }: { finding: ResearchFinding; sources: ResearchSource[] }) {
  return <div className="mt-2 space-y-2 text-xs text-muted-foreground">
    <p>引用：{finding.source_ids.map((id, index) => {
      const source = sources.find((item) => item.id === id)
      return <span key={id}>{index ? '、' : ''}{source ? <a href={source.url} target="_blank" rel="noreferrer" className="underline">{source.title || id}</a> : id}</span>
    })}{!finding.source_ids.length ? '未提供来源 ID' : null}</p>
    {finding.evidence?.map((item, index) => <blockquote key={`${item.source_id}:${index}`} className="border-l-2 pl-3 whitespace-pre-wrap"><p>{item.quote}</p><footer className="mt-1">原文来源：{item.source_id}</footer></blockquote>)}
  </div>
}

function WatchChanges({ changes, sources }: { changes: WatchChange[]; sources: ResearchSource[] }) {
  const watched = sources.filter((source) => source.watch_status)
  if (!changes.length && !watched.length) return <p className="rounded-md bg-muted p-3 text-xs text-muted-foreground">本次没有可确认的页面变化；首次成功读取只建立基线，读取失败不会替换已有基线。</p>
  return <div className="space-y-2 rounded-md border border-primary/20 p-3"><p className="text-xs font-medium">页面变化与基线状态</p>{watched.map((source) => <div key={source.id} className="text-xs"><span className="font-medium">{source.url}</span><span className="block text-muted-foreground">{watchStatusLabel(source.watch_status!)}{source.baseline_run_id ? ` · 基线任务：${source.baseline_run_id}` : ''}</span>{source.change_excerpt ? <ChangeExcerptView excerpt={source.change_excerpt} /> : null}</div>)}{changes.filter((change) => !watched.some((source) => source.id === change.source_id && source.change_excerpt)).map((change) => <div key={`${change.source_id}:${change.after_content_hash}`} className="text-xs"><span className="font-medium">{change.url}</span><span className="block text-muted-foreground">相对成功基线已变化 · 基线任务：{change.baseline_run_id}</span><ChangeExcerptView excerpt={change.change_excerpt} /></div>)}</div>
}

function ChangeExcerptView({ excerpt }: { excerpt: ChangeExcerpt }) {
  return <div className="mt-1 space-y-1 whitespace-pre-wrap rounded bg-muted/60 p-2 text-muted-foreground"><span className="block">变更摘要：{excerpt.diff}</span><span className="block">变更前：{excerpt.before}</span><span className="block">变更后：{excerpt.after}</span></div>
}

function watchStatusLabel(status: NonNullable<ResearchSource['watch_status']>) {
  return ({ 'baseline-established': '已建立成功基线', unchanged: '与成功基线一致', changed: '相对成功基线已变化', 'new-source': '新来源，待建立基线', 'fetch-failed': '本次读取失败，保留原成功基线' })[status]
}

function DownstreamGuide({ workspaceId, projectId, capabilities }: { workspaceId: string | null; projectId: string; capabilities?: AgentCapabilities }) {
  const path = workspaceId && projectId ? `/api/v1/workspaces/${workspaceId}/projects/${projectId}/agent-data/context?q=${encodeURIComponent('你的问题')}` : null
  const projectsHref = workspaceId ? `/studio?workspace=${encodeURIComponent(workspaceId)}` : '/studio'
  const mcpPath = capabilities?.mcp_path ?? '/mcp'
  const codexConfig = `[mcp_servers.opencli]\ncommand = "opencli-mcp"\nenv_vars = ["OPENCLI_ADMIN_API_URL", "API_AUTH_TOKEN", "OPENCLI_MCP_CALLER_TOKEN"]`
  const claudeConfig = JSON.stringify({ mcpServers: { opencli: { command: 'opencli-mcp', env: { OPENCLI_ADMIN_API_URL: '${OPENCLI_ADMIN_API_URL}', API_AUTH_TOKEN: '${API_AUTH_TOKEN}', OPENCLI_MCP_CALLER_TOKEN: '${OPENCLI_MCP_CALLER_TOKEN}' } } } }, null, 2)
  return <Card className="min-w-0">
    <CardHeader><p className="eyebrow-mono">04 · handoff</p><CardTitle className="mt-1">连接你的 Agent</CardTitle><CardDescription>把项目成果和引用交给 Codex 或 Claude Code，继续分析和使用。</CardDescription></CardHeader>
    <CardContent className="space-y-4">
      <details className="group rounded-md border p-3">
        <summary className="cursor-pointer text-sm font-medium focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">查看 Agent 接入配置</summary>
        <div className="mt-4 min-w-0 space-y-4">
          <div className="rounded-md border bg-muted/40 p-3 text-xs"><div className="flex items-center gap-2"><Waypoints className="size-4 shrink-0 text-primary" aria-hidden /><span className="break-all">HTTP MCP：{mcpPath}</span></div><p className="mt-2 text-muted-foreground">使用内置 opencli-mcp 客户端。配置只引用环境变量；调用方凭据必须拥有当前项目的授权，Fleet 传输令牌不能代替项目成员身份。</p></div>
          <div className="rounded-md border p-3"><div className="flex items-center gap-2 text-sm font-medium"><Link2 className="size-4" aria-hidden />项目上下文 API</div><code className="mt-2 block break-all text-xs text-muted-foreground">GET {path ?? '先选择工作区和项目'}</code></div>
          <ConfigSnippet title="Codex MCP 配置" text={codexConfig} />
          <ConfigSnippet title="Claude Code MCP 配置" text={claudeConfig} />
          <p className="text-xs leading-5 text-muted-foreground">可调用范围：query_project_records、query_project_context、get_project_data_capabilities、get_project_research_readiness、start_project_research、list_project_research_runs、get_project_research_run。先查询项目资料，列出来源与缺口，再基于证据完成工作。</p>
        </div>
      </details>
      <Link href={projectsHref} className={cn(buttonVariants({ variant: 'outline' }), 'w-full')}><FolderKanban className="size-4" />查看所有项目</Link>
    </CardContent>
  </Card>
}

function ConfigSnippet({ title, text }: { title: string; text: string }) {
  return <div className="rounded-lg border p-3"><div className="flex items-center justify-between gap-2"><p className="text-sm font-medium">{title}</p><ButtonCopy text={text} label={`复制${title}`} /></div><code className="mt-2 block max-h-36 overflow-auto whitespace-pre text-xs text-muted-foreground">{text}</code></div>
}

function sourceLabel(source: Record<string, unknown>) {
  return typeof source.title === 'string' ? source.title : typeof source.url === 'string' ? source.url : typeof source.id === 'string' ? source.id : '已授权项目资料'
}
