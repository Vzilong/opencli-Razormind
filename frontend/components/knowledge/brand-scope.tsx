'use client'

import { useEffect, useState } from 'react'
import { useGovernedWorkspaces } from '@/lib/api/hooks'
import { useBrands, useBrandProducts, type BrandScope } from '@/lib/api/brand-knowledge'

export const selectClass = 'h-10 min-w-0 rounded-lg border border-input bg-background px-3 text-sm focus-visible:outline-2 focus-visible:outline-ring'

export function useBrandScope() {
  const workspaces = useGovernedWorkspaces()
  const [scope, setScope] = useState<BrandScope>({ workspaceId: '', brandId: '', productId: '' })
  useEffect(() => {
    const params = new URLSearchParams(window.location.search)
    if (params.get('workspace')) setScope({ workspaceId: params.get('workspace') || '', brandId: params.get('brand') || '', productId: params.get('product') || '' })
  }, [])
  return { scope, setScope, workspaces }
}

export function BrandScopeFields({ scope, onChange, allowUnclassified = false }: {
  scope: BrandScope; onChange: (value: BrandScope) => void; allowUnclassified?: boolean
}) {
  const workspaces = useGovernedWorkspaces()
  const brands = useBrands(scope.workspaceId)
  const products = useBrandProducts({ ...scope, brandId: scope.brandId === '__unclassified' ? '' : scope.brandId })
  const error = workspaces.error || brands.error || products.error
  return <div className="space-y-2">
    <div className="flex flex-wrap gap-3">
      <select className={selectClass} aria-label="工作区" value={scope.workspaceId} onChange={event => onChange({ workspaceId: event.target.value, brandId: '', productId: '' })}>
        <option value="">选择工作区</option>
        {workspaces.data?.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}
      </select>
      <select className={selectClass} aria-label="品牌" disabled={!scope.workspaceId} value={scope.brandId} onChange={event => onChange({ ...scope, brandId: event.target.value, productId: '' })}>
        <option value="">全部品牌 / 选择品牌</option>
        {allowUnclassified && <option value="__unclassified">未分类项目的数据</option>}
        {brands.data?.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}
      </select>
      <select className={selectClass} aria-label="产品" disabled={!scope.brandId || scope.brandId === '__unclassified'} value={scope.productId} onChange={event => onChange({ ...scope, productId: event.target.value })}>
        <option value="">全部产品 / 品牌通用</option>
        {products.data?.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}
      </select>
    </div>
    {error && <p role="alert" className="text-sm text-destructive">{error.message}<button className="ml-3 underline" onClick={() => { void workspaces.refetch(); if (scope.workspaceId) void brands.refetch(); if (scope.brandId && scope.brandId !== '__unclassified') void products.refetch() }}>重试</button></p>}
  </div>
}
