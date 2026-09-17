'use client'

import { useEffect, useState } from 'react'

import { useGovernedWorkspaces, useGovernedWorkspaceProjects } from '@/lib/api/hooks'
import { useKnowledgeLibraries, type KnowledgeScope } from '@/lib/api/knowledge-libraries'
import { selectClass } from './brand-scope'

export function useKnowledgeScope() {
  const [scope, setScope] = useState<KnowledgeScope>({ workspaceId: '', libraryId: '', projectId: '', productId: '' })
  useEffect(() => { const params = new URLSearchParams(window.location.search); setScope({ workspaceId: params.get('workspace') || '', libraryId: params.get('library') || '', projectId: params.get('project') || '', productId: params.get('product') || '', legacyBrandId: params.get('brand') || undefined }) }, [])
  return { scope, setScope }
}

export function KnowledgeScopeFields({ scope, onChange }: { scope: KnowledgeScope; onChange: (scope: KnowledgeScope) => void }) {
  const workspaces = useGovernedWorkspaces()
  const libraries = useKnowledgeLibraries(scope.workspaceId)
  const projects = useGovernedWorkspaceProjects(scope.workspaceId)
  const error = workspaces.error || libraries.error || projects.error
  return <div className="space-y-2"><div className="flex flex-wrap gap-3"><select className={selectClass} aria-label="工作区" value={scope.workspaceId} onChange={event => onChange({ workspaceId: event.target.value, libraryId: '', projectId: '', productId: '' })}><option value="">选择工作区</option>{workspaces.data?.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}</select><select className={selectClass} aria-label="资料库" disabled={!scope.workspaceId} value={scope.libraryId} onChange={event => onChange({ ...scope, libraryId: event.target.value, productId: '' })}><option value="">选择资料库</option>{libraries.data?.map(item => <option key={item.id} value={item.id}>{item.name}{item.legacy_brand_id ? '（兼容资料库）' : ''}</option>)}</select><select className={selectClass} aria-label="项目（可选绑定）" disabled={!scope.workspaceId} value={scope.projectId} onChange={event => onChange({ ...scope, projectId: event.target.value, productId: '' })}><option value="">不绑定项目</option>{projects.data?.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}</select></div>{error ? <p role="alert" className="text-sm text-destructive">{error.message}</p> : null}</div>
}
