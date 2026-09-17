'use client'

import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react'

import { changeLocalPassword, getCurrentIdentity, getHealth, loginWithPassword } from '@/lib/api/endpoints'
import {
  AUTH_REQUIRED_EVENT,
  shouldInvalidateIdentityForGeneration,
  type AuthRequiredEventDetail,
} from '@/lib/api/auth-events'
import {
  classifyIdentityRecoveryError,
  createIdentityRecoveryCoordinator,
  getApiErrorStatus,
  isAuthRejection,
  isFleetAuthError,
  RecoverySupersededError,
  retryIdentityValidation,
  waitForApiLiveness,
  type RecoveryEpoch,
} from '@/lib/api/recovery'
import {
  classifyStoredOidcUser,
  getOidcManager,
  isOidcConfigured,
  isOidcIdentity,
  oidcReturnTo,
  sanitizeReturnTo,
  shouldAcceptOidcRenewal,
  shouldClearIdentityForOidcUnload,
} from '@/lib/auth/oidc'
import {
  clearIdentityToken,
  getIdentityAccessToken,
  getBootstrapIdentityToken,
  getIdentityGeneration,
  hasDevelopmentSession,
  isDevelopmentLoginAllowed,
  persistBootstrapIdentityToken,
  setDevelopmentSession,
  setRuntimeIdentityToken,
} from '@/lib/auth/session'
import type { AuthIdentity, AuthStatus } from '@/lib/auth/types'

type AuthContextValue = {
  status: AuthStatus
  identity: AuthIdentity | null
  recoveryError: string | null
  recoveryMode: 'service' | 'incompatible' | null
  recoveryPending: boolean
  oidcEnabled: boolean
  developmentLoginEnabled: boolean
  signInWithOidc: (returnTo?: string) => Promise<void>
  signInWithPassword: (username: string, password: string) => Promise<boolean>
  changePassword: (currentPassword: string, newPassword: string) => Promise<void>
  completeOidcSignIn: () => Promise<string>
  enterDevelopmentMode: () => void
  retrySession: () => Promise<void>
  signOut: () => Promise<void>
}

type IdentityTokenOwner = 'bootstrap' | 'oidc'

const AuthContext = createContext<AuthContextValue | null>(null)

const DEVELOPMENT_IDENTITY: AuthIdentity = {
  subject: 'bootstrap-admin',
  email: null,
  name: 'Local Development',
  username: null,
  picture: null,
  is_platform_admin: true,
  auth_method: 'development',
}

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [status, setStatus] = useState<AuthStatus>('loading')
  const [identity, setIdentity] = useState<AuthIdentity | null>(null)
  const [recoveryError, setRecoveryError] = useState<string | null>(null)
  const [recoveryMode, setRecoveryMode] = useState<'service' | 'incompatible' | null>(null)
  const [recoveryPending, setRecoveryPending] = useState(false)
  const mountedRef = useRef(false)
  const restorePromiseRef = useRef<Promise<void> | null>(null)
  const oidcRenewPromiseRef = useRef<Promise<void> | null>(null)
  const oidcRemovalPromiseRef = useRef<Promise<boolean> | null>(null)
  const oidcRemovalInProgressRef = useRef(false)
  const oidcOwnershipRef = useRef(false)
  const oidcOwnershipGenerationRef = useRef(0)
  const recoveryCoordinatorRef = useRef<ReturnType<typeof createIdentityRecoveryCoordinator> | null>(null)
  if (!recoveryCoordinatorRef.current) {
    recoveryCoordinatorRef.current = createIdentityRecoveryCoordinator()
  }
  const recoveryCoordinator = recoveryCoordinatorRef.current
  const oidcEnabled = isOidcConfigured()
  const developmentLoginEnabled = isDevelopmentLoginAllowed()

  const isRecoveryCurrent = useCallback(
    (epoch: RecoveryEpoch) => mountedRef.current && recoveryCoordinator.isCurrent(epoch),
    [recoveryCoordinator],
  )

  const requireCurrentRecovery = useCallback(
    (epoch: RecoveryEpoch) => {
      if (!isRecoveryCurrent(epoch)) throw new RecoverySupersededError()
    },
    [isRecoveryCurrent],
  )

  const claimOidcOwnership = useCallback((owner: IdentityTokenOwner | false) => {
    oidcOwnershipGenerationRef.current += 1
    oidcOwnershipRef.current = owner === 'oidc'
  }, [])

  const acceptIdentityToken = useCallback(
    async (token: string, owner: IdentityTokenOwner, epoch: RecoveryEpoch) => {
      requireCurrentRecovery(epoch)
      claimOidcOwnership(owner)
      setRuntimeIdentityToken(token)
      const nextIdentity = await getCurrentIdentity()
      requireCurrentRecovery(epoch)
      claimOidcOwnership(isOidcIdentity(nextIdentity.auth_method) ? 'oidc' : false)
      setIdentity(nextIdentity)
      setStatus('authenticated')
      setRecoveryError(null)
      setRecoveryMode(null)
      setDevelopmentSession(false)
      return nextIdentity
    },
    [claimOidcOwnership, requireCurrentRecovery],
  )

  const clearLocalIdentity = useCallback(() => {
    claimOidcOwnership(false)
    recoveryCoordinator.invalidate()
    clearIdentityToken()
    setDevelopmentSession(false)
    setIdentity(null)
    setRecoveryError(null)
    setRecoveryMode(null)
    setStatus('anonymous')
  }, [claimOidcOwnership, recoveryCoordinator])

  const removeOidcUser = useCallback((): Promise<boolean> => {
    if (oidcRemovalPromiseRef.current) return oidcRemovalPromiseRef.current
    const manager = getOidcManager()
    if (!manager) return Promise.resolve(true)
    oidcRemovalInProgressRef.current = true
    const promise = Promise.resolve()
      .then(() => manager.removeUser())
      .then(() => true)
      .catch(() => false)
      .finally(() => {
        if (oidcRemovalPromiseRef.current === promise) {
          oidcRemovalPromiseRef.current = null
          oidcRemovalInProgressRef.current = false
        }
      })
    oidcRemovalPromiseRef.current = promise
    return promise
  }, [])

  const becomeAnonymous = useCallback(async () => {
    clearLocalIdentity()
    await removeOidcUser()
  }, [clearLocalIdentity, removeOidcUser])

  const markIdentityFailure = useCallback(
    async (error: unknown, epoch: RecoveryEpoch) => {
      const failureKind = classifyIdentityRecoveryError(error)
      if (failureKind === 'auth-rejection') {
        if (isRecoveryCurrent(epoch)) await becomeAnonymous()
        else if (oidcRemovalPromiseRef.current) await oidcRemovalPromiseRef.current
        throw error
      }

      requireCurrentRecovery(epoch)
      setIdentity(null)
      setStatus('recovering')
      if (failureKind === 'incompatible') {
        const statusCode = getApiErrorStatus(error)
        setRecoveryMode('incompatible')
        setRecoveryError(
          isFleetAuthError(error)
            ? '传输凭据不一致。登录状态已保留，请检查部署配置后重新检查。'
            : `身份校验返回 HTTP ${statusCode ?? '4xx'}。请确认前端与 API 版本一致，然后重新检查。`,
        )
        return false
      }

      setRecoveryMode('service')
      setRecoveryError(error instanceof Error ? error.message : 'API 暂时不可用')
      return true
    },
    [becomeAnonymous, isRecoveryCurrent, requireCurrentRecovery],
  )

  const recoverIdentityToken = useCallback(
    (token: string, owner: IdentityTokenOwner, epoch?: RecoveryEpoch): Promise<void> =>
      recoveryCoordinator.run(
        token,
        async (operationEpoch) => {
          const maxIdentityValidationAttempts = 3
          let incompatibleFailure = false
          const result = await retryIdentityValidation(
            async () => {
              await acceptIdentityToken(token, owner, operationEpoch)
            },
            (error) => {
              incompatibleFailure = classifyIdentityRecoveryError(error) === 'incompatible'
              return markIdentityFailure(error, operationEpoch)
            },
            async () => {
              const liveness = await waitForApiLiveness(
                async () => {
                  const health = await getHealth()
                  return health.status === 'ok'
                },
                { isCancelled: () => !isRecoveryCurrent(operationEpoch) },
              )
              if (liveness !== 'cancelled') requireCurrentRecovery(operationEpoch)
              return liveness
            },
            maxIdentityValidationAttempts,
          )
          if (result === 'cancelled') throw new RecoverySupersededError()
          if (result === 'timeout') {
            setRecoveryError('未能在限定时间内确认 API 恢复。登录状态仍已保留。')
          } else if (result === 'stopped' && !incompatibleFailure) {
            setRecoveryError('身份校验仍不可用。登录状态已保留，请稍后重新检查。')
          }
        },
        epoch,
      ),
    [
      acceptIdentityToken,
      isRecoveryCurrent,
      markIdentityFailure,
      recoveryCoordinator,
      requireCurrentRecovery,
    ],
  )

  const cleanupStaleOidcRenewal = useCallback(
    async (manager: NonNullable<ReturnType<typeof getOidcManager>>, token: string) => {
      if (oidcRemovalInProgressRef.current) {
        await removeOidcUser()
        return
      }
      const cleanupGeneration = oidcOwnershipGenerationRef.current
      const storedUser = await manager.getUser().catch(() => null)
      if (
        cleanupGeneration === oidcOwnershipGenerationRef.current &&
        storedUser?.id_token === token &&
        getIdentityAccessToken() !== token
      ) {
        await removeOidcUser()
      }
    },
    [removeOidcUser],
  )

  const renewOidcSession = useCallback((): Promise<void> => {
    if (oidcRenewPromiseRef.current) return oidcRenewPromiseRef.current
    if (!oidcOwnershipRef.current || oidcRemovalInProgressRef.current) return Promise.resolve()
    const manager = getOidcManager()
    if (!manager) return Promise.resolve()

    const epoch = recoveryCoordinator.beginEpoch()
    const ownershipGeneration = oidcOwnershipGenerationRef.current
    const promise = manager
      .signinSilent()
      .then(async (user) => {
        if (!user?.id_token) return
        const current =
          mountedRef.current &&
          recoveryCoordinator.isCurrent(epoch) &&
          shouldAcceptOidcRenewal(
            oidcOwnershipRef.current,
            oidcRemovalInProgressRef.current,
            ownershipGeneration,
            oidcOwnershipGenerationRef.current,
          )
        if (!current) {
          await cleanupStaleOidcRenewal(manager, user.id_token)
          return
        }
        await recoverIdentityToken(user.id_token, 'oidc', epoch)
      })
      .catch((error) => {
        if (error instanceof RecoverySupersededError) return
        if (mountedRef.current && recoveryCoordinator.isCurrent(epoch)) {
          setStatus('recovering')
          setRecoveryMode('service')
          setRecoveryError(error instanceof Error ? error.message : 'OIDC 会话续期暂时不可用。')
        }
      })
      .finally(() => {
        if (oidcRenewPromiseRef.current === promise) oidcRenewPromiseRef.current = null
      })
    oidcRenewPromiseRef.current = promise
    return promise
  }, [cleanupStaleOidcRenewal, recoverIdentityToken, recoveryCoordinator])

  const restoreSession = useCallback(
    async (epoch: RecoveryEpoch) => {
      let oidcUser: { expired?: boolean; id_token?: string } | null = null
      try {
        oidcUser = (await getOidcManager()?.getUser()) ?? null
      } catch (error) {
        if (isRecoveryCurrent(epoch)) {
          setRecoveryError(error instanceof Error ? error.message : '无法读取 OIDC 会话存储。')
        }
      }

      requireCurrentRecovery(epoch)
      const storedOidcState = classifyStoredOidcUser(oidcUser)
      if (storedOidcState === 'expired' || storedOidcState === 'invalid') {
        claimOidcOwnership(false)
        await removeOidcUser()
        requireCurrentRecovery(epoch)
        oidcUser = null
      }
      if (oidcUser) {
        claimOidcOwnership('oidc')
        try {
          await recoverIdentityToken(oidcUser.id_token!, 'oidc', epoch)
        } catch (error) {
          if (!isAuthRejection(error) && !(error instanceof RecoverySupersededError)) throw error
        }
        return
      }

      const bootstrapToken = getBootstrapIdentityToken()
      if (bootstrapToken) {
        claimOidcOwnership(false)
        try {
          await recoverIdentityToken(bootstrapToken, 'bootstrap', epoch)
        } catch (error) {
          if (!isAuthRejection(error) && !(error instanceof RecoverySupersededError)) throw error
        }
        return
      }

      requireCurrentRecovery(epoch)
      claimOidcOwnership(false)
      if (developmentLoginEnabled && hasDevelopmentSession()) {
        setRecoveryError(null)
        setRecoveryMode(null)
        setIdentity(DEVELOPMENT_IDENTITY)
        setStatus('authenticated')
        return
      }

      setRecoveryError(null)
      setRecoveryMode(null)
      setIdentity(null)
      setStatus('anonymous')
    },
    [
      claimOidcOwnership,
      developmentLoginEnabled,
      isRecoveryCurrent,
      removeOidcUser,
      recoverIdentityToken,
      requireCurrentRecovery,
    ],
  )

  const retrySession = useCallback(() => {
    if (restorePromiseRef.current) return restorePromiseRef.current
    const epoch = recoveryCoordinator.beginEpoch()
    if (mountedRef.current) {
      setRecoveryPending(true)
      setRecoveryError(null)
    }
    const promise = restoreSession(epoch)
      .catch((error) => {
        if (!(error instanceof RecoverySupersededError)) throw error
      })
      .finally(() => {
        if (restorePromiseRef.current === promise) {
          restorePromiseRef.current = null
          if (mountedRef.current) setRecoveryPending(false)
        }
      })
    restorePromiseRef.current = promise
    return promise
  }, [recoveryCoordinator, restoreSession])

  useEffect(() => {
    mountedRef.current = true
    void retrySession()
    return () => {
      mountedRef.current = false
      recoveryCoordinator.invalidate()
      restorePromiseRef.current = null
    }
  }, [recoveryCoordinator, retrySession])

  useEffect(() => {
    const manager = getOidcManager()
    if (!manager) return

    const onUserUnloaded = () => {
      const eventGeneration = oidcOwnershipGenerationRef.current
      if (!shouldClearIdentityForOidcUnload(oidcOwnershipRef.current, oidcRemovalInProgressRef.current)) return
      void manager
        .getUser()
        .then((storedUser) => {
          if (
            shouldClearIdentityForOidcUnload(
              oidcOwnershipRef.current,
              oidcRemovalInProgressRef.current,
              eventGeneration,
              oidcOwnershipGenerationRef.current,
              Boolean(storedUser?.id_token),
            )
          ) {
            clearLocalIdentity()
          }
        })
        .catch(() => undefined)
    }
    const onAccessTokenExpiring = () => {
      void renewOidcSession()
    }
    const onAccessTokenExpired = () => {
      if (!oidcOwnershipRef.current) return
      void becomeAnonymous()
    }

    manager.events.addUserUnloaded(onUserUnloaded)
    manager.events.addAccessTokenExpiring(onAccessTokenExpiring)
    manager.events.addAccessTokenExpired(onAccessTokenExpired)
    return () => {
      manager.events.removeUserUnloaded(onUserUnloaded)
      manager.events.removeAccessTokenExpiring(onAccessTokenExpiring)
      manager.events.removeAccessTokenExpired(onAccessTokenExpired)
    }
  }, [becomeAnonymous, clearLocalIdentity, renewOidcSession])

  useEffect(() => {
    const onAuthRequired = (event: Event) => {
      const eventGeneration = (event as CustomEvent<AuthRequiredEventDetail>).detail
        ?.identityGeneration
      if (!shouldInvalidateIdentityForGeneration(eventGeneration, getIdentityGeneration())) return
      if (developmentLoginEnabled && hasDevelopmentSession()) return
      void becomeAnonymous()
    }
    window.addEventListener(AUTH_REQUIRED_EVENT, onAuthRequired)
    return () => window.removeEventListener(AUTH_REQUIRED_EVENT, onAuthRequired)
  }, [becomeAnonymous, developmentLoginEnabled])

  const signInWithOidc = useCallback(
    async (returnTo = '/launch') => {
      const manager = getOidcManager()
      if (!manager) throw new Error('OIDC 登录尚未配置')
      recoveryCoordinator.beginEpoch()
      await manager.signinRedirect({ state: { returnTo: sanitizeReturnTo(returnTo) } })
    },
    [recoveryCoordinator],
  )

  const signInWithPassword = useCallback(
    async (username: string, password: string) => {
      const epoch = recoveryCoordinator.beginEpoch()
      const result = await loginWithPassword(username, password)
      requireCurrentRecovery(epoch)
      claimOidcOwnership(false)
      const oidcRemoved = await removeOidcUser()
      requireCurrentRecovery(epoch)
      if (!oidcRemoved) throw new Error('无法清理旧 OIDC 会话，本地管理员登录未保存。请重试。')
      persistBootstrapIdentityToken(result.access_token)
      await recoverIdentityToken(result.access_token, 'bootstrap', epoch)
      return result.using_default_password
    },
    [
      claimOidcOwnership,
      recoverIdentityToken,
      recoveryCoordinator,
      removeOidcUser,
      requireCurrentRecovery,
    ],
  )

  const changePassword = useCallback(async (currentPassword: string, newPassword: string) => {
    await changeLocalPassword(currentPassword, newPassword)
  }, [])


  const completeOidcSignIn = useCallback(async () => {
    const manager = getOidcManager()
    if (!manager) throw new Error('OIDC 登录尚未配置')
    const epoch = recoveryCoordinator.beginEpoch()
    const user = await manager.signinRedirectCallback()
    requireCurrentRecovery(epoch)
    if (!user.id_token) throw new Error('OIDC 未返回身份令牌')
    claimOidcOwnership('oidc')
    await recoverIdentityToken(user.id_token, 'oidc', epoch)
    requireCurrentRecovery(epoch)
    return oidcReturnTo(user)
  }, [claimOidcOwnership, recoverIdentityToken, recoveryCoordinator, requireCurrentRecovery])

  const enterDevelopmentMode = useCallback(() => {
    if (!developmentLoginEnabled) throw new Error('本地开发模式不可用')
    recoveryCoordinator.beginEpoch()
    claimOidcOwnership(false)
    clearIdentityToken()
    setDevelopmentSession(true)
    setIdentity(DEVELOPMENT_IDENTITY)
    setRecoveryError(null)
    setRecoveryMode(null)
    setStatus('authenticated')
  }, [claimOidcOwnership, developmentLoginEnabled, recoveryCoordinator])

  const signOut = useCallback(async () => {
    const manager = getOidcManager()
    const shouldSignOutFromIdentityProvider = isOidcIdentity(identity?.auth_method)
    const oidcUserPromise = shouldSignOutFromIdentityProvider ? manager?.getUser() : undefined
    clearLocalIdentity()
    const localSignOutGeneration = oidcOwnershipGenerationRef.current
    let oidcUser = null
    try {
      oidcUser = (await oidcUserPromise) ?? null
    } catch {
      // Continue with local OIDC cleanup even when the stored user cannot be decoded.
    }
    if (!manager) return
    if (oidcOwnershipGenerationRef.current !== localSignOutGeneration) return
    if (!shouldSignOutFromIdentityProvider || !oidcUser) {
      await removeOidcUser()
      return
    }
    try {
      oidcRemovalInProgressRef.current = true
      await manager.signoutRedirect({ id_token_hint: oidcUser.id_token })
    } catch {
      await removeOidcUser()
    } finally {
      oidcRemovalInProgressRef.current = false
    }
  }, [clearLocalIdentity, identity?.auth_method, removeOidcUser])

  const value = useMemo<AuthContextValue>(
    () => ({
      status,
      identity,
      recoveryError,
      recoveryMode,
      recoveryPending,
      oidcEnabled,
      developmentLoginEnabled,
      signInWithOidc,
      signInWithPassword,
      changePassword,
      completeOidcSignIn,
      enterDevelopmentMode,
      retrySession,
      signOut,
    }),
    [
      changePassword,
      completeOidcSignIn,
      developmentLoginEnabled,
      enterDevelopmentMode,
      identity,
      oidcEnabled,
      recoveryError,
      recoveryMode,
      recoveryPending,
      retrySession,
      signInWithOidc,
      signInWithPassword,
      signOut,
      status,
    ],
  )

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth(): AuthContextValue {
  const context = useContext(AuthContext)
  if (!context) throw new Error('useAuth must be used within AuthProvider')
  return context
}
