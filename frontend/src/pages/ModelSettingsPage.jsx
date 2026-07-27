import { useCallback, useEffect, useRef, useState } from 'react'
import {
  ArrowLeft,
  Check,
  Eye,
  EyeOff,
  KeyRound,
  LoaderCircle,
  Monitor,
  Moon,
  RefreshCw,
  Save,
  Sun,
  Trash2,
  X,
} from 'lucide-react'
import { Link } from 'react-router-dom'
import { modelService } from '../services/modelService'

const THEME_STORAGE_KEY = 'thinkai-theme-mode'
const THEME_SYNC_CHANNEL = 'thinkai-theme-sync-v1'
const THEME_MODES = new Set(['dark', 'light', 'system'])

function items(payload) {
  return Array.isArray(payload?.items) ? payload.items : []
}

function readThemeMode(value = localStorage.getItem(THEME_STORAGE_KEY)) {
  return THEME_MODES.has(value) ? value : 'dark'
}

function CredentialRow({ group, onSaved, onDelete, registerFocusTarget }) {
  const [apiKey, setApiKey] = useState('')
  const [visible, setVisible] = useState(false)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const headingId = `credential-group-${group.id}`
  const registerHeading = useCallback(
    (element) => registerFocusTarget(group.id, 'heading', element),
    [group.id, registerFocusTarget],
  )
  const registerInput = useCallback(
    (element) => registerFocusTarget(group.id, 'input', element),
    [group.id, registerFocusTarget],
  )

  const save = async (event) => {
    event.preventDefault()
    const value = apiKey.trim()
    if (value.length < 4) { setError('请输入有效的 API Key'); return }
    if (/^bearer\s/i.test(value)) { setError('只需填写 API Key 本身，请移除 Bearer 前缀'); return }
    if (/[^\x21-\x7E]/.test(value)) { setError('API Key 只能包含英文、数字和 ASCII 符号'); return }
    setSaving(true)
    setError('')
    try {
      await modelService.saveCredential(group.id, value)
      setApiKey('')
      setVisible(false)
      await onSaved()
    } catch (requestError) {
      setError(requestError.message || 'API Key 保存失败')
    } finally {
      setSaving(false)
    }
  }

  return (
    <section className="credential-group" aria-labelledby={headingId}>
      <div className="credential-identity">
        <span className="credential-icon" aria-hidden="true"><KeyRound size={17} /></span>
        <div>
          <h2 ref={registerHeading} id={headingId} tabIndex={-1}>{group.name}</h2>
          <p>{group.description || '该分组下的模型共用一份 API Key。'}</p>
        </div>
      </div>

      <div className="credential-models" aria-label={`${group.name} 模型列表`}>
        <span className="credential-column-label">可用模型</span>
        {group.models.length ? group.models.map((model) => (
          <span className="credential-model" key={model.id}>
            <b>{model.display_name}</b>
            <small>{model.model_name}</small>
          </span>
        )) : <em>该分组暂时没有已启用模型</em>}
      </div>

      <form className="credential-form" onSubmit={save}>
        <div className="credential-status">
          {group.credential_configured ? (
            <>
              <span className="configured-dot" aria-hidden="true"><Check size={12} /></span>
              <strong>已配置</strong>
              <small>末四位 {group.api_key_last_four}</small>
            </>
          ) : (
            <>
              <span className="unconfigured-dot" aria-hidden="true" />
              <strong>未配置</strong>
              <small>配置后模型才会出现在 Studio</small>
            </>
          )}
        </div>
        <label>
          <span>{group.credential_configured ? '替换 API Key' : 'API Key'}</span>
          <div className="secret-input">
            <input
              ref={registerInput}
              type={visible ? 'text' : 'password'}
              value={apiKey}
              onChange={(event) => setApiKey(event.target.value)}
              autoComplete="off"
              spellCheck="false"
              placeholder={group.credential_configured ? '输入新 Key 以替换现有配置' : '输入服务商 API Key'}
            />
            <button
              type="button"
              aria-label={visible ? '隐藏 API Key' : '显示 API Key'}
              title={visible ? '隐藏' : '显示'}
              onClick={() => setVisible((current) => !current)}
            >
              {visible ? <EyeOff size={16} /> : <Eye size={16} />}
            </button>
          </div>
        </label>
        {error && <p className="credential-error" role="alert">{error}</p>}
        <div className="credential-actions">
          {group.credential_configured && (
            <button
              className="settings-danger-button"
              type="button"
              onClick={(event) => onDelete(group, event.currentTarget)}
            >
              <Trash2 size={15} />删除 Key
            </button>
          )}
          <button className="settings-primary-button" type="submit" disabled={saving || !apiKey.trim()}>
            {saving ? <LoaderCircle className="spin" size={15} /> : <Save size={15} />}
            {group.credential_configured ? '替换 Key' : '保存 Key'}
          </button>
        </div>
      </form>
    </section>
  )
}

export function ModelSettingsPage() {
  const [state, setState] = useState({ loading: true, refreshing: false, error: '', groups: [] })
  const [themeMode, setThemeMode] = useState(readThemeMode)
  const [systemLight, setSystemLight] = useState(() => window.matchMedia('(prefers-color-scheme: light)').matches)
  const [pendingDelete, setPendingDelete] = useState(null)
  const [deleting, setDeleting] = useState(false)
  const [deleteFocusGroupId, setDeleteFocusGroupId] = useState(null)
  const [notice, setNotice] = useState('')
  const requestSequence = useRef(0)
  const noticeTimer = useRef(null)
  const deleteTrigger = useRef(null)
  const dialog = useRef(null)
  const cancelDelete = useRef(null)
  const deletingRef = useRef(false)
  const groupFocusTargets = useRef(new Map())
  const themeChannel = useRef(null)
  deletingRef.current = deleting

  const registerGroupFocusTarget = useCallback((groupId, kind, element) => {
    const targets = groupFocusTargets.current.get(groupId) || {}
    if (element) {
      targets[kind] = element
      groupFocusTargets.current.set(groupId, targets)
      return
    }
    delete targets[kind]
    if (targets.input || targets.heading) groupFocusTargets.current.set(groupId, targets)
    else groupFocusTargets.current.delete(groupId)
  }, [])

  useEffect(() => {
    const media = window.matchMedia('(prefers-color-scheme: light)')
    const handleMediaChange = (event) => setSystemLight(event.matches)
    const handleStorage = (event) => {
      if (event.key === THEME_STORAGE_KEY) setThemeMode(readThemeMode(event.newValue))
    }
    media.addEventListener?.('change', handleMediaChange)
    window.addEventListener('storage', handleStorage)
    return () => {
      media.removeEventListener?.('change', handleMediaChange)
      window.removeEventListener('storage', handleStorage)
    }
  }, [])

  useEffect(() => {
    if (typeof BroadcastChannel === 'undefined') return undefined
    const channel = new BroadcastChannel(THEME_SYNC_CHANNEL)
    themeChannel.current = channel
    return () => {
      if (themeChannel.current === channel) themeChannel.current = null
      channel.close()
    }
  }, [])

  useEffect(() => {
    const resolved = themeMode === 'system' ? (systemLight ? 'light' : 'dark') : themeMode
    localStorage.setItem(THEME_STORAGE_KEY, themeMode)
    document.documentElement.dataset.theme = resolved
    document.documentElement.dataset.themeMode = themeMode
    themeChannel.current?.postMessage({ type: 'thinktv:theme', mode: themeMode, resolved })
  }, [systemLight, themeMode])

  useEffect(() => () => window.clearTimeout(noticeTimer.current), [])

  const showNotice = useCallback((message) => {
    window.clearTimeout(noticeTimer.current)
    setNotice(message)
    noticeTimer.current = window.setTimeout(() => setNotice(''), 2400)
  }, [])

  const load = useCallback(async () => {
    const sequence = ++requestSequence.current
    setState((current) => ({
      ...current,
      loading: current.groups.length === 0,
      refreshing: current.groups.length > 0,
      error: '',
    }))
    try {
      const groups = items(await modelService.listGroups())
      if (sequence === requestSequence.current) setState({ loading: false, refreshing: false, error: '', groups })
    } catch (error) {
      if (sequence === requestSequence.current) {
        setState((current) => ({
          ...current,
          loading: false,
          refreshing: false,
          error: error.message || '模型分组加载失败',
        }))
      }
    }
  }, [])

  useEffect(() => { load() }, [load])

  const saved = useCallback(async () => {
    showNotice('API Key 已安全保存')
    await load()
  }, [load, showNotice])

  const closeDeleteDialog = useCallback(() => {
    if (deletingRef.current) return
    setPendingDelete(null)
    window.requestAnimationFrame(() => deleteTrigger.current?.focus())
  }, [])

  const openDeleteDialog = useCallback((group, trigger) => {
    deleteTrigger.current = trigger
    setPendingDelete(group)
  }, [])

  useEffect(() => {
    if (!pendingDelete) return undefined
    const previousOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    window.requestAnimationFrame(() => cancelDelete.current?.focus())

    const handleKeyDown = (event) => {
      if (event.key === 'Escape') {
        event.preventDefault()
        closeDeleteDialog()
        return
      }
      if (event.key !== 'Tab') return
      const focusable = [...(dialog.current?.querySelectorAll('button:not(:disabled), [href], input:not(:disabled), [tabindex]:not([tabindex="-1"])') || [])]
      if (!focusable.length) {
        event.preventDefault()
        dialog.current?.focus()
        return
      }
      const first = focusable[0]
      const last = focusable[focusable.length - 1]
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault()
        last.focus()
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault()
        first.focus()
      }
    }

    document.addEventListener('keydown', handleKeyDown)
    return () => {
      document.body.style.overflow = previousOverflow
      document.removeEventListener('keydown', handleKeyDown)
    }
  }, [closeDeleteDialog, pendingDelete])

  useEffect(() => {
    if (!deleteFocusGroupId || pendingDelete) return
    const targets = groupFocusTargets.current.get(deleteFocusGroupId)
    const target = targets?.input?.isConnected ? targets.input : targets?.heading
    if (target?.isConnected) target.focus()
    setDeleteFocusGroupId(null)
  }, [deleteFocusGroupId, pendingDelete, state.groups])

  const remove = async () => {
    if (!pendingDelete || deletingRef.current) return
    const deletedGroupId = pendingDelete.id
    deletingRef.current = true
    setDeleting(true)
    try {
      await modelService.deleteCredential(deletedGroupId)
      showNotice('API Key 已删除')
      await load()
      deletingRef.current = false
      setDeleting(false)
      setPendingDelete(null)
      setDeleteFocusGroupId(deletedGroupId)
    } catch (error) {
      showNotice(error.message || 'API Key 删除失败')
    } finally {
      if (deletingRef.current) {
        deletingRef.current = false
        setDeleting(false)
      }
    }
  }

  const chooseTheme = (mode) => {
    if (THEME_MODES.has(mode)) setThemeMode(mode)
  }
  const busy = state.loading || state.refreshing
  const themeOptions = [
    { mode: 'dark', label: '深色主题', Icon: Moon },
    { mode: 'light', label: '浅色主题', Icon: Sun },
    { mode: 'system', label: '跟随系统', Icon: Monitor },
  ]

  return (
    <main className="model-settings-page">
      <header className="settings-header">
        <div className="settings-header-inner">
          <Link className="settings-back" to="/"><ArrowLeft size={16} /><span>返回 Studio</span></Link>
          <span className="settings-header-divider" aria-hidden="true" />
          <h1>模型 API Key</h1>
          <div className="settings-header-actions">
            <div className="settings-theme-control" role="group" aria-label="界面主题">
              {themeOptions.map(({ mode, label, Icon }) => (
                <button
                  key={mode}
                  type="button"
                  className={themeMode === mode ? 'active' : ''}
                  aria-label={label}
                  aria-pressed={themeMode === mode}
                  title={label}
                  onClick={() => chooseTheme(mode)}
                >
                  <Icon size={15} />
                </button>
              ))}
            </div>
            <button className="settings-refresh" type="button" onClick={load} disabled={busy} aria-label="刷新模型分组" title="刷新">
              <RefreshCw className={busy ? 'spin' : ''} size={16} />
            </button>
          </div>
        </div>
      </header>

      <section className="settings-intro" aria-labelledby="settings-intro-title">
        <div className="settings-intro-inner">
          <KeyRound size={18} aria-hidden="true" />
          <div>
            <h2 id="settings-intro-title">按服务分组管理模型访问</h2>
            <p>同一分组内的模型共用一份凭证。密钥加密保存，页面不会再次显示完整内容。</p>
          </div>
          <span className="settings-security"><Check size={13} />仅显示末四位</span>
        </div>
      </section>

      <div className="settings-content" aria-busy={busy}>
        {state.loading && !state.groups.length && (
          <div className="settings-state"><LoaderCircle className="spin" size={20} />正在加载模型分组</div>
        )}
        {!state.loading && state.error && !state.groups.length && (
          <div className="settings-state settings-error" role="alert">
            <strong>加载失败</strong><span>{state.error}</span><button type="button" onClick={load}>重新加载</button>
          </div>
        )}
        {!state.loading && !state.error && !state.groups.length && (
          <div className="settings-state"><KeyRound size={22} /><strong>暂无可配置的模型分组</strong><span>请等待管理员启用模型分组。</span></div>
        )}
        {!!state.groups.length && state.error && (
          <div className="settings-inline-error" role="alert">
            <span>刷新失败，当前仍显示上次加载的数据。</span>
            <button type="button" onClick={load}>重试</button>
          </div>
        )}
        {!!state.groups.length && (
          <div className="credential-workspace">
            {state.groups.map((group) => (
              <CredentialRow
                key={group.id}
                group={group}
                onSaved={saved}
                onDelete={openDeleteDialog}
                registerFocusTarget={registerGroupFocusTarget}
              />
            ))}
          </div>
        )}
      </div>

      {notice && (
        <div className="settings-toast" role="status">
          {notice}<button type="button" aria-label="关闭提示" onClick={() => setNotice('')}><X size={14} /></button>
        </div>
      )}

      {pendingDelete && (
        <div className="settings-modal-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) closeDeleteDialog() }}>
          <section
            ref={dialog}
            className="settings-dialog"
            role="alertdialog"
            aria-modal="true"
            aria-labelledby="delete-key-title"
            aria-describedby="delete-key-description"
            tabIndex={-1}
          >
            <span className="settings-dialog-icon" aria-hidden="true"><Trash2 size={18} /></span>
            <h2 id="delete-key-title">删除“{pendingDelete.name}”的 API Key？</h2>
            <p id="delete-key-description">删除后，该分组下的模型会立即从你的 Studio 模型列表中移除。</p>
            <div>
              <button ref={cancelDelete} type="button" disabled={deleting} onClick={() => closeDeleteDialog()}>取消</button>
              <button className="settings-danger-button" type="button" disabled={deleting} onClick={remove}>
                {deleting && <LoaderCircle className="spin" size={15} />}确认删除
              </button>
            </div>
          </section>
        </div>
      )}
    </main>
  )
}
