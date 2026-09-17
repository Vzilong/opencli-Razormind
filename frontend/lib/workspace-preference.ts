import type { AuthIdentity } from '@/lib/auth/types'

type WorkspacePreferenceIdentity = Pick<AuthIdentity, 'auth_method' | 'subject'>
type ReadableStorage = Pick<Storage, 'getItem'>
type WritableStorage = Pick<Storage, 'setItem'>

export function workspacePreferenceStorageKey(identity: WorkspacePreferenceIdentity | null | undefined) {
  if (!identity?.subject) return null
  const scope = JSON.stringify([identity.auth_method || 'unknown', identity.subject])
  return `opencli:studio-workspace:v2:${encodeURIComponent(scope)}`
}

export function readWorkspacePreference(
  identity: WorkspacePreferenceIdentity | null | undefined,
  storage?: ReadableStorage,
) {
  const key = workspacePreferenceStorageKey(identity)
  if (!key) return null
  try {
    return (storage ?? window.localStorage).getItem(key)
  } catch {
    return null
  }
}

export function writeWorkspacePreference(
  identity: WorkspacePreferenceIdentity | null | undefined,
  workspaceId: string,
  storage?: WritableStorage,
) {
  const key = workspacePreferenceStorageKey(identity)
  if (!key) return
  try {
    (storage ?? window.localStorage).setItem(key, workspaceId)
  } catch {
    // Workspace selection remains usable when persistence is unavailable.
  }
}
