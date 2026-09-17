export const AGENT_INSTALL: Record<string, { command: string; url: string }> = {
  claude: { command: 'npm install -g @anthropic-ai/claude-code', url: 'https://docs.claude.com/en/docs/claude-code/setup' },
  codex: { command: 'npm install -g @openai/codex', url: 'https://github.com/openai/codex' },
  opencode: { command: 'npm install -g opencode-ai', url: 'https://opencode.ai' },
  pi: { command: 'npm install -g @earendil-works/pi-coding-agent', url: 'https://github.com/earendil-works/pi' },
  cursor: { command: 'curl https://cursor.com/install -fsS | bash', url: 'https://cursor.com/docs/cli/overview' },
  agy: { command: 'curl -fsSL https://antigravity.google/cli/install.sh | bash', url: 'https://www.antigravity.google/docs/cli/install/' },
  grok: { command: 'curl -fsSL https://x.ai/cli/install.sh | bash', url: 'https://x.ai/cli' },
  omp: { command: 'curl -fsSL https://omp.sh/install | sh', url: 'https://omp.sh/' },
}
