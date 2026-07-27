import { useEffect, useState } from 'react'
import { LockKeyhole, Mail } from 'lucide-react'
import { useAuthForm } from '../../hooks/useAuthForm'
import { FormField } from './FormField'
import { PasswordRules } from './PasswordRules'
import { PasswordToggle } from './PasswordToggle'
import { SubmitButton } from './SubmitButton'

export function AuthForm({ mode, onSubmit, loggedOut }) {
  const [passwordVisible, setPasswordVisible] = useState(false)
  const form = useAuthForm({ mode, onSubmit })

  useEffect(() => {
    if (!form.passwordResetVersion) return
    setPasswordVisible(false)
    window.requestAnimationFrame(() => document.getElementById('password')?.focus())
  }, [form.passwordResetVersion])

  const emailError = form.touched.email && !form.emailValid
    ? '请输入有效的邮箱地址'
    : ''
  const passwordError = mode === 'login' && form.touched.password && !form.passwordValid
    ? '请输入密码'
    : ''

  return (
    <form noValidate onSubmit={form.submit}>
      <FormField
        id="email"
        name="email"
        label="邮箱"
        icon={Mail}
        type="email"
        inputMode="email"
        autoComplete="email"
        placeholder="name@example.com"
        value={form.values.email}
        error={emailError}
        onChange={form.updateField}
        onBlur={form.touchField}
      />

      <FormField
        id="password"
        name="password"
        className="password-control"
        label="密码"
        icon={LockKeyhole}
        type={passwordVisible ? 'text' : 'password'}
        autoComplete={mode === 'register' ? 'new-password' : 'current-password'}
        placeholder={mode === 'register' ? '设置安全密码' : '输入密码'}
        value={form.values.password}
        error={passwordError}
        onChange={form.updateField}
        onBlur={form.touchField}
        trailing={
          <PasswordToggle
            visible={passwordVisible}
            onToggle={() => setPasswordVisible((visible) => !visible)}
          />
        }
      />

      {mode === 'register' && <PasswordRules checks={form.passwordChecks} />}
      {form.submitError && <p className="submit-error" role="alert">{form.submitError}</p>}
      {form.retrySeconds > 0 && (
        <p className="rate-limit-status" role="status" aria-live="polite">
          请在 {form.retrySeconds} 秒后重新输入密码并重试
        </p>
      )}
      {loggedOut && !form.submitError && <p className="logout-note">你已安全退出账户</p>}

      <SubmitButton
        mode={mode}
        submitting={form.submitting}
        retrySeconds={form.retrySeconds}
        disabled={!form.formValid || form.retrySeconds > 0}
      />
    </form>
  )
}
