'use client'

import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { BookOpen, FileText, Folder, Search, Sparkles, Upload } from 'lucide-react'
import { toast } from 'sonner'
import { PageContainer } from '@/components/shell/page-container'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { selectClass } from '@/components/knowledge/brand-scope'
import { KnowledgeScopeFields, useKnowledgeScope } from '@/components/knowledge/knowledge-scope'
import { apiClient } from '@/lib/api/client'
import { knowledgeGet, knowledgePost, libraryPagesRoot, libraryParams, libraryRoot, useKnowledgeLibraries, useProjectKnowledgeLibraries, type KnowledgeScope, type KnowledgeLibrary, type KnowledgePage, type Citation, type KnowledgeAnswer } from '@/lib/api/knowledge-libraries'

const statusLabels = { draft: '草稿', published: '已发布', archived: '已归档' }

function Citations({ hits, onOpen }: { hits: Citation[]; onOpen: (id: string) => void }) {
  return <div className="space-y-3">{hits.map((hit, index) => <button key={`${hit.page_id}:${index}`} onClick={() => onOpen(hit.page_id)} className="block w-full rounded-lg border p-3 text-left text-sm hover:bg-muted/40">
    <span className="font-medium">[{hit.number || index + 1}] {hit.title}</span>
    <span className="mt-1 block text-xs text-muted-foreground">引用版本 v{hit.revision} · {hit.product_name || (hit.product_id ? '产品专属' : '资料库通用')} · 点击查看页面与历史版本</span>
    <span className="mt-2 line-clamp-4 block whitespace-pre-wrap text-xs leading-5">{hit.excerpt}</span>
  </button>)}</div>
}

function PageEditor({ page, root, onSaved, onDirtyChange, locked }: { page: KnowledgePage; root: string; onSaved: (page: KnowledgePage) => void; onDirtyChange: (dirty: boolean) => void; locked: boolean }) {
  const [title, setTitle] = useState(page.title)
  const [content, setContent] = useState(page.content || '')
  const dirty = title !== page.title || content !== (page.content || '')
  useEffect(() => { onDirtyChange(dirty); return () => onDirtyChange(false) }, [dirty, onDirtyChange])
  useEffect(() => {
    if (!dirty) return
    const unload = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = '' }
    const navigate = (event: MouseEvent) => {
      if (!(event.target instanceof Element) || !event.target.closest('a[href]')) return
      if (!window.confirm('当前页面有未保存的修改，确定离开并放弃修改吗？')) { event.preventDefault(); event.stopPropagation() }
    }
    window.addEventListener('beforeunload', unload)
    document.addEventListener('click', navigate, true)
    return () => { window.removeEventListener('beforeunload', unload); document.removeEventListener('click', navigate, true) }
  }, [dirty])
  const [historyOpen, setHistoryOpen] = useState(false)
  const history = useQuery({ queryKey: ['knowledge-history', root, page.id, page.revision], enabled: historyOpen,
    queryFn: () => knowledgeGet<{ revision: number; content: string; status: keyof typeof statusLabels; created_at: string }[]>(`${root}/pages/${page.id}/revisions`) })
  const save = useMutation({ mutationFn: (status: KnowledgePage['status']) => apiClient.patch(`${root}/pages/${page.id}`, { title, content, status, revision: page.revision }).then(result => result.data.data as KnowledgePage),
    onSuccess: result => { toast.success('页面已保存'); onSaved(result) }, onError: (error: Error) => toast.error(error.message) })
  const summarize = useMutation({ mutationFn: () => knowledgePost<KnowledgePage>(`${root}/pages/${page.id}/summarize`, {}),
    onSuccess: result => { toast.success('整理草稿已生成，请核实后发布'); onSaved(result) }, onError: (error: Error) => toast.error(error.message) })
  const download = useMutation({ mutationFn: async () => {
    const response = await apiClient.get(`${root}/pages/${page.id}/download`, { responseType: 'blob' })
    const url = URL.createObjectURL(response.data)
    const anchor = document.createElement('a'); anchor.href = url; anchor.download = page.original_name || page.title; anchor.click()
    setTimeout(() => URL.revokeObjectURL(url), 1000)
  }, onError: (error: Error) => toast.error(error.message) })
  const busy = save.isPending || summarize.isPending || locked
  return <div className="space-y-4">
    <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-muted-foreground"><span>{page.kind === 'source' ? '原始资料' : page.kind === 'folder' ? '目录' : '整理页面'} · {statusLabels[page.status]} · v{page.revision}</span><span>{page.product_id ? '产品专属' : '资料库通用'}</span></div>
    <Input aria-label="页面标题" value={title} disabled={busy} readOnly={page.kind === 'source'} maxLength={255} onChange={event => setTitle(event.target.value)} />
    {page.kind !== 'folder' && <Textarea aria-label="页面正文" disabled={busy} className="min-h-80 font-mono text-sm leading-6" value={content} readOnly={page.kind === 'source'} onChange={event => setContent(event.target.value)} />}
    {dirty && <p role="status" className="text-xs text-amber-700">有未保存的修改</p>}
    {page.kind === 'source' && <p className="text-xs text-muted-foreground">原始资料保留原文。可生成整理草稿，经核实发布后参与检索。</p>}
    <div className="flex flex-wrap gap-2">
      {page.kind === 'page' && <><Button disabled={busy || !title.trim()} onClick={() => save.mutate('draft')}>保存草稿</Button><Button variant="outline" disabled={busy || !content.trim() || !title.trim()} onClick={() => save.mutate('published')}>核实并发布</Button></>}
      {page.kind === 'folder' && <Button disabled={busy || !title.trim()} onClick={() => save.mutate('published')}>保存目录</Button>}
      {page.kind !== 'folder' && page.status === 'published' && <Button variant="outline" disabled={busy} onClick={() => summarize.mutate()}><Sparkles className="size-4" />{summarize.isPending ? '正在整理…' : 'AI 整理为草稿'}</Button>}
      {page.original_name && <Button variant="outline" disabled={download.isPending} onClick={() => download.mutate()}>下载原文件</Button>}
      <Button variant="ghost" disabled={busy} onClick={() => save.mutate(page.status === 'archived' ? 'draft' : 'archived')}>{page.status === 'archived' ? '恢复为草稿' : '归档'}</Button>
      {page.kind === 'source' && page.status === 'draft' && <Button disabled={busy} onClick={() => save.mutate('published')}>重新发布资料</Button>}
      <Button variant="ghost" onClick={() => setHistoryOpen(!historyOpen)}>历史版本</Button>
    </div>
    {(save.error || summarize.error || download.error) && <p role="alert" className="text-sm text-destructive">{(save.error || summarize.error || download.error)?.message}</p>}
    {page.source_refs.length > 0 && <section className="space-y-2"><h3 className="text-sm font-medium">整理依据</h3>{page.source_refs.map((ref, index) => <p key={index} className="text-xs text-muted-foreground">[{ref.number || index + 1}] {ref.title} · v{ref.revision}</p>)}</section>}
    {historyOpen && <section className="space-y-2 rounded-lg border p-3"><h3 className="text-sm font-medium">最近 50 个版本</h3>{history.isPending && <p role="status">正在加载…</p>}{history.error && <p role="alert">{history.error.message}</p>}{history.data?.map(item => <details key={item.revision} className="text-sm"><summary className="cursor-pointer">v{item.revision} · {statusLabels[item.status]} · {new Date(item.created_at).toLocaleString()}</summary><pre className="mt-2 max-h-64 overflow-auto whitespace-pre-wrap text-xs">{item.content}</pre></details>)}</section>}
  </div>
}

function KnowledgeLibrary({ scope, onBeforeLeave, onDirtyChange }: { scope: KnowledgeScope; onBeforeLeave: () => boolean; onDirtyChange: (dirty: boolean) => void }) {
  const client = useQueryClient()
  const root = libraryPagesRoot(scope)
  const [selectedId, setSelectedId] = useState('')
  const openPage = (id: string) => { if (id !== selectedId && onBeforeLeave()) setSelectedId(id) }
  const [parentId, setParentId] = useState('')
  const [title, setTitle] = useState('')
  const [kind, setKind] = useState<'page' | 'folder'>('page')
  const [query, setQuery] = useState('')
  const [showArchived, setShowArchived] = useState(false)
  const pages = useQuery({ queryKey: ['knowledge-pages', root, scope.productId], queryFn: () => knowledgeGet<KnowledgePage[]>(`${root}/pages`, libraryParams(scope)) })
  const selected = useQuery({ queryKey: ['knowledge-page', root, selectedId], enabled: !!selectedId, refetchOnWindowFocus: false, refetchOnReconnect: false,
    queryFn: () => knowledgeGet<KnowledgePage>(`${root}/pages/${selectedId}`) })
  const refresh = (page: KnowledgePage) => {
    client.setQueryData(['knowledge-page', root, page.id], page)
    setSelectedId(page.id)
    void client.invalidateQueries({ queryKey: ['knowledge-pages', root] })
  }
  const create = useMutation({ mutationFn: () => knowledgePost<KnowledgePage>(`${root}/pages`, {
    title, kind, product_id: scope.productId || null, parent_id: parentId || null,
  }), onSuccess: page => { setTitle(''); refresh(page) }, onError: (error: Error) => toast.error(error.message) })
  const upload = useMutation({ mutationFn: async (file: File) => {
    const form = new FormData(); form.append('file', file)
    return apiClient.post(`${root}/upload`, form, { headers: { 'Content-Type': 'multipart/form-data' }, params: { ...libraryParams(scope), ...(parentId ? { parent_id: parentId } : {}) } }).then(result => result.data.data as KnowledgePage)
  }, onSuccess: page => { toast.success(page.reused ? '相同资料已存在，已打开原页面' : '资料已上传，可搜索和问答'); refresh(page) }, onError: (error: Error) => toast.error(error.message) })
  const search = useMutation({ mutationFn: (question: string) => knowledgeGet<Citation[]>(`${root}/search`, { q: question, ...libraryParams(scope) }) })
  const ask = useMutation({ mutationFn: (question: string) => knowledgePost<KnowledgeAnswer>(`${root}/ask`, { question, product_id: scope.productId || null }) })
  const folders = pages.data?.filter(page => page.kind === 'folder' && page.status !== 'archived' && page.product_id === (scope.productId || null)) || []
  const visiblePages = (pages.data || []).filter(page => showArchived || page.status !== 'archived')
  const tree: { page: KnowledgePage; depth: number }[] = []
  const seen = new Set<string>()
  const append = (parent: string | null, depth: number) => {
    for (const page of visiblePages.filter(item => item.parent_id === parent)) {
      if (seen.has(page.id)) continue
      seen.add(page.id); tree.push({ page, depth }); append(page.id, depth + 1)
    }
  }
  append(null, 0)
  for (const page of visiblePages) if (!seen.has(page.id)) tree.push({ page, depth: 0 })
  return <>
    <div className="rounded-lg bg-muted/40 px-4 py-3 text-sm text-muted-foreground">{scope.productId ? '检索范围：当前项目获准的产品限制资料与通用资料。' : '检索范围：当前资料库的通用资料。'} 草稿与归档内容不参与问答。</div>
    <div className="grid items-start gap-4 xl:grid-cols-[16rem_minmax(0,1fr)_22rem]">
      <aside className="space-y-4 rounded-xl border bg-card p-4" aria-label="知识目录">
        <div className="flex items-center gap-2 font-medium"><BookOpen className="size-4" />知识目录</div>
        <label className="flex items-center gap-2 text-xs"><input type="checkbox" checked={showArchived} onChange={event => setShowArchived(event.target.checked)} />显示已归档</label>
        {pages.isPending && <p role="status" className="text-sm">正在加载目录…</p>}
        {pages.error && <p role="alert" className="text-sm">{pages.error.message}<button className="ml-2 underline" onClick={() => void pages.refetch()}>重试</button></p>}
        <nav className="max-h-96 space-y-1 overflow-auto">{tree.map(({ page, depth }) => <button key={page.id} aria-current={selectedId === page.id ? 'page' : undefined} className={`flex w-full items-start gap-2 rounded-lg px-2 py-2 text-left text-sm ${selectedId === page.id ? 'bg-primary/10 text-primary' : 'hover:bg-muted'}`} style={{ paddingLeft: 8 + Math.min(depth, 6) * 12 }} onClick={() => openPage(page.id)}>
          {page.kind === 'folder' ? <Folder className="mt-0.5 size-4 shrink-0" /> : <FileText className="mt-0.5 size-4 shrink-0" />}<span className="min-w-0 break-words">{page.title}<span className="block text-xs text-muted-foreground">{page.product_id ? '产品' : '通用'} · {statusLabels[page.status]}</span></span>
        </button>)}</nav>
        {pages.data?.length === 0 && <p className="text-sm text-muted-foreground">还没有资料，上传文件或新建页面开始。</p>}
        <div className="space-y-2 border-t pt-3">
          <select className={`${selectClass} w-full`} aria-label="目标目录" value={parentId} onChange={event => setParentId(event.target.value)}><option value="">根目录</option>{folders.map(folder => <option key={folder.id} value={folder.id}>{folder.title}</option>)}</select>
          <label className={`flex cursor-pointer items-center justify-center gap-2 rounded-lg border p-2 text-sm ${upload.isPending ? 'opacity-50' : ''}`}><Upload className="size-4" />{upload.isPending ? '正在上传…' : '上传资料'}<input type="file" className="sr-only" aria-label="上传知识资料" accept=".txt,.md,.csv,.docx" disabled={upload.isPending} onChange={event => { const file = event.target.files?.[0]; if (file && onBeforeLeave()) upload.mutate(file); event.target.value = '' }} /></label>
          <p className="text-xs leading-5 text-muted-foreground">TXT / Markdown / CSV / DOCX，最大 2 MB、10 万字。文本使用 UTF-8；PDF 需先转为文本。</p>
          <form className="space-y-2" onSubmit={event => { event.preventDefault(); if (onBeforeLeave()) create.mutate() }}>
            <Input aria-label="新页面名称" placeholder="页面或目录名称" value={title} maxLength={255} onChange={event => setTitle(event.target.value)} />
            <div className="flex gap-2"><select aria-label="新建类型" className={`${selectClass} flex-1`} value={kind} onChange={event => setKind(event.target.value as 'page' | 'folder')}><option value="page">整理页面</option><option value="folder">目录</option></select><Button type="submit" disabled={!title.trim() || create.isPending}>新建</Button></div>
          </form>
          {(upload.error || create.error) && <p role="alert" className="text-sm text-destructive">{(upload.error || create.error)?.message}</p>}
        </div>
      </aside>
      <section className="min-w-0 rounded-xl border bg-card p-5" aria-label="知识正文">
        {!selectedId ? <div className="py-16 text-center"><BookOpen className="mx-auto mb-3 size-8 text-muted-foreground" /><h2 className="font-medium">让资料成为可查询的知识</h2><p className="mt-2 text-sm text-muted-foreground">从左侧选择资料，阅读原文或整理知识页面。</p></div> : selected.isPending ? <p role="status">正在加载正文…</p> : selected.error ? <p role="alert">{selected.error.message}</p> : selected.data ? <PageEditor key={`${selected.data.id}:${selected.data.revision}`} page={selected.data} root={root} onSaved={refresh} onDirtyChange={onDirtyChange} locked={create.isPending || upload.isPending} /> : null}
      </section>
      <aside className="space-y-4 rounded-xl border bg-card p-4" aria-label="知识查询">
        <h2 className="font-medium">搜索与知识问答</h2>
        <Textarea aria-label="查询问题" placeholder="输入关键词，或询问资料中的背景、结论与依据…" maxLength={1000} value={query} onChange={event => setQuery(event.target.value)} />
        <div className="flex flex-wrap gap-2"><Button variant="outline" disabled={!query.trim() || search.isPending || ask.isPending} onClick={() => { ask.reset(); search.mutate(query) }}><Search className="size-4" />搜索</Button><Button disabled={!query.trim() || ask.isPending || search.isPending} onClick={() => { search.reset(); ask.mutate(query) }}><Sparkles className="size-4" />{ask.isPending ? '正在回答…' : 'AI 问答'}</Button></div>
        <p className="text-xs text-muted-foreground">AI 使用“模型与连接”中的 chat 默认模型。回答附引用资料与版本，需结合原文核实。</p>
        {(search.error || ask.error) && <p role="alert" className="text-sm text-destructive">{(search.error || ask.error)?.message}</p>}
        {search.isPending && <p role="status" className="text-sm">正在检索…</p>}
        {search.data && <><p className="text-xs text-muted-foreground">“{search.variables}” · {search.data.length ? `找到 ${search.data.length} 条相关资料` : '没有找到相关资料，请调整关键词。'}</p><Citations hits={search.data} onOpen={openPage} /></>}
        {ask.data && <><p className="text-xs text-muted-foreground">问题：{ask.variables}</p><div className="whitespace-pre-wrap text-sm leading-6">{ask.data.answer}</div><Citations hits={ask.data.citations} onOpen={openPage} /></>}
      </aside>
    </div>
  </>
}

function LibrarySetup({ workspaceId, onCreated }: { workspaceId: string; onCreated: (library: KnowledgeLibrary) => void }) {
  const client = useQueryClient()
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const create = useMutation({ mutationFn: () => knowledgePost<KnowledgeLibrary>(libraryRoot(workspaceId), { name: name.trim(), description: description.trim() }), onSuccess: library => { void client.invalidateQueries({ queryKey: ['knowledge-libraries', workspaceId] }); setName(''); setDescription(''); onCreated(library); toast.success('资料库已创建') }, onError: (error: Error) => toast.error(error.message) })
  return <form className="flex flex-wrap gap-2 rounded-xl border p-4" onSubmit={event => { event.preventDefault(); if (name.trim()) create.mutate() }}><Input aria-label="新资料库名称" className="min-w-52 flex-1" placeholder="例如：产品研究资料" value={name} onChange={event => setName(event.target.value)} maxLength={120} /><Input aria-label="资料库说明" className="min-w-52 flex-1" placeholder="可选说明" value={description} onChange={event => setDescription(event.target.value)} maxLength={1000} /><Button type="submit" disabled={!name.trim() || create.isPending}>{create.isPending ? '正在创建…' : '创建资料库'}</Button></form>
}

function ProjectBindingPanel({ scope }: { scope: KnowledgeScope }) {
  const client = useQueryClient()
  const bindings = useProjectKnowledgeLibraries(scope.workspaceId, scope.projectId)
  const selected = bindings.data?.find(item => item.id === scope.libraryId)
  const bind = useMutation({ mutationFn: () => apiClient.put(`/workspaces/${encodeURIComponent(scope.workspaceId)}/projects/${encodeURIComponent(scope.projectId)}/knowledge-libraries/${encodeURIComponent(scope.libraryId)}`, { product_id: null }).then(result => result.data.data), onSuccess: () => { void client.invalidateQueries({ queryKey: ['project-knowledge-libraries', scope.workspaceId, scope.projectId] }); toast.success('资料库已绑定到项目') }, onError: (error: Error) => toast.error(error.message) })
  const unbind = useMutation({ mutationFn: () => apiClient.delete(`/workspaces/${encodeURIComponent(scope.workspaceId)}/projects/${encodeURIComponent(scope.projectId)}/knowledge-libraries/${encodeURIComponent(scope.libraryId)}`), onSuccess: () => { void client.invalidateQueries({ queryKey: ['project-knowledge-libraries', scope.workspaceId, scope.projectId] }); toast.success('已解除项目绑定') }, onError: (error: Error) => toast.error(error.message) })
  if (!scope.projectId || !scope.libraryId) return null
  return <div className="flex flex-wrap items-center gap-3 rounded-lg border bg-muted/30 px-4 py-3 text-sm"><span>{selected ? selected.product_id ? '项目绑定为产品限定访问；当前资料库页面仍按工作区范围管理，运行时检索会执行此限制。' : '项目已绑定整座资料库；当前页面仍按工作区范围管理。' : '此资料库尚未绑定到当前项目。'}</span>{selected ? <Button variant="outline" size="sm" disabled={unbind.isPending} onClick={() => unbind.mutate()}>解除绑定</Button> : <Button size="sm" disabled={bind.isPending} onClick={() => bind.mutate()}>绑定到项目</Button>}</div>
}

export default function KnowledgePageView() {
  const { scope, setScope } = useKnowledgeScope()
  const libraries = useKnowledgeLibraries(scope.workspaceId)
  const [dirty, setDirty] = useState(false)
  const canLeave = () => !dirty || window.confirm('当前页面有未保存的修改，确定离开并放弃修改吗？')
  useEffect(() => { if (!scope.libraryId && scope.legacyBrandId && libraries.data) { const legacy = libraries.data.find(item => item.legacy_brand_id === scope.legacyBrandId); if (legacy) setScope(current => ({ ...current, libraryId: legacy.id })) } }, [libraries.data, scope.libraryId, scope.legacyBrandId, setScope])
  return <PageContainer title="知识库" description="保存原始资料，整理可复用知识，以出处支撑每一次查询。" className="max-w-none">
    <KnowledgeScopeFields scope={scope} onChange={value => { if (canLeave()) setScope(value) }} />
    {scope.workspaceId ? <LibrarySetup workspaceId={scope.workspaceId} onCreated={library => { if (canLeave()) setScope({ ...scope, libraryId: library.id }) }} /> : null}
    <ProjectBindingPanel scope={scope} />
    {scope.workspaceId && scope.libraryId ? <KnowledgeLibrary key={`${scope.workspaceId}:${scope.libraryId}:${scope.projectId}:${scope.productId}`} scope={scope} onBeforeLeave={canLeave} onDirtyChange={setDirty} /> : <div className="rounded-xl border p-10 text-center text-sm text-muted-foreground">选择工作区和资料库以打开知识库；没有资料库时可在上方创建。</div>}
  </PageContainer>
}
