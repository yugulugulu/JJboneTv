import { BrandMark } from './BrandMark'

export function AuthVisual() {
  return (
    <section className="auth-visual" aria-label="ThinkAI Studio 品牌展示">
      <div className="auth-visual-shade" />

      <header className="auth-brand">
        <BrandMark />
        <span>ThinkAI Studio</span>
      </header>

      <div className="auth-story">
        <p className="eyebrow">CREATE BEYOND THE FRAME</p>
        <h1>
          让每一个想象，<br />抵达画面。
        </h1>
        <p>从一段描述开始，生成图片、视频与下一次灵感。</p>
      </div>

      <p className="auth-caption">AI IMAGE · VIDEO · CREATIVE CANVAS</p>
    </section>
  )
}
