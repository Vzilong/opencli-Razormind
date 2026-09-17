'use client'

import Link from 'next/link'
import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { PageContainer } from '@/components/shell/page-container'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { BrandScopeFields, selectClass, useBrandScope } from '@/components/knowledge/brand-scope'
import { apiClient } from '@/lib/api/client'
import { brandRoot, knowledgeRoot, knowledgeGet, knowledgePost, useBrandProducts, type BrandScope, type NamedItem, type ProjectClassification } from '@/lib/api/brand-knowledge'

function BrandManager({ scope }: { scope: BrandScope }) {
  const client = useQueryClient()
  const [name, setName] = useState('')
  const products = useBrandProducts(scope)
  const projects = useQuery({ queryKey: ['brand-projects', scope.workspaceId, scope.brandId], enabled: !!scope.brandId,
    queryFn: () => knowledgeGet<ProjectClassification[]>(`${knowledgeRoot(scope)}/projects`) })
  const create = useMutation({ mutationFn: () => knowledgePost<NamedItem>(scope.brandId ? `${knowledgeRoot(scope)}/products` : brandRoot(scope.workspaceId), { name }),
    onSuccess: () => { setName(''); toast.success(scope.brandId ? '产品已创建' : '品牌已创建'); void client.invalidateQueries({ queryKey: [scope.brandId ? 'brand-products' : 'brands'] }) },
    onError: (error: Error) => toast.error(error.message) })
  const assign = useMutation({ mutationFn: ({ id, productId }: { id: string; productId: string }) => productId === '__unclassified'
    ? apiClient.delete(`${knowledgeRoot(scope)}/projects/${id}`)
    : apiClient.put(`${knowledgeRoot(scope)}/projects/${id}`, { product_id: productId || null }),
    onSuccess: () => { toast.success('项目分类已更新'); void client.invalidateQueries({ queryKey: ['brand-projects'] }); void client.invalidateQueries({ queryKey: ['records'] }) },
    onError: (error: Error) => toast.error(error.message) })
  return <div className="grid gap-6 lg:grid-cols-[20rem_1fr]">
    <section className="space-y-4 rounded-xl border bg-card p-5">
      <h2 className="font-semibold">{scope.brandId ? '品牌旗下产品' : '新建品牌'}</h2>
      <form className="flex gap-2" onSubmit={event => { event.preventDefault(); create.mutate() }}>
        <Input aria-label={scope.brandId ? '产品名称' : '品牌名称'} placeholder={scope.brandId ? '输入产品名称' : '例如：高吉星'} value={name} maxLength={120} onChange={event => setName(event.target.value)} />
        <Button type="submit" disabled={!name.trim() || create.isPending}>添加</Button>
      </form>
      {products.data?.map(product => <div className="rounded-lg border px-3 py-2 text-sm" key={product.id}>{product.name}</div>)}
      {scope.brandId && products.data?.length === 0 && <p className="text-sm text-muted-foreground">添加产品后，即可分别归类项目与知识资料。</p>}
      {scope.brandId && <Link className="block text-sm underline" href={`/knowledge?workspace=${scope.workspaceId}&brand=${scope.brandId}`}>打开品牌知识库 →</Link>}
    </section>
    <section className="space-y-4 rounded-xl border bg-card p-5">
      <h2 className="font-semibold">项目与产品归属</h2>
      <p className="text-sm text-muted-foreground">项目分类适用于该项目已有及后续的采集数据。品牌通用项目不归入某个产品；未分类项目保持原样。其他品牌的项目需在原品牌先取消归类。</p>
      {!scope.brandId ? <p className="text-sm">请先选择品牌。</p> : projects.isPending ? <p role="status">正在加载项目…</p> : projects.isError ? <p role="alert">{projects.error.message}</p> : <div className="divide-y">
        {projects.data?.filter(project => !project.brand_id || project.brand_id === scope.brandId).map(project => <div key={project.id} className="flex flex-wrap items-center justify-between gap-3 py-3">
          <Link className="text-sm hover:underline" href={`/studio/projects/${project.id}?workspace=${scope.workspaceId}`}>{project.name}</Link>
          <select className={selectClass} aria-label={`${project.name}所属产品`} disabled={assign.isPending} value={project.brand_id ? project.product_id || '' : '__unclassified'} onChange={event => assign.mutate({ id: project.id, productId: event.target.value })}>
            <option value="__unclassified">未分类</option><option value="">当前品牌 · 通用项目</option>
            {products.data?.map(product => <option key={product.id} value={product.id}>{product.name}</option>)}
          </select>
        </div>)}
        {projects.data?.length === 0 && <p className="py-4 text-sm">当前工作区还没有项目，请先在项目页面创建。</p>}
      </div>}
    </section>
  </div>
}

export default function BrandsPage() {
  const { scope, setScope } = useBrandScope()
  return <PageContainer title="品牌与产品" description="管理品牌、旗下产品，以及采集项目的业务归属。" actions={<Link className="text-sm underline" href="/knowledge">知识库</Link>}>
    <BrandScopeFields scope={scope} onChange={setScope} />
    {scope.workspaceId ? <BrandManager key={`${scope.workspaceId}:${scope.brandId}`} scope={scope} /> : <p className="rounded-xl border p-8 text-sm text-muted-foreground">选择工作区，开始管理品牌与产品。</p>}
  </PageContainer>
}
