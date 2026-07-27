export function AuthTabs({ mode, onChange }) {
  return (
    <div className="auth-tabs" role="tablist" aria-label="账户操作">
      <button
        type="button"
        role="tab"
        aria-selected={mode === 'login'}
        className={mode === 'login' ? 'active' : ''}
        onClick={() => onChange('login')}
      >
        登录
      </button>
      <button
        type="button"
        role="tab"
        aria-selected={mode === 'register'}
        className={mode === 'register' ? 'active' : ''}
        onClick={() => onChange('register')}
      >
        注册
      </button>
    </div>
  )
}
