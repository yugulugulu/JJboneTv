import { BrandMark } from '../brand/BrandMark'

export function LoadingScreen({ label = '正在进入 ThinkAI Studio', spinner = false }) {
  return (
    <main className="loading-screen" aria-live="polite">
      <BrandMark />
      {spinner && <span className="loading-spinner" aria-hidden="true" />}
      <p>{label}</p>
    </main>
  )
}
