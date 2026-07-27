const rules = [
  ['length', '至少 8 位'],
  ['uppercase', '大写字母'],
  ['lowercase', '小写字母'],
  ['number', '数字'],
]

export function PasswordRules({ checks }) {
  return (
    <div className="password-rules" aria-live="polite">
      {rules.map(([key, label]) => (
        <span key={key} className={checks[key] ? 'met' : ''}>{label}</span>
      ))}
    </div>
  )
}
