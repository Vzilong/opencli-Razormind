'use client'

import { GlobalAgentDock } from '@/components/shell/global-agent-dock'

const keepOpen = () => {}

export default function LaunchPage() {
  return <GlobalAgentDock open onOpenChange={keepOpen} presentation="page" />
}
