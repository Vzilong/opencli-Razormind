import { createAgentSession, Settings } from './node_modules/@oh-my-pi/pi-coding-agent/src/sdk.ts'
import { SessionManager } from './node_modules/@oh-my-pi/pi-coding-agent/src/session/session-manager.ts'
import { runRpcMode } from './node_modules/@oh-my-pi/pi-coding-agent/src/modes/rpc/rpc-mode.ts'
import metadata from './node_modules/@oh-my-pi/pi-coding-agent/package.json'

if (metadata.version !== '18.1.15') throw new Error('Unsupported OMP SDK version')
if (!process.argv.includes('--operator-chat') && !process.argv.includes('--operator-chat-probe')) {
  throw new Error('Only controlled inference is supported')
}

const created = await createAgentSession({
  cwd: process.cwd(),
  agentDir: '/home/agent/.omp/agent',
  settings: Settings.isolated(),
  sessionManager: SessionManager.inMemory(process.cwd()),
  systemPrompt: 'You are an inference-only assistant. Follow the supplied conversation and its XML tool protocol. Never execute native tools.',
  toolNames: [],
  restrictToolNames: true,
  allowRestrictedCustomTools: false,
  customTools: [],
  extensions: [],
  additionalExtensionPaths: [],
  disableExtensionDiscovery: true,
  skills: [],
  rules: [],
  contextFiles: [],
  promptTemplates: [],
  slashCommands: [],
  enableMCP: false,
  enableLsp: false,
  enableIrc: false,
  skipPythonPreflight: true,
  requireYieldTool: false,
  hasUI: false,
  interactivePrompts: false,
})

if (created.session.getActiveToolNames().length || created.mcpManager || created.extensionsResult.extensions.length) {
  throw new Error('OMP inference tool isolation failed')
}

if (process.argv.includes('--operator-chat-probe')) {
  process.stdout.write(JSON.stringify({
    runtime: 'omp', version: metadata.version, protocol: 'rpc-v1', operator_chat: true,
    native_tools: false, mcp: false, extensions: false, user_context: false,
  }) + '\n')
  process.exit(0)
}

await runRpcMode(created.session, created.setToolUIContext, created.subagentEventBus)
