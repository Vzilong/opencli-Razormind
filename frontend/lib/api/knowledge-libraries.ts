'use client'

import { useQuery } from '@tanstack/react-query'

import { apiClient } from './client'
import type { ApiResponse } from './types'
import type { Citation, KnowledgeAnswer } from './brand-knowledge'

export type KnowledgeLibrary = { id: string; workspace_id: string; name: string; description: string | null; legacy_brand_id: string | null }
export type ProjectLibraryBinding = KnowledgeLibrary & { product_id: string | null }
export type KnowledgeScope = { workspaceId: string; libraryId: string; projectId: string; productId: string; legacyBrandId?: string }
export type KnowledgePage = { id: string; library_id: string; brand_id: string | null; product_id: string | null; parent_id: string | null
  title: string; kind: 'folder' | 'source' | 'page'; status: 'draft' | 'published' | 'archived'; revision: number; content?: string
  original_name: string | null; updated_at: string; source_refs: Citation[]; reused?: boolean }

export const libraryRoot = (workspaceId: string) => `/workspaces/${encodeURIComponent(workspaceId)}/knowledge-libraries`
export const libraryPagesRoot = (scope: KnowledgeScope) => `${libraryRoot(scope.workspaceId)}/${encodeURIComponent(scope.libraryId)}`
export const libraryParams = (scope: KnowledgeScope) => scope.productId ? { product_id: scope.productId } : {}
export const knowledgeGet = <T,>(path: string, params = {}) => apiClient.get<ApiResponse<T>>(path, { params }).then(result => result.data.data)
export const knowledgePost = <T,>(path: string, body: unknown) => apiClient.post<ApiResponse<T>>(path, body).then(result => result.data.data)

export function useKnowledgeLibraries(workspaceId: string) {
  return useQuery({ queryKey: ['knowledge-libraries', workspaceId], enabled: !!workspaceId, queryFn: () => knowledgeGet<KnowledgeLibrary[]>(libraryRoot(workspaceId)) })
}

export function useProjectKnowledgeLibraries(workspaceId: string, projectId: string) {
  return useQuery({ queryKey: ['project-knowledge-libraries', workspaceId, projectId], enabled: !!workspaceId && !!projectId,
    queryFn: () => knowledgeGet<ProjectLibraryBinding[]>(`/workspaces/${encodeURIComponent(workspaceId)}/projects/${encodeURIComponent(projectId)}/knowledge-libraries`) })
}

export type { Citation, KnowledgeAnswer }
