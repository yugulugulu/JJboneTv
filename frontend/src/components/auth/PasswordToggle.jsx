import { Eye, EyeOff } from 'lucide-react'

export function PasswordToggle({ visible, onToggle }) {
  const Icon = visible ? EyeOff : Eye

  return (
    <button
      className="password-toggle"
      type="button"
      aria-label={visible ? '隐藏密码' : '显示密码'}
      onClick={onToggle}
    >
      <Icon size={18} aria-hidden="true" />
    </button>
  )
}
