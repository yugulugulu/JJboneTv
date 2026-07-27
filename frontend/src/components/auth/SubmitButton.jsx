import { ArrowRight } from 'lucide-react'

export function SubmitButton({ mode, submitting, retrySeconds = 0, disabled }) {
  const label = retrySeconds > 0
    ? `请在 ${retrySeconds} 秒后重试`
    : submitting
      ? '请稍候…'
      : mode === 'register' ? '创建账户' : '进入工作台'

  return (
    <button className="submit-button" type="submit" disabled={disabled || submitting}>
      <span>{label}</span>
      {!submitting && retrySeconds === 0 && <ArrowRight size={18} aria-hidden="true" />}
    </button>
  )
}
