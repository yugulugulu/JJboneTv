const content = {
  login: {
    eyebrow: 'WELCOME BACK',
    title: '登录创作空间',
    description: '继续你的图片与视频创作。',
  },
  register: {
    eyebrow: 'START CREATING',
    title: '创建你的账户',
    description: '只需邮箱和密码，即刻开启创作。',
  },
}

export function AuthHeading({ mode }) {
  const copy = content[mode]

  return (
    <div className="auth-heading" key={mode}>
      <p className="eyebrow">{copy.eyebrow}</p>
      <h2>{copy.title}</h2>
      <p>{copy.description}</p>
    </div>
  )
}
