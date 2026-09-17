import { expect, test } from '@playwright/test'

const envelope = data => ({ contentType: 'application/json', body: JSON.stringify({ success: true, data }) })
const workspace = { id: 'w', name: 'Research workspace', slug: 'research', active: true }
const project = { id: 'p', workspace_id: 'w', name: 'Launch project', slug: 'launch', description: null, archived: false }

async function installApi(page, { libraries = [] } = {}) {
  const calls = []
  const pages = []
  await page.addInitScript(() => sessionStorage.setItem('opencli.bootstrapIdentityToken', 'knowledge-test-token'))
  await page.route('**/api/v1/**', async route => {
    const request = route.request(); const url = new URL(request.url()); const path = url.pathname
    calls.push({ path, search: url.search, method: request.method(), body: request.postDataJSON?.() })
    if (path.endsWith('/auth/me')) return route.fulfill(envelope({ subject: 'admin', name: 'Admin', is_platform_admin: true, auth_method: 'bootstrap' }))
    if (path.endsWith('/governance/workspaces')) return route.fulfill(envelope([workspace]))
    if (path === '/api/v1/governance/workspaces/w/projects') return route.fulfill(envelope([project]))
    if (path === '/api/v1/workspaces/w/knowledge-libraries' && request.method() === 'GET') return route.fulfill(envelope(libraries))
    if (path === '/api/v1/workspaces/w/knowledge-libraries' && request.method() === 'POST') { const item = { id: 'lib', workspace_id: 'w', name: request.postDataJSON().name, description: request.postDataJSON().description, legacy_brand_id: null }; libraries.push(item); return route.fulfill(envelope(item)) }
    if (path === '/api/v1/workspaces/w/projects/p/knowledge-libraries') return route.fulfill(envelope([]))
    if (path.includes('/projects/p/knowledge-libraries/') && request.method() === 'PUT') return route.fulfill(envelope({ ...libraries[0], product_id: null }))
    if (path.includes('/projects/p/knowledge-libraries/') && request.method() === 'DELETE') return route.fulfill(envelope({}))
    if (path.endsWith('/pages') && request.method() === 'GET') return route.fulfill(envelope(pages))
    if (path.endsWith('/pages') && request.method() === 'POST') { const item = { id: 'page-1', library_id: 'lib', brand_id: null, product_id: null, parent_id: null, title: request.postDataJSON().title, kind: 'page', status: 'draft', revision: 1, content: '', original_name: null, source_refs: [], updated_at: new Date().toISOString() }; pages.push(item); return route.fulfill(envelope(item)) }
    if (path.includes('/pages/page-1')) { const item = pages[0]; if (request.method() === 'PATCH') Object.assign(item, request.postDataJSON(), { revision: item.revision + 1 }); return route.fulfill(envelope(item)) }
    if (path.endsWith('/records')) return route.fulfill(envelope([]))
    return route.fulfill(envelope([]))
  })
  return calls
}

test('creates a generic library without a Brand and binds it to a project', async ({ page }) => {
  const calls = await installApi(page)
  await page.goto('/knowledge?workspace=w')
  await expect(page.getByText('创建资料库')).toBeVisible()
  await page.getByLabel('新资料库名称').fill('公开研究')
  await page.getByRole('button', { name: '创建资料库' }).click()
  await expect.poll(() => calls.some(call => call.path === '/api/v1/workspaces/w/knowledge-libraries' && call.method === 'POST')).toBe(true)
  const create = calls.find(call => call.path === '/api/v1/workspaces/w/knowledge-libraries' && call.method === 'POST')
  expect(create.body).toEqual({ name: '公开研究', description: '' })
  await expect(page.getByLabel('新资料库名称')).toHaveAttribute('maxlength', '120')
  await page.getByLabel('项目（可选绑定）').selectOption('p')
  await page.getByRole('button', { name: '绑定到项目' }).click()
  await expect.poll(() => calls.some(call => call.path.endsWith('/projects/p/knowledge-libraries/lib') && call.method === 'PUT')).toBe(true)
})

test('uses generic workspace and project filters for records', async ({ page }) => {
  const calls = await installApi(page, { libraries: [{ id: 'lib', workspace_id: 'w', name: 'Public research', description: null, legacy_brand_id: null }] })
  await page.goto('/records?workspace=w&project=p')
  await expect.poll(() => calls.some(call => call.path.endsWith('/records'))).toBe(true)
  await expect.poll(() => calls.some(call => call.path.endsWith('/records') && call.search.includes('workspace_id=w') && call.search.includes('project_id=p'))).toBe(true)
  const recordCall = calls.find(call => call.path.endsWith('/records') && call.search.includes('workspace_id=w'))
  expect(recordCall.search).toContain('workspace_id=w')
  expect(recordCall.search).toContain('project_id=p')
})

test('does not load unscoped records before a workspace is selected', async ({ page }) => {
  const calls = await installApi(page)
  await page.goto('/records')
  await expect(page.getByText('先选择工作区后才会加载记录', { exact: false })).toBeVisible()
  await expect(page.getByRole('button', { name: /清空全部/ })).toHaveCount(0)
  expect(calls.some(call => call.path.endsWith('/records'))).toBe(false)
})

test('maps a legacy brand deep link to its library while preserving product scope', async ({ page }) => {
  const calls = await installApi(page, { libraries: [{ id: 'legacy-lib', workspace_id: 'w', name: 'Legacy brand', description: '', legacy_brand_id: 'brand-a' }] })
  await page.goto('/knowledge?workspace=w&brand=brand-a&product=product-a')
  await expect.poll(() => calls.some(call => call.path === '/api/v1/workspaces/w/knowledge-libraries/legacy-lib/pages' && call.search.includes('product_id=product-a'))).toBe(true)
})

test('declining dirty-editor scope replacement preserves the current page', async ({ page }) => {
  await installApi(page, { libraries: [{ id: 'lib', workspace_id: 'w', name: 'Existing', description: '', legacy_brand_id: null }] })
  await page.goto('/knowledge?workspace=w&library=lib')
  await page.getByLabel('新页面名称').fill('未保存页面')
  await page.getByRole('button', { name: '新建', exact: true }).click()
  await page.getByLabel('页面正文').fill('不要丢失')
  page.once('dialog', dialog => dialog.dismiss())
  await page.getByLabel('工作区').selectOption('')
  await expect(page.getByLabel('页面正文')).toHaveValue('不要丢失')
  await expect(page.getByLabel('工作区')).toHaveValue('w')
})
