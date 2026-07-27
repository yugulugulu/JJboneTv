import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react'
import { authService } from '../services/authService'
import { AUTH_UNAUTHORIZED_EVENT } from '../services/httpClient'

const AuthContext = createContext(null)

export function AuthProvider({ children }) {
  const [user, setUser] = useState(null)
  const [status, setStatus] = useState('loading')

  const clearSession = useCallback(() => {
    setUser(null)
    setStatus('ready')
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
    return currentUser
  }, [])

  const signUp = useCallback(async (credentials) => {
    const currentUser = await authService.register(credentials)
    setUser(currentUser)
    return currentUser
  }, [])

  const signOut = useCallback(async () => {
    try {
      await authService.logout()
    } finally {
      clearSession()
    }
  }, [clearSession])

  const value = useMemo(
    () => ({ user, status, signIn, signUp, signOut, clearSession }),
    [user, status, signIn, signUp, signOut, clearSession],
  )

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth() {
  const context = useContext(AuthContext)
  if (!context) throw new Error('useAuth must be used within AuthProvider')
  return context
}
