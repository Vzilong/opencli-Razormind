import assert from 'node:assert/strict'
import test from 'node:test'
import http from 'node:http'
import { once } from 'node:events'
import { parse } from 'node:url'
import { proxyRequest } from 'next/dist/server/lib/router-utils/proxy-request.js'
import nextConfig from '../next.config.mjs'

test('API rewrite outlives maximum native dispatch and cleanup deadlines', async () => {
  assert.ok(nextConfig.experimental.proxyTimeout > 630_000)
  assert.ok(nextConfig.experimental.proxyTimeout <= 900_000)
  const rewrites = await nextConfig.rewrites()
  assert.ok(rewrites.some((route) => route.source === '/api/v1/:path*'))
})

test('a real slow native response is not cut off at the old 30-second deadline', { timeout: 45_000 }, async () => {
  const upstream = http.createServer((_request, response) => {
    const timer = setTimeout(() => response.end('native-completed'), 31_000)
    response.on('close', () => clearTimeout(timer))
  })
  upstream.listen(0, '127.0.0.1')
  await once(upstream, 'listening')
  const proxy = http.createServer((request, response) => {
    void proxyRequest(request, response,
      parse(`http://127.0.0.1:${upstream.address().port}/`, true),
      undefined, undefined, nextConfig.experimental.proxyTimeout,
    ).catch(() => response.destroy())
  })
  proxy.listen(0, '127.0.0.1')
  await once(proxy, 'listening')
  try {
    const result = await fetch(`http://127.0.0.1:${proxy.address().port}/`, { signal: AbortSignal.timeout(40_000) })
    assert.equal(await result.text(), 'native-completed')
  } finally {
    proxy.closeAllConnections()
    upstream.closeAllConnections()
    await Promise.all([new Promise((resolve) => proxy.close(resolve)), new Promise((resolve) => upstream.close(resolve))])
  }
})
