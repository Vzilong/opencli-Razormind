export type TerminalStatus = 'connecting' | 'reconnecting' | 'connected' | 'locked' | 'kicked' | 'stopping' | 'closed' | 'error'

export type TerminalServerControl =
  | { type: 'attached'; controls: boolean; status: string; exit_code: number | null; cleanup_complete: boolean; replay_truncated: boolean }
  | { type: 'locked'; controls: false; status: string; exit_code: number | null; cleanup_complete: boolean; replay_truncated: boolean }
  | { type: 'control_granted'; controls: true }
  | { type: 'kicked' }
  | { type: 'snapshot_begin'; sequence: number }
  | { type: 'snapshot_end'; sequence: number }
  | { type: 'exit'; status: 'exited'; exit_code: number; cleanup_complete: boolean }
  | { type: 'transport'; status: 'reconnecting'; recoverable: true }
  | { type: 'takeover'; controls: boolean }

export type TerminalClientControl =
  | { type: 'resize'; cols: number; rows: number }
  | { type: 'takeover' }

export function parseTerminalControl(input: string): TerminalServerControl | null {
  let value: unknown
  try {
    value = JSON.parse(input)
  } catch {
    return null
  }
  if (!value || typeof value !== 'object') return null
  const control = value as Record<string, unknown>
  if (control.type === 'snapshot_begin' || control.type === 'snapshot_end') {
    return typeof control.sequence === 'number'
      ? { type: control.type, sequence: control.sequence }
      : null
  }
  if (control.type === 'kicked') return { type: 'kicked' }
  if (control.type === 'control_granted' && control.controls === true) {
    return { type: 'control_granted', controls: true }
  }
  if (control.type === 'takeover' && typeof control.controls === 'boolean') {
    return { type: 'takeover', controls: control.controls }
  }
  if (control.type === 'transport' && control.status === 'reconnecting') {
    return { type: 'transport', status: 'reconnecting', recoverable: true }
  }
  if (control.type === 'exit' && typeof control.exit_code === 'number') {
    return {
      type: 'exit',
      status: 'exited',
      exit_code: control.exit_code,
      cleanup_complete: control.cleanup_complete === true,
    }
  }
  if ((control.type === 'attached' || control.type === 'locked') && typeof control.status === 'string') {
    return {
      type: control.type,
      controls: control.type === 'attached' && control.controls === true,
      status: control.status,
      exit_code: typeof control.exit_code === 'number' ? control.exit_code : null,
      cleanup_complete: control.cleanup_complete === true,
      replay_truncated: control.replay_truncated === true,
    } as TerminalServerControl
  }
  return null
}
