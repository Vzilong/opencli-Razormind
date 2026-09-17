'use client'

import { Bot } from 'lucide-react'

const BRANDS: Record<string, { asset: string; monochrome?: boolean }> = {
  claude: { asset: 'claude-color' },
  codex: { asset: 'codex', monochrome: true },
  cursor: { asset: 'cursor', monochrome: true },
  agy: { asset: 'antigravity-color' },
  grok: { asset: 'grok', monochrome: true },
  omp: { asset: 'omp' },
  opencode: { asset: 'opencode', monochrome: true },
  pi: { asset: 'pi', monochrome: true },
}

export function AgentRuntimeIcon({ runtimeId, className }: { runtimeId?: string; className?: string }) {
  const brand = runtimeId ? BRANDS[runtimeId] : undefined
  if (!brand) return <Bot aria-hidden data-agent-runtime-icon={runtimeId ?? 'generic'} className={className} />
  const image = `url("/agent-runtime-icons/${brand.asset}.svg") center / contain no-repeat`
  return <span aria-hidden data-agent-runtime-icon={runtimeId} className={`${className ?? ''} inline-block ${brand.monochrome ? 'bg-current' : ''}`} style={brand.monochrome ? { mask: image, WebkitMask: image } : { background: image }} />
}
