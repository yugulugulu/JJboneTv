import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react'
import { authService } from '../services/authService'
import { AUTH_UNAUTHORIZED_EVENT } from '../services/httpClient'
import { modelService } from '../services/modelService'

const AuthContext = createContext(null)

export function AuthProvider({ children }) {
  const [user, setUser] = useState(null)
  const [status, setStatus] = useState('loading')
  const [showZeroKeyPrompt, setShowZeroKeyPrompt] = useState(false)
  const credentialCheckSequence = useRef(0)
  const promptedInSession = useRef(false)

  const clearSession = useCallback(() => {
    credentialCheckSequence.current += 1
    promptedInSession.current = false
    setShowZeroKeyPrompt(false)
    setUser(null)
    setStatus('ready')
  }, [])

  const checkCredentialsAfterAuthentication = useCallback(() => {
    const sequence = ++credentialCheckSequence.current
    promptedInSession.current = false
    setShowZeroKeyPrompt(false)

    modelService
      .listGroups()
      .then((payload) => {
        if (sequence !== credentialCheckSequence.current || promptedInSession.current) return
        const groups = Array.isArray(payload?.items) ? payload.items : []
        if (groups.length > 0 && groups.every((group) => group?.credential_configured === false)) {
          promptedInSession.current = true
          setShowZeroKeyPrompt(true)
        }
      })
      .catch(() => {
        // 登录不应被凭据提示检查阻断，请求失败时保持安静并允许用户正常使用。
      })
  }, [])

  const dismissZeroKeyPrompt = useCallback(() => {
    promptedInSession.current = true
    setShowZeroKeyPrompt(false)
  }, [])

  useEffect(() => {
    window.addEventListener(AUTH_UNAUTHORIZED_EVENT, clearSession)
    return () => window.removeEventListener(AUTH_UNAUTHORIZED_EVENT, clearSession)
  }, [clearSession])

  useEffect(() => {
    let active = true

    authService
      .getCurrentUser()
      .then((currentUser) => {
        if (active) setUser(currentUser)
      })
      .catch(() => {
        if (active) setUser(null)
      })
      .finally(() => {
        if (active) setStatus('ready')
      })

    return () => {
      active = false
    }
  }, [])

  const signIn = useCallback(async (credentials) => {
    const currentUser = await authService.login(credentials)
    setUser(currentUser)
    checkCredentialsAfterAuthentication()
    return currentUser
  }, [checkCredentialsAfterAuthentication])

  const signUp = useCallback(async (credentials) => {
    const currentUser = await authService.register(credentials)
    setUser(currentUser)
    checkCredentialsAfterAuthentication()
    return currentUser
  }, [checkCredentialsAfterAuthentication])

  const signOut = useCallback(async () => {
    try {
      await authService.logout()
    } finally {
      clearSession()
    }
  }, [clearSession])

  const value = useMemo(
    () => ({
      user,
      status,
      showZeroKeyPrompt,
      signIn,
      signUp,
      signOut,
      clearSession,
      dismissZeroKeyPrompt,
    }),
    [
      user,
      status,
      showZeroKeyPrompt,
      signIn,
      signUp,
      signOut,
      clearSession,
      dismissZeroKeyPrompt,
    ],
  )

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth() {
  const context = useContext(AuthContext)
  if (!context) throw new Error('useAuth must be used within AuthProvider')
  return context
}
