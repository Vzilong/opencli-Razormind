import { UserManager, WebStorageStateStore, type User } from 'oidc-client-ts'

let manager: UserManager | null = null

export function isOidcConfigured(): boolean {
  return Boolean(
    process.env.NEXT_PUBLIC_OIDC_AUTHORITY?.trim() &&
      process.env.NEXT_PUBLIC_OIDC_CLIENT_ID?.trim() &&
      process.env.NEXT_PUBLIC_OIDC_AUTHORIZATION_ENDPOINT?.trim(),
  )
}

export function getOidcManager(): UserManager | null {
  if (typeof window === 'undefined' || !isOidcConfigured()) return null
  if (manager) return manager

  const origin = window.location.origin
  const authority = process.env.NEXT_PUBLIC_OIDC_AUTHORITY!.trim().replace(/\/$/, '')
  const authorizationEndpoint = process.env.NEXT_PUBLIC_OIDC_AUTHORIZATION_ENDPOINT!.trim()
  const sessionStore = new WebStorageStateStore({ store: window.sessionStorage })
  manager = new UserManager({
    authority,
    client_id: process.env.NEXT_PUBLIC_OIDC_CLIENT_ID!.trim(),
    redirect_uri: process.env.NEXT_PUBLIC_OIDC_REDIRECT_URI?.trim() || `${origin}/auth/callback`,
    post_logout_redirect_uri:
      process.env.NEXT_PUBLIC_OIDC_POST_LOGOUT_REDIRECT_URI?.trim() || `${origin}/login`,
    response_type: 'code',
    scope: process.env.NEXT_PUBLIC_OIDC_SCOPE?.trim() || 'openid profile email',
    metadata: {
      issuer: authority,
      authorization_endpoint: authorizationEndpoint,
      token_endpoint: `${origin}/api/auth/oidc/token`,
      jwks_uri: `${origin}/api/auth/oidc/jwks`,
    },
    automaticSilentRenew: false,
    loadUserInfo: false,
    monitorSession: false,
    revokeTokensOnSignout: true,
    stateStore: sessionStore,
    userStore: sessionStore,
  })
  return manager
}

export function oidcReturnTo(user: User): string {
  const state = user.state
  if (!state || typeof state !== 'object' || !('returnTo' in state)) return '/launch'
  const returnTo = (state as { returnTo?: unknown }).returnTo
  return sanitizeReturnTo(returnTo)
}

export function isOidcIdentity(authMethod: unknown): boolean {
  return authMethod === 'oidc'
}

export type StoredOidcUserState = 'absent' | 'expired' | 'invalid' | 'usable'

export function classifyStoredOidcUser(
  user: { expired?: boolean; id_token?: string } | null | undefined,
): StoredOidcUserState {
  if (!user) return 'absent'
  if (user.expired) return 'expired'
  if (!user.id_token?.trim()) return 'invalid'
  return 'usable'
}

export function shouldClearIdentityForOidcUnload(
  ownsOidcIdentity: boolean,
  removalInProgress: boolean,
  eventGeneration?: number,
  currentGeneration?: number,
  hasStoredUser = false,
): boolean {
  return (
    ownsOidcIdentity &&
    !removalInProgress &&
    !hasStoredUser &&
    (eventGeneration === undefined ||
      currentGeneration === undefined ||
      eventGeneration === currentGeneration)
  )
}

export function shouldAcceptOidcRenewal(
  ownsOidcIdentity: boolean,
  removalInProgress: boolean,
  eventGeneration: number,
  currentGeneration: number,
): boolean {
  return ownsOidcIdentity && !removalInProgress && eventGeneration === currentGeneration
}

export function sanitizeReturnTo(value: unknown): string {
  return typeof value === 'string' && value.startsWith('/') && !value.startsWith('//')
    ? value
    : '/launch'
}
