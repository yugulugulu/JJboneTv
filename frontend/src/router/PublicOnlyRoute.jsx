import { Navigate, Outlet } from 'react-router-dom'
import { LoadingScreen } from '../components/feedback/LoadingScreen'
import { useAuth } from '../context/AuthContext'

export function PublicOnlyRoute() {
  const { user, status } = useAuth()

  if (status === 'loading') return <LoadingScreen label="正在验证会话" />
  return user ? <Navigate to="/" replace /> : <Outlet />
}
