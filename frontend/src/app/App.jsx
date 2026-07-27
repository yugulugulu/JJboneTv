import { Navigate, Route, Routes } from 'react-router-dom'
import { ProtectedRoute } from '../router/ProtectedRoute'
import { PublicOnlyRoute } from '../router/PublicOnlyRoute'
import { AuthPage } from '../pages/AuthPage'
import { LogoutPage } from '../pages/LogoutPage'
import { StudioPage } from '../pages/StudioPage'
import { ModelSettingsPage } from '../pages/ModelSettingsPage'

export function App() {
  return (
    <Routes>
      <Route element={<ProtectedRoute />}>
        <Route path="/" element={<StudioPage />} />
        <Route path="/projects" element={<StudioPage />} />
        <Route path="/projects/:projectId" element={<StudioPage />} />
        <Route path="/settings/models" element={<ModelSettingsPage />} />
      </Route>

      <Route element={<PublicOnlyRoute />}>
        <Route path="/login" element={<AuthPage mode="login" />} />
        <Route path="/register" element={<AuthPage mode="register" />} />
      </Route>

      <Route path="/logout" element={<LogoutPage />} />
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  )
}
