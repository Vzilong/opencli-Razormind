'use client'

import { useQuery } from '@tanstack/react-query'
import { apiClient } from './client'
import type { ApiResponse } from './types'

export type NamedItem = { id: string; name: string; description: string }
export type BrandScope = { workspaceId: string; brandId: string; productId: string }
export type KnowledgePage = {
  id: string; title: string; brand_id: string; product_id: string | null; parent_id: string | null
  kind: 'folder' | 'source' | 'page'; status: 'draft' | 'published' | 'archived'
  revision: number; content?: string; original_name: string | null; updated_at: string
  source_refs: Citation[]
  reused?: boolean
}
export type Citation = {
  page_id: string; title: string; revision: number; excerpt: string; number?: number
  product_id: string | null; kind: string
  product_name?: string | null
}
export type KnowledgeAnswer = { answer: string; citations: Citation[] }
export type ProjectClassification = { id: string; name: string; brand_id: string | null; product_id: string | null }
export const brandRoot = (workspaceId: string) => `/workspaces/${encodeURIComponent(workspaceId)}/brands`
export const knowledgeRoot = (scope: BrandScope) => `${brandRoot(scope.workspaceId)}/${encodeURIComponent(scope.brandId)}`
export const knowledgeParams = (scope: BrandScope) => scope.productId ? { product_id: scope.productId } : {}
export const knowledgeGet = <T,>(path: string, params = {}) =>
  apiClient.get<ApiResponse<T>>(path, { params }).then(result => result.data.data)
export const knowledgePost = <T,>(path: string, body: unknown) =>
  apiClient.post<ApiResponse<T>>(path, body).then(result => result.data.data)

export function useBrands(workspaceId: string) {
  return useQuery({ queryKey: ['brands', workspaceId], enabled: !!workspaceId,
    queryFn: () => knowledgeGet<NamedItem[]>(brandRoot(workspaceId)) })
}
export function useBrandProducts(scope: BrandScope) {
  return useQuery({ queryKey: ['brand-products', scope.workspaceId, scope.brandId], enabled: !!scope.workspaceId && !!scope.brandId,
    queryFn: () => knowledgeGet<NamedItem[]>(`${knowledgeRoot(scope)}/products`) })
}
