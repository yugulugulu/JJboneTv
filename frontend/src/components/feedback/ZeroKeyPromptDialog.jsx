import { useEffect, useRef } from 'react'
import { ArrowRight, KeyRound, X } from 'lucide-react'
import { useNavigate } from 'react-router-dom'
import { useAuth } from '../../context/AuthContext'

const FOCUSABLE_SELECTOR = [
  'button:not([disabled])',
  'a[href]',
  'input:not([disabled])',
  'select:not([disabled])',
  'textarea:not([disabled])',
  '[tabindex]:not([tabindex="-1"])',
].join(',')

export function ZeroKeyPromptDialog() {
  const { showZeroKeyPrompt, dismissZeroKeyPrompt } = useAuth()
  const dialogRef = useRef(null)
  const configureButtonRef = useRef(null)
  const navigate = useNavigate()

  useEffect(() => {
    if (!showZeroKeyPrompt) return undefined

    const previousFocus = document.activeElement
    const previousOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    window.requestAnimationFrame(() => configureButtonRef.current?.focus())

    return () => {
      document.body.style.overflow = previousOverflow
      if (previousFocus instanceof HTMLElement) previousFocus.focus()
    }
  }, [showZeroKeyPrompt])

  if (!showZeroKeyPrompt) return null

  const handleKeyDown = (event) => {
    if (event.key === 'Escape') {
      event.preventDefault()
      dismissZeroKeyPrompt()
      return
    }
    if (event.key !== 'Tab') return

    const focusable = [...dialogRef.current.querySelectorAll(FOCUSABLE_SELECTOR)]
    if (!focusable.length) return
    const first = focusable[0]
    const last = focusable.at(-1)
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault()
      last.focus()
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault()
      first.focus()
    }
  }

  const openSettings = () => {
    dismissZeroKeyPrompt()
    navigate('/settings/models')
  }

  return (
    <div
      className="zero-key-overlay"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) dismissZeroKeyPrompt()
      }}
    >
      <section
        ref={dialogRef}
        className="zero-key-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="zero-key-title"
        aria-describedby="zero-key-description"
        onKeyDown={handleKeyDown}
      >
        <button
          className="zero-key-close"
          type="button"
          aria-label="关闭 API Key 配置提示"
          onClick={dismissZeroKeyPrompt}
        >
          <X size={16} aria-hidden="true" />
        </button>

        <div className="zero-key-icon" aria-hidden="true">
          <KeyRound size={22} />
        </div>
        <span className="zero-key-eyebrow">开始创作前</span>
        <h2 id="zero-key-title">配置你的 API Key</h2>
        <p id="zero-key-description">
          点击右上角“配置”，根据相应分组，填写你的 Thinkai API Key，即可开始使用生图功能！
        </p>

        <div className="zero-key-actions">
          <button className="zero-key-secondary" type="button" onClick={dismissZeroKeyPrompt}>
            稍后配置
          </button>
          <button ref={configureButtonRef} className="zero-key-primary" type="button" onClick={openSettings}>
            去配置
            <ArrowRight size={15} aria-hidden="true" />
          </button>
        </div>
      </section>
    </div>
  )
}
