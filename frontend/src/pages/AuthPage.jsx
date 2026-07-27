import { useCallback } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { AuthForm } from '../components/auth/AuthForm'
import { AuthHeading } from '../components/auth/AuthHeading'
import { AuthTabs } from '../components/auth/AuthTabs'
import { AuthVisual } from '../components/brand/AuthVisual'
import { MobileBrand } from '../components/brand/MobileBrand'
import { useAuth } from '../context/AuthContext'

export function AuthPage({ mode }) {
  const navigate = useNavigate()
  const [searchParams] = useSearchParams()
  const { signIn, signUp } = useAuth()

  const redirect = searchParams.get('redirect') || '/'
  const loggedOut = searchParams.get('loggedOut') === '1'

  const changeMode = useCallback((nextMode) => {
    if (nextMode === mode) return
    const params = new URLSearchParams()
    if (redirect !== '/') params.set('redirect', redirect)
    const query = params.toString()
    navigate(`/${nextMode === 'register' ? 'register' : 'login'}${query ? `?${query}` : ''}`, {
      replace: true,
    })
  }, [mode, navigate, redirect])

  const submit = useCallback(async (credentials) => {
    if (mode === 'register') {
      await signUp(credentials)
    } else {
      await signIn(credentials)
    }
    navigate(redirect, { replace: true })
  }, [mode, navigate, redirect, signIn, signUp])

  return (
    <main className="auth-screen">
      <AuthVisual />

      <section className="auth-panel">
        <div className="auth-form-wrap">
          <MobileBrand />
          <AuthHeading mode={mode} />
          <AuthTabs mode={mode} onChange={changeMode} />
          <AuthForm mode={mode} onSubmit={submit} loggedOut={loggedOut} />
          <p className="auth-legal">继续即代表你同意 ThinkAI 的服务条款与隐私政策。</p>
        </div>
      </section>
    </main>
  )
}
