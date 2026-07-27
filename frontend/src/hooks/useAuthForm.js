import { useEffect, useMemo, useState } from 'react'
import { EMAIL_PATTERN, getPasswordChecks, isStrongPassword } from '../utils/authValidation'

const initialTouched = { email: false, password: false }

export function useAuthForm({ mode, onSubmit }) {
  const [values, setValues] = useState({ email: '', password: '' })
  const [touched, setTouched] = useState(initialTouched)
  const [submitting, setSubmitting] = useState(false)
  const [submitError, setSubmitError] = useState('')
  const [rateLimitUntil, setRateLimitUntil] = useState(0)
  const [clock, setClock] = useState(Date.now())
  const [passwordResetVersion, setPasswordResetVersion] = useState(0)

  const retrySeconds = rateLimitUntil > clock
    ? Math.max(1, Math.ceil((rateLimitUntil - clock) / 1000))
    : 0

  useEffect(() => {
    if (!rateLimitUntil) return undefined
    const tick = () => {
      const now = Date.now()
      setClock(now)
      if (now >= rateLimitUntil) setRateLimitUntil(0)
    }
    tick()
    const timer = window.setInterval(tick, 250)
    return () => window.clearInterval(timer)
  }, [rateLimitUntil])

  const emailValid = EMAIL_PATTERN.test(values.email.trim())
  const passwordChecks = useMemo(() => getPasswordChecks(values.password), [values.password])
  const passwordValid = mode === 'register'
    ? isStrongPassword(values.password)
    : values.password.length > 0
  const formValid = emailValid && passwordValid

  function updateField(event) {
    const { name, value } = event.target
    setValues((current) => ({ ...current, [name]: value }))
    setSubmitError('')
  }

  function touchField(event) {
    const { name } = event.target
    setTouched((current) => ({ ...current, [name]: true }))
  }

  async function submit(event) {
    event.preventDefault()
    setTouched({ email: true, password: true })
    setSubmitError('')
    if (!formValid || submitting || retrySeconds > 0) return

    setSubmitting(true)
    try {
      await onSubmit({
        email: values.email.trim().toLowerCase(),
        password: values.password,
      })
    } catch (error) {
      const rateLimited = error?.status === 429
        && ['AUTH_RATE_LIMITED', 'AUTH_RATE_LIMIT_EXCEEDED', null, undefined].includes(error?.code)
      if (rateLimited) {
        const waitSeconds = Math.max(1, error.retryAfterSeconds || 60)
        const now = Date.now()
        setClock(now)
        setRateLimitUntil(now + waitSeconds * 1000)
        setValues((current) => ({ ...current, password: '' }))
        setTouched((current) => ({ ...current, password: false }))
        setPasswordResetVersion((version) => version + 1)
        setSubmitError('操作过于频繁，请稍后再试。')
      } else {
        setSubmitError(error.message)
      }
    } finally {
      setSubmitting(false)
    }
  }

  return {
    values,
    touched,
    submitting,
    submitError,
    retrySeconds,
    passwordResetVersion,
    emailValid,
    passwordValid,
    passwordChecks,
    formValid,
    updateField,
    touchField,
    submit,
  }
}
