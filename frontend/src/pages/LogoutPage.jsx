import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { LoadingScreen } from '../components/feedback/LoadingScreen'
import { useAuth } from '../context/AuthContext'

export function LogoutPage() {
  const navigate = useNavigate()
  const { signOut } = useAuth()
  const [message, setMessage] = useState('正在安全退出…')

  useEffect(() => {
    let active = true
    let redirectTimer

    signOut()
      .catch(() => {
        if (active) setMessage('本地会话已清理')
      })
      .finally(() => {
        redirectTimer = window.setTimeout(() => {
          navigate('/login?loggedOut=1', { replace: true })
        }, 450)
      })

    return () => {
      active = false
      window.clearTimeout(redirectTimer)
    }
  }, [navigate, signOut])

  return <LoadingScreen label={message} spinner />
}
