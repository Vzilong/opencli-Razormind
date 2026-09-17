import { expect, test } from '@playwright/test'

const envelope = data => ({ contentType: 'application/json', body: JSON.stringify({ success: true, data }) })

async function mockKnowledge(page) {
  const requests = []
  const pages = []
  const products = [{ id: 'a', name: '产品 A', description: '' }, { id: 'b', name: '产品 B', description: '' }]
  await page.addInitScript(() => sessionStorage.setItem('opencli.bootstrapIdentityToken', 'test-token'))
  await page.route('**/api/v1/**', async route => {
    const request = route.request()
    const url = new URL(request.url())
    const path = url.pathname
    requests.push({ path, search: url.search, method: request.method(), body: request.postData() })
    if (path.endsWith('/auth/me')) return route.fulfill(envelope({ subject: 'admin', name: 'Admin', is_platform_admin: true, auth_method: 'bootstrap' }))
    if (path.endsWith('/governance/workspaces')) return route.fulfill(envelope([{ id: 'w', name: '品牌工作区' }]))
    if (path.endsWith('/brands')) return route.fulfill(envelope([{ id: 'g', name: '高吉星', description: '' }]))
    if (path.endsWith('/products')) return route.fulfill(envelope(products))
    if (path.endsWith('/projects')) return route.fulfill(envelope([{ id: 'p', name: '产品采集项目', brand_id: null, product_id: null }]))
    if (path.endsWith('/projects/p') && request.method() === 'PUT') return route.fulfill(envelope({}))
    if (path.endsWith('/records')) return route.fulfill(envelope([]))
    if (path.endsWith('/upload')) {
      const item = { id: 'source', title: '产品说明.md', kind: 'source', status: 'published', revision: 1,
        brand_id: 'g', product_id: url.searchParams.get('product_id'), parent_id: null,
        original_name: '产品说明.md', content: '产品特点：轻便。', source_refs: [], updated_at: new Date().toISOString() }
      pages.push(item)
      return route.fulfill(envelope(item))
    }
    if (path.endsWith('/pages') && request.method() === 'POST') {
      const item = { id: `page-${pages.length}`, ...request.postDataJSON(), revision: 1,
        status: 'draft', content: '', original_name: null, source_refs: [], updated_at: new Date().toISOString() }
      pages.push(item); return route.fulfill(envelope(item))
    }
    if (path.endsWith('/pages')) return route.fulfill(envelope(pages.filter(item => !url.searchParams.get('product_id') || !item.product_id || item.product_id === url.searchParams.get('product_id'))))
    if (path.includes('/pages/')) {
      const item = pages.find(item => path.endsWith(`/pages/${item.id}`))
      if (item && request.method() === 'PATCH') Object.assign(item, request.postDataJSON(), { revision: item.revision + 1 })
      return route.fulfill(envelope(item || []))
    }
    if (path.endsWith('/search')) return route.fulfill(envelope([{ page_id: 'source', title: '产品说明.md', excerpt: '产品特点：轻便。', revision: 1, product_id: 'a', kind: 'source' }]))
    if (path.endsWith('/ask')) return route.fulfill(envelope({ answer: '产品 A 的特点是轻便。[1]', citations: [{ number: 1, page_id: 'source', title: '产品说明.md', excerpt: '产品特点：轻便。', revision: 1, product_id: 'a', kind: 'source' }] }))
    return route.fulfill(envelope([]))
  })
  return { requests, pages }
}

test('知识资料上传、检索与问答携带产品范围，切换产品清除旧回答', async ({ page }) => {
  const { requests } = await mockKnowledge(page)
  await page.goto('/knowledge?workspace=w&brand=g&product=a')
  await expect(page.getByRole('heading', { name: '品牌知识库', exact: true })).toBeVisible()
  await page.getByLabel('上传知识资料').setInputFiles({ name: '产品说明.md', mimeType: 'text/markdown', buffer: Buffer.from('产品特点：轻便。') })
  await expect(page.getByLabel('页面正文')).toHaveValue('产品特点：轻便。')
  await expect(page.getByLabel('页面正文')).toHaveAttribute('readonly', '')
  expect(requests.find(item => item.path.endsWith('/upload')).search).toContain('product_id=a')
  await page.getByLabel('查询问题').fill('产品特点')
  await page.getByRole('button', { name: '搜索', exact: true }).click()
  await expect(page.getByRole('button', { name: /引用版本 v1/ })).toBeVisible()
  expect(requests.find(item => item.path.endsWith('/search')).search).toContain('product_id=a')
  await page.getByRole('button', { name: 'AI 问答', exact: true }).click()
  await expect(page.getByText('产品 A 的特点是轻便。[1]', { exact: true })).toBeVisible()
  await page.screenshot({ path: test.info().outputPath('knowledge-library.png'), fullPage: true })
  expect(JSON.parse(requests.find(item => item.path.endsWith('/ask')).body).product_id).toBe('a')
  await page.getByRole('combobox', { name: '产品', exact: true }).selectOption('b')
  await expect(page.getByText('产品 A 的特点是轻便。[1]', { exact: true })).toHaveCount(0)
  await expect(page.getByLabel('页面正文')).toHaveCount(0)
  await page.setViewportSize({ width: 390, height: 844 })
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
})

test('整理页面可保存草稿和核实发布', async ({ page }) => {
  await mockKnowledge(page)
  await page.goto('/knowledge?workspace=w&brand=g')
  await page.getByLabel('新页面名称').fill('品牌售后政策')
  await page.getByRole('button', { name: '新建', exact: true }).click()
  await page.getByLabel('页面正文').fill('售后政策经人工核实。')
  await page.getByRole('button', { name: '保存草稿', exact: true }).click()
  await expect(page.getByText('整理页面 · 草稿 · v2', { exact: true })).toBeVisible()
  await page.getByRole('button', { name: '核实并发布', exact: true }).click()
  await expect(page.getByText('整理页面 · 已发布 · v3', { exact: true })).toBeVisible()
})

test('项目按产品归类，记录筛选时禁用全站清空', async ({ page }) => {
  const { requests } = await mockKnowledge(page)
  await page.goto('/brands?workspace=w&brand=g')
  await page.getByLabel('产品采集项目所属产品').selectOption('a')
  await expect.poll(() => requests.some(item => item.method === 'PUT' && item.path.endsWith('/projects/p'))).toBe(true)
  expect(JSON.parse(requests.find(item => item.method === 'PUT').body)).toEqual({ product_id: 'a' })
  await page.goto('/records?workspace=w&brand=g&product=a')
  await expect(page.getByRole('button', { name: '清空全部', exact: true })).toBeDisabled()
  await expect.poll(() => requests.some(item => item.path.endsWith('/records') && item.search.includes('product_id=a') && item.search.includes('brand_id=g'))).toBe(true)
})

test('未保存正文切换产品时提醒，取消后保留输入', async ({ page }) => {
  await mockKnowledge(page)
  await page.goto('/knowledge?workspace=w&brand=g')
  await page.getByLabel('新页面名称').fill('待编辑知识')
  await page.getByRole('button', { name: '新建', exact: true }).click()
  await page.getByLabel('页面正文').fill('还没有保存的正文')
  await expect(page.getByText('有未保存的修改', { exact: true })).toBeVisible()
  page.once('dialog', dialog => dialog.dismiss())
  await page.getByRole('combobox', { name: '产品', exact: true }).selectOption('a')
  await expect(page.getByLabel('页面正文')).toHaveValue('还没有保存的正文')
  await expect(page.getByRole('combobox', { name: '产品', exact: true })).toHaveValue('')
  await page.getByRole('button', { name: '保存草稿', exact: true }).click()
  await expect(page.getByText('有未保存的修改', { exact: true })).toHaveCount(0)
})
