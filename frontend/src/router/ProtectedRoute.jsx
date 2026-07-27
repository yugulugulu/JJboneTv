import { Navigate, Outlet, useLocation } from 'react-router-dom'
import { LoadingScreen } from '../components/feedback/LoadingScreen'
import { useAuth } from '../context/AuthContext'

export function ProtectedRoute() {
  const { user, status } = useAuth()
  const location = useLocation()

  if (status === 'loading') return <LoadingScreen label="正在验证会话" />
  if (user) return <Outlet />

  const redirect = `${location.pathname}${location.search}`
  return <Navigate to={`/login?redirect=${encodeURIComponent(redirect)}`} replace />
}
