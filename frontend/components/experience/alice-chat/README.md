# OpenAlice chat presentation port

Scope: only the right-hand `/launch` chat surface. OpenCLI's existing AppSidebar,
AppHeader, work tabs and other pages remain in place. Do not extend this port to
the application navigation without a separate user request.

Source: OpenAlice revision `c02a3c7d7d438783897e3f7610714b121c41367c`, inspected
locally at `D:/Temp/openalice-comparison-20260915` on 2026-09-16.

- `composer-shell.tsx` copies `ui/src/components/conversation/ComposerShell.tsx`.
- `chat-surface.tsx` adapts the landing layout, suggestions and composer placement
  from `ui/src/pages/ChatLandingPage.tsx` to existing OpenCLI conversation APIs.
- `chat-surface.css` scopes the composer styles from `ui/src/index.css` beneath
  `.alice-chat`; colors do not replace OpenCLI's global theme.
- `agent-runtime-picker.tsx` adapts the upstream `AgentRuntimePicker.tsx`
  installed/missing groups, runtime rows, searchable Others dialog, installation
  command copying and setup links. The four-slot default projection follows
  `agentRuntimeQuickAccess.ts`; OpenCLI's built-in runner is retained as an
  additional current-runtime option, not substituted for installed native CLIs.
- `agent-install.ts` carries upstream `agentInstall.ts` guidance. Commands are
  copied as text only, never installed or executed; shell examples are upstream
  guidance, not an assertion that they run directly in Windows PowerShell.
- `agent-runtime-icon.tsx` uses the same assets and monochrome masks as upstream
  `agentRuntimeIcon.tsx`. Seven SVGs in `public/agent-runtime-icons/` come from
  `@lobehub/icons-static-svg` 1.94.0 (MIT); its original notice is retained in
  `LICENSE-LobeHub.txt`, sourced from the [LobeHub license](https://raw.githubusercontent.com/lobehub/lobe-icons/master/LICENSE).
  `omp.svg` comes from OpenAlice and remains under its AGPL notice below.
- The copied upstream code is licensed under AGPL-3.0; the complete upstream
  license is retained in `LICENSE-OpenAlice.txt`. This is not an assertion that
  the copied code is covered by the repository's Apache license. Distribution
  requires reviewing the applicable upstream license obligations.

Adaptations: Chinese collection examples, OpenCLI branding, workspace authority,
existing proposal confirmation and session APIs. History/new-conversation
controls stay in the right-hand toolbar, not a replacement left navigation.
The built-in runtime uses the existing governed conversation runner. Connection
and model selection are validated server-side and pinned to each new session;
they never rewrite global defaults or expose provider credentials. A default
route readiness check prevents sending when no configured route exists.

Cancellation is cooperative: accepting a stop request does not mean execution
has stopped. The UI waits for the persisted terminal turn, including after a
reload or disconnected send request. Completed external effects are not undone.

Installation and execution are separate: administrators see an allowlisted
controller-host PATH probe, without command execution, binary paths, account
files or credentials. Unknown installation status is never grouped as missing
or installed. Installed choices can be inspected, but unavailable execution
remains blocked at send time and by server validation. Native execution must
reuse the registered-node/isolated-runner boundary; do not launch directly from
the controller to imitate Alice. TUI and reasoning overrides remain unavailable.
