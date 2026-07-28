import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  Boxes,
  ChevronLeft,
  ChevronRight,
  CircleAlert,
  Edit3,
  Eye,
  LoaderCircle,
  Layers3,
  LogOut,
  Maximize2,
  Menu,
  Pause,
  Play,
  Plus,
  RefreshCw,
  Save,
  Search,
  ShieldCheck,
  Trash2,
  UserCheck,
  UserRound,
  UserX,
  Users,
  X,
} from 'lucide-react'
import { BrowserRouter, Navigate, Route, Routes, useNavigate } from 'react-router-dom'
import { adminApi, listItems } from './api/adminApi'

const NAV_ITEMS = [
  { id: 'users', label: '用户管理', icon: Users },
  { id: 'model-groups', label: '模型分组', icon: Layers3 },
  { id: 'models', label: '模型管理', icon: Boxes },
]

const STATUS_LABELS = {
  draft: '草稿',
  active: '已启用',
  disabled: '已停用',
  archived: '不可用',
}

const KIND_LABELS = {
  text: '文本',
  image: '图片',
  video: '视频',
  audio: '音频',
}

const EMPTY_MODEL = {
  group_id: '',
  model_name: '',
  base_url: '',
  display_name: '',
  kind: 'image',
  description: '',
  status: 'draft',
  display_order: 0,
  config_schema: {},
  default_parameters: {},
  metadata: {},
}

const METADATA_EXAMPLE = {
  protocol: 'openai_images',
  endpoint_path: 'images/generations',
  supported_input_kinds: ['text', 'image'],
  edit_protocol: 'openai_images_multipart',
  edit_endpoint_path: 'images/edits',
  edit_max_input_images: 5,
  edit_image_field: 'image[]',
  image_quality_options: {
    schema_version: 1,
    provider_parameter: 'quality',
    default_key: 'medium',
    options: [
      { key: 'low', display_name: '低质量', provider_value: 'low' },
      { key: 'medium', display_name: '标准质量', provider_value: 'medium' },
      { key: 'high', display_name: '高质量', provider_value: 'high' },
    ],
  },
}

const METADATA_EXAMPLE_TEXT = JSON.stringify(METADATA_EXAMPLE, null, 2)

function formatDate(value, includeTime = false) {
  if (!value) return '未记录'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return String(value)
  return new Intl.DateTimeFormat('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    ...(includeTime ? { hour: '2-digit', minute: '2-digit' } : {}),
  }).format(date)
}

function getErrorMessage(error, fallback = '操作失败') {
  if (error?.status === 403) return '当前管理员没有执行此操作的权限'
  if (error?.status === 409) return error.message || '数据已发生变化，请刷新后重试'
  if (error?.status === 422) return error.message || '提交内容不符合接口要求'
  return error?.message || fallback
}

function LoadingState({ label = '正在加载' }) {
  return (
    <div className="state-panel" role="status">
      <LoaderCircle className="spin" size={20} />
      <span>{label}</span>
    </div>
  )
}

function EmptyState({ title, description }) {
  return (
    <div className="state-panel">
      <Boxes size={22} />
      <strong>{title}</strong>
      <span>{description}</span>
    </div>
  )
}

function ErrorState({ message, onRetry }) {
  return (
    <div className="state-panel error-state" role="alert">
      <CircleAlert size={22} />
      <strong>加载失败</strong>
      <span>{message}</span>
      <button className="secondary-button" type="button" onClick={onRetry}>
        <RefreshCw size={15} />
        重新加载
      </button>
    </div>
  )
}

function StatusBadge({ status }) {
  return <span className={`status-badge status-${status || 'unknown'}`}>{STATUS_LABELS[status] || status || '未知'}</span>
}

function Toast({ toast, onClose }) {
  useEffect(() => {
    if (!toast) return undefined
    const timer = window.setTimeout(onClose, 2800)
    return () => window.clearTimeout(timer)
  }, [toast, onClose])

  if (!toast) return null
  return (
    <div className={`toast toast-${toast.type || 'success'}`} role="status">
      <span>{toast.message}</span>
      <button type="button" aria-label="关闭提示" onClick={onClose}><X size={14} /></button>
    </div>
  )
}

function LoginScreen({ onAuthenticated }) {
  const [values, setValues] = useState({ email: '', password: '' })
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState('')

  const submit = async (event) => {
    event.preventDefault()
    if (!values.email.trim() || !values.password) {
      setError('请输入管理员邮箱和密码')
      return
    }
    setSubmitting(true)
    setError('')
    try {
      const payload = await adminApi.login({ email: values.email.trim(), password: values.password })
      onAuthenticated(payload?.admin || payload?.user || payload)
    } catch (requestError) {
      setError(requestError?.status === 401 ? '邮箱或密码不正确' : getErrorMessage(requestError, '登录失败'))
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <main className="login-page">
      <section className="login-brand" aria-label="JJboneTv 管理后台">
        <div className="brand-lockup">
          <span className="brand-mark"><img src="/assets/branding/jjbonetv-logo.png" alt="" /></span>
          <span><strong>JJboneTv</strong><small>ADMIN CONSOLE</small></span>
        </div>
        <div className="login-context">
          <span className="eyebrow">内部管理系统</span>
          <h1>管理用户与模型配置</h1>
          <p>所有变更均通过独立管理员身份执行并记录。</p>
        </div>
      </section>
      <section className="login-panel">
        <form className="login-form" onSubmit={submit}>
          <div className="form-heading">
            <span>管理员登录</span>
            <h2>进入控制台</h2>
          </div>
          <label>
            <span>邮箱</span>
            <input type="email" autoComplete="username" value={values.email} onChange={(event) => setValues((current) => ({ ...current, email: event.target.value }))} placeholder="admin@example.com" />
          </label>
          <label>
            <span>密码</span>
            <input type="password" autoComplete="current-password" value={values.password} onChange={(event) => setValues((current) => ({ ...current, password: event.target.value }))} placeholder="输入管理员密码" />
          </label>
          {error && <div className="inline-error" role="alert"><CircleAlert size={15} />{error}</div>}
          <button className="primary-button login-submit" type="submit" disabled={submitting}>
            {submitting ? <LoaderCircle className="spin" size={16} /> : <ShieldCheck size={16} />}
            {submitting ? '正在验证' : '登录'}
          </button>
        </form>
      </section>
    </main>
  )
}

function UserDetailDrawer({ userId, onClose }) {
  const [state, setState] = useState({ loading: true, error: '', user: null })

  const load = useCallback(async () => {
    setState({ loading: true, error: '', user: null })
    try {
      const payload = await adminApi.getUser(userId)
      setState({ loading: false, error: '', user: payload?.user || payload })
    } catch (error) {
      setState({ loading: false, error: getErrorMessage(error, '用户详情加载失败'), user: null })
    }
  }, [userId])

  useEffect(() => { load() }, [load])

  const user = state.user
  return (
    <div className="drawer-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose() }}>
      <aside className="detail-drawer" aria-label="用户详情">
        <header className="drawer-header">
          <div><span className="drawer-kicker">用户详情</span><h2>{user?.email || '正在读取'}</h2></div>
          <button className="icon-button" type="button" aria-label="关闭用户详情" onClick={onClose}><X size={18} /></button>
        </header>
        {state.loading && <LoadingState label="正在加载用户详情" />}
        {state.error && <ErrorState message={state.error} onRetry={load} />}
        {user && (
          <div className="detail-content">
            <section className="identity-strip">
              <span className="avatar"><UserRound size={24} /></span>
              <div><strong>{user.email}</strong><small>用户 ID：{user.id}</small></div>
            </section>
            <dl className="definition-list">
              <div><dt>注册时间</dt><dd>{formatDate(user.created_at, true)}</dd></div>
              <div><dt>项目数量</dt><dd>{user.project_count ?? user.projects_count ?? '未统计'}</dd></div>
              <div><dt>文件夹数量</dt><dd>{user.folder_count ?? '未统计'}</dd></div>
              <div><dt>资产数量</dt><dd>{user.asset_count ?? user.assets_count ?? '未统计'}</dd></div>
              <div><dt>账号状态</dt><dd><StatusBadge status={user.is_active === false ? 'disabled' : 'active'} /></dd></div>
            </dl>
            <p className="readonly-note"><Eye size={14} />用户资料仅供查看；账号启用与禁用请在用户列表中操作。</p>
          </div>
        )}
      </aside>
    </div>
  )
}

function UsersView({ notify }) {
  const [query, setQuery] = useState('')
  const [submittedQuery, setSubmittedQuery] = useState('')
  const [page, setPage] = useState(1)
  const [selectedUserId, setSelectedUserId] = useState(null)
  const [state, setState] = useState({ loading: true, error: '', users: [], total: 0, pageSize: 30 })
  const [confirmAction, setConfirmAction] = useState(null)
  const [actionLoading, setActionLoading] = useState(false)

  const load = useCallback(async () => {
    setState((current) => ({ ...current, loading: true, error: '' }))
    try {
      const payload = await adminApi.listUsers({ search: submittedQuery, page, page_size: 30, sort_order: 'desc' })
      setState({ loading: false, error: '', users: listItems(payload), total: Number(payload?.total || 0), pageSize: Number(payload?.page_size || 30) })
    } catch (error) {
      setState({ loading: false, error: getErrorMessage(error, '用户列表加载失败'), users: [], total: 0, pageSize: 30 })
    }
  }, [page, submittedQuery])

  useEffect(() => { load() }, [load])

  const search = (event) => {
    event.preventDefault()
    setPage(1)
    setSubmittedQuery(query.trim())
  }

  const hasNextPage = page * state.pageSize < state.total

  const confirmUserStatus = async () => {
    if (!confirmAction?.user) return
    setActionLoading(true)
    try {
      const payload = await adminApi.updateUserStatus(confirmAction.user.id, { is_active: confirmAction.isActive })
      const updated = payload?.user || payload || {}
      setState((current) => ({
        ...current,
        users: current.users.map((user) => user.id === confirmAction.user.id ? { ...user, ...updated, is_active: updated.is_active ?? confirmAction.isActive } : user),
      }))
      notify(`${confirmAction.user.email} 已${confirmAction.isActive ? '启用' : '禁用'}`)
      setConfirmAction(null)
    } catch (error) {
      notify(getErrorMessage(error, '用户状态更新失败'), 'error')
    } finally {
      setActionLoading(false)
    }
  }

  return (
    <section className="workspace-view">
      <header className="view-header">
        <div><span className="eyebrow">Users</span><h1>用户管理</h1><p>查看注册用户、项目与资产使用情况。</p></div>
        <button className="secondary-button" type="button" onClick={load} disabled={state.loading}><RefreshCw className={state.loading ? 'spin' : ''} size={15} />刷新</button>
      </header>
      <div className="toolbar-row">
        <form className="search-control" onSubmit={search}>
          <Search size={16} />
          <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="按邮箱搜索用户" aria-label="按邮箱搜索用户" />
          <button type="submit">搜索</button>
        </form>
        <span className="result-note">共 {state.total} 位用户，每页显示 30 位</span>
      </div>
      <div className="table-surface">
        {state.loading && <LoadingState label="正在加载用户" />}
        {!state.loading && state.error && <ErrorState message={state.error} onRetry={load} />}
        {!state.loading && !state.error && !state.users.length && <EmptyState title="没有找到用户" description={submittedQuery ? '请尝试其他邮箱关键词。' : '当前还没有注册用户。'} />}
        {!state.loading && !state.error && state.users.length > 0 && (
          <div className="table-scroll">
            <table>
              <thead><tr><th>用户</th><th>注册时间</th><th>项目</th><th>资产</th><th>状态</th><th><span className="sr-only">操作</span></th></tr></thead>
              <tbody>
                {state.users.map((user) => (
                  <tr key={user.id}>
                    <td><div className="primary-cell"><span className="mini-avatar"><UserRound size={15} /></span><span><strong>{user.email}</strong><small>{user.id}</small></span></div></td>
                    <td>{formatDate(user.created_at)}</td>
                    <td>{user.project_count ?? user.projects_count ?? '—'}</td>
                    <td>{user.asset_count ?? user.assets_count ?? '—'}</td>
                    <td><StatusBadge status={user.is_active === false ? 'disabled' : 'active'} /></td>
                    <td className="row-actions user-actions">
                      <button className="icon-button" type="button" aria-label={`查看 ${user.email}`} title="查看详情" onClick={() => setSelectedUserId(user.id)}><ChevronRight size={17} /></button>
                      {user.is_active === false
                        ? <button className="icon-button" type="button" aria-label={`启用 ${user.email}`} title="启用用户" onClick={() => setConfirmAction({ type: 'user-status', user, isActive: true, label: '启用' })}><UserCheck size={16} /></button>
                        : <button className="icon-button danger-icon-button" type="button" aria-label={`禁用 ${user.email}`} title="禁用用户" onClick={() => setConfirmAction({ type: 'user-status', user, isActive: false, label: '禁用' })}><UserX size={16} /></button>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
      <footer className="pagination-row">
        <button className="secondary-button" type="button" onClick={() => setPage((current) => Math.max(1, current - 1))} disabled={page === 1 || state.loading}><ChevronLeft size={15} />上一页</button>
        <span>第 {page} 页</span>
        <button className="secondary-button" type="button" onClick={() => setPage((current) => current + 1)} disabled={!hasNextPage || state.loading}>下一页<ChevronRight size={15} /></button>
      </footer>
      {selectedUserId && <UserDetailDrawer userId={selectedUserId} onClose={() => setSelectedUserId(null)} />}
      <ConfirmDialog action={confirmAction} loading={actionLoading} onCancel={() => setConfirmAction(null)} onConfirm={confirmUserStatus} />
    </section>
  )
}

function parseJsonObject(value, label) {
  try {
    const parsed = JSON.parse(value || '{}')
    if (!parsed || Array.isArray(parsed) || typeof parsed !== 'object') throw new Error()
    return parsed
  } catch {
    throw new Error(`${label}必须是合法的 JSON 对象`)
  }
}

function isEmptyJsonObject(value) {
  try {
    const parsed = JSON.parse(value || '{}')
    return Boolean(parsed) && !Array.isArray(parsed) && typeof parsed === 'object' && Object.keys(parsed).length === 0
  } catch {
    return false
  }
}

function MetadataEditorModal({ value, onCancel, onApply }) {
  const [draft, setDraft] = useState(value)
  const [error, setError] = useState('')
  const textareaRef = useRef(null)

  useEffect(() => {
    const previousOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    textareaRef.current?.focus()
    const onKeyDown = (event) => {
      if (event.key === 'Escape') onCancel()
    }
    window.addEventListener('keydown', onKeyDown)
    return () => {
      document.body.style.overflow = previousOverflow
      window.removeEventListener('keydown', onKeyDown)
    }
  }, [onCancel])

  const apply = () => {
    try {
      parseJsonObject(draft, 'Metadata')
      onApply(draft)
    } catch (jsonError) {
      setError(jsonError.message)
    }
  }

  const empty = isEmptyJsonObject(draft)
  return (
    <div className="modal-backdrop metadata-modal-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) onCancel() }}>
      <section className="metadata-editor-modal" role="dialog" aria-modal="true" aria-labelledby="metadata-editor-title">
        <header className="metadata-modal-header">
          <div><span className="drawer-kicker">JSON editor</span><h2 id="metadata-editor-title">放大编辑 Metadata</h2></div>
          <button className="icon-button" type="button" aria-label="关闭 Metadata 编辑器" onClick={onCancel}><X size={18} /></button>
        </header>
        <div className="metadata-modal-body">
          <div className="metadata-modal-toolbar">
            <p>这里的修改只有点击“应用到表单”后才会回填，模型仍需在主表单中保存。</p>
            <button className="secondary-button" type="button" disabled={!empty} title={empty ? '填入合法的 OpenAI 图片接口示例' : '已有内容，不会自动覆盖'} onClick={() => { setDraft(METADATA_EXAMPLE_TEXT); setError('') }}>填入示例</button>
          </div>
          <textarea ref={textareaRef} className="code-input metadata-expanded-input" spellCheck="false" value={draft} onChange={(event) => { setDraft(event.target.value); setError('') }} aria-label="Metadata JSON" />
          {error && <div className="inline-error" role="alert"><CircleAlert size={15} />{error}</div>}
        </div>
        <footer className="metadata-modal-actions">
          <button className="secondary-button" type="button" onClick={onCancel}>取消</button>
          <button className="primary-button" type="button" onClick={apply}>应用到表单</button>
        </footer>
      </section>
    </div>
  )
}

const EMPTY_IMAGE_SIZE_MATRIX = {
  schema_version: 1,
  default_resolution_key: '',
  default_aspect_ratio_key: '',
  resolutions: [],
}

function normalizeImageSizeMatrix(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return { ...EMPTY_IMAGE_SIZE_MATRIX }
  const resolutions = Array.isArray(value.resolutions) ? value.resolutions.map((resolution) => ({
    key: String(resolution?.key || ''),
    display_name: String(resolution?.display_name || ''),
    ratios: Array.isArray(resolution?.ratios) ? resolution.ratios.map((ratio) => ({
      key: String(ratio?.key || ''),
      display_name: String(ratio?.display_name || ''),
      size: String(ratio?.size || ''),
    })) : [],
  })) : []
  return {
    schema_version: 1,
    default_resolution_key: String(value.default_resolution_key || ''),
    default_aspect_ratio_key: String(value.default_aspect_ratio_key || ''),
    resolutions,
  }
}

function withValidImageSizeDefaults(matrix) {
  const resolutions = matrix.resolutions || []
  const defaultResolution = resolutions.find((item) => item.key === matrix.default_resolution_key) || resolutions[0]
  const defaultRatio = defaultResolution?.ratios?.find((item) => item.key === matrix.default_aspect_ratio_key) || defaultResolution?.ratios?.[0]
  return {
    schema_version: 1,
    default_resolution_key: defaultResolution?.key || '',
    default_aspect_ratio_key: defaultRatio?.key || '',
    resolutions,
  }
}

function validateImageSizeMatrix(matrix) {
  if (!matrix.resolutions.length) return
  const resolutionKeys = new Set()
  for (const resolution of matrix.resolutions) {
    if (!resolution.key.trim() || !resolution.display_name.trim()) throw new Error('每个清晰度都必须填写稳定 Key 和展示名称')
    const normalizedResolutionKey = resolution.key.trim().toLocaleLowerCase()
    if (resolutionKeys.has(normalizedResolutionKey)) throw new Error(`清晰度 Key「${resolution.key}」重复（Key 不区分大小写）`)
    resolutionKeys.add(normalizedResolutionKey)
    if (!resolution.ratios.length) throw new Error(`清晰度「${resolution.display_name}」至少需要一个图片比例`)
    const ratioKeys = new Set()
    for (const ratio of resolution.ratios) {
      if (!ratio.key.trim() || !ratio.display_name.trim()) throw new Error(`清晰度「${resolution.display_name}」下的比例必须填写稳定 Key 和展示名称`)
      const normalizedRatioKey = ratio.key.trim().toLocaleLowerCase()
      if (ratioKeys.has(normalizedRatioKey)) throw new Error(`清晰度「${resolution.display_name}」下的比例 Key「${ratio.key}」重复（Key 不区分大小写）`)
      ratioKeys.add(normalizedRatioKey)
      if (!/^\d{1,5}x\d{1,5}$/.test(ratio.size.trim())) throw new Error(`「${resolution.display_name} / ${ratio.display_name}」的实际分辨率格式应为 2048x1152`)
      const [width, height] = ratio.size.split('x').map(Number)
      if (width < 1 || height < 1 || width > 32768 || height > 32768) throw new Error(`「${resolution.display_name} / ${ratio.display_name}」的宽高必须在 1–32768 之间`)
    }
  }
}

function removeDerivedImageSizeFields(configSchema, defaultParameters) {
  const nextSchema = { ...configSchema }
  if (nextSchema.properties && typeof nextSchema.properties === 'object' && !Array.isArray(nextSchema.properties)) {
    const properties = { ...nextSchema.properties }
    delete properties.size
    nextSchema.properties = properties
  }
  const nextDefaults = { ...defaultParameters }
  delete nextDefaults.size
  return { configSchema: nextSchema, defaultParameters: nextDefaults }
}

const EMPTY_IMAGE_QUALITY_OPTIONS = {
  schema_version: 1,
  provider_parameter: 'quality',
  default_key: '',
  options: [],
}

const IMAGE_QUALITY_EXAMPLE = {
  schema_version: 1,
  provider_parameter: 'quality',
  default_key: 'medium',
  options: [
    { key: 'low', display_name: '低质量', provider_value: 'low' },
    { key: 'medium', display_name: '标准质量', provider_value: 'medium' },
    { key: 'high', display_name: '高质量', provider_value: 'high' },
  ],
}

function normalizeImageQualityOptions(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return { ...EMPTY_IMAGE_QUALITY_OPTIONS }
  return {
    schema_version: 1,
    provider_parameter: String(value.provider_parameter || value.parameter_name || 'quality'),
    default_key: String(value.default_key || ''),
    options: Array.isArray(value.options) ? value.options.map((option) => ({
      key: String(option?.key || ''),
      display_name: String(option?.display_name || ''),
      provider_value: String(option?.provider_value || ''),
    })) : [],
  }
}

function withValidImageQualityDefault(value) {
  const options = value.options || []
  const defaultOption = options.find((option) => option.key === value.default_key) || options[0]
  const { parameter_name: _legacyParameterName, ...normalizedValue } = value
  return {
    ...normalizedValue,
    schema_version: 1,
    provider_parameter: String(value.provider_parameter || value.parameter_name || 'quality'),
    default_key: defaultOption?.key || '',
    options,
  }
}

function validateImageQualityOptions(value) {
  if (!value.options.length) return
  if (!value.provider_parameter.trim()) throw new Error('图片质量的实际请求字段不能为空')
  const keys = new Set()
  for (const option of value.options) {
    if (!option.key.trim() || !option.display_name.trim() || !option.provider_value.trim()) throw new Error('图片质量的 Key、用户端展示名称和实际请求值均不能为空')
    const normalizedKey = option.key.trim().toLocaleLowerCase()
    if (keys.has(normalizedKey)) throw new Error(`图片质量 Key「${option.key}」重复（Key 不区分大小写）`)
    keys.add(normalizedKey)
  }
  if (!value.options.some((option) => option.key === value.default_key)) throw new Error('默认图片质量必须是当前已配置的质量项')
}

function removeDerivedImageQualityFields(configSchema, defaultParameters, providerParameter = 'quality') {
  const nextSchema = { ...configSchema }
  if (nextSchema.properties && typeof nextSchema.properties === 'object' && !Array.isArray(nextSchema.properties)) {
    const properties = { ...nextSchema.properties }
    delete properties[providerParameter]
    nextSchema.properties = properties
  }
  const nextDefaults = { ...defaultParameters }
  delete nextDefaults[providerParameter]
  return { configSchema: nextSchema, defaultParameters: nextDefaults }
}

function ModelFormDrawer({ model, groups, onClose, onSaved }) {
  const editing = Boolean(model)
  const [values, setValues] = useState(() => ({ ...EMPTY_MODEL, ...model }))
  const [jsonValues, setJsonValues] = useState(() => ({
    config_schema: JSON.stringify(model?.config_schema || {}, null, 2),
    default_parameters: JSON.stringify(model?.default_parameters || {}, null, 2),
    metadata: JSON.stringify(model?.metadata || {}, null, 2),
  }))
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState('')
  const [imageSizeMatrix, setImageSizeMatrix] = useState(() => normalizeImageSizeMatrix(model?.metadata?.image_size_matrix))
  const [imageSizeMatrixTouched, setImageSizeMatrixTouched] = useState(false)
  const [imageQualityOptions, setImageQualityOptions] = useState(() => normalizeImageQualityOptions(model?.metadata?.image_quality_options))
  const [imageQualityOptionsTouched, setImageQualityOptionsTouched] = useState(false)
  const [metadataExpanded, setMetadataExpanded] = useState(false)
  const activeGroups = groups.filter((group) => group.status === 'active')
  const currentGroup = groups.find((group) => String(group.id) === String(values.group_id))
  const inactiveCurrentTextGroup = values.kind === 'text' && currentGroup?.status !== 'active' ? currentGroup : null

  const update = (field, value) => setValues((current) => ({ ...current, [field]: value }))
  const updateImageSizeMatrix = (updater) => {
    setImageSizeMatrix((current) => withValidImageSizeDefaults(typeof updater === 'function' ? updater(current) : updater))
    setImageSizeMatrixTouched(true)
  }
  const updateResolution = (resolutionIndex, field, value) => updateImageSizeMatrix((current) => {
    const resolutions = current.resolutions.map((resolution, index) => index === resolutionIndex ? { ...resolution, [field]: value } : resolution)
    const previousKey = current.resolutions[resolutionIndex]?.key
    return { ...current, resolutions, ...(field === 'key' && current.default_resolution_key === previousKey ? { default_resolution_key: value } : {}) }
  })
  const removeResolution = (resolutionIndex) => updateImageSizeMatrix((current) => ({ ...current, resolutions: current.resolutions.filter((_, index) => index !== resolutionIndex) }))
  const addResolution = () => updateImageSizeMatrix((current) => {
    const suffix = current.resolutions.length + 1
    return { ...current, resolutions: [...current.resolutions, { key: `resolution_${suffix}`, display_name: `清晰度 ${suffix}`, ratios: [{ key: '1:1', display_name: '1:1', size: '' }] }] }
  })
  const updateRatio = (resolutionIndex, ratioIndex, field, value) => updateImageSizeMatrix((current) => {
    const resolutions = current.resolutions.map((resolution, index) => {
      if (index !== resolutionIndex) return resolution
      const ratios = resolution.ratios.map((ratio, currentRatioIndex) => currentRatioIndex === ratioIndex ? { ...ratio, [field]: value } : ratio)
      return { ...resolution, ratios }
    })
    const resolution = current.resolutions[resolutionIndex]
    const previousKey = resolution?.ratios?.[ratioIndex]?.key
    return {
      ...current,
      resolutions,
      ...(current.default_resolution_key === resolution?.key && field === 'key' && current.default_aspect_ratio_key === previousKey ? { default_aspect_ratio_key: value } : {}),
    }
  })
  const removeRatio = (resolutionIndex, ratioIndex) => updateImageSizeMatrix((current) => ({
    ...current,
    resolutions: current.resolutions.map((resolution, index) => index === resolutionIndex ? { ...resolution, ratios: resolution.ratios.filter((_, currentRatioIndex) => currentRatioIndex !== ratioIndex) } : resolution),
  }))
  const addRatio = (resolutionIndex) => updateImageSizeMatrix((current) => ({
    ...current,
    resolutions: current.resolutions.map((resolution, index) => {
      if (index !== resolutionIndex) return resolution
      const suffix = resolution.ratios.length + 1
      return { ...resolution, ratios: [...resolution.ratios, { key: `ratio_${suffix}`, display_name: `比例 ${suffix}`, size: '' }] }
    }),
  }))
  const updateImageQualityOptions = (updater) => {
    setImageQualityOptions((current) => withValidImageQualityDefault(typeof updater === 'function' ? updater(current) : updater))
    setImageQualityOptionsTouched(true)
  }
  const updateQualityOption = (optionIndex, field, value) => updateImageQualityOptions((current) => {
    const options = current.options.map((option, index) => index === optionIndex ? { ...option, [field]: value } : option)
    const previousKey = current.options[optionIndex]?.key
    return { ...current, options, ...(field === 'key' && current.default_key === previousKey ? { default_key: value } : {}) }
  })
  const addQualityOption = () => updateImageQualityOptions((current) => {
    const suffix = current.options.length + 1
    return { ...current, options: [...current.options, { key: `quality_${suffix}`, display_name: `质量 ${suffix}`, provider_value: '' }] }
  })
  const removeQualityOption = (optionIndex) => updateImageQualityOptions((current) => ({ ...current, options: current.options.filter((_, index) => index !== optionIndex) }))

  const submit = async (event) => {
    event.preventDefault()
    setError('')
    if (values.kind === 'text') {
      const groupId = String(values.group_id || '').trim()
      if (!groupId) {
        setError('请选择启用中的模型分组')
        return
      }
      const selectedGroup = groups.find((group) => String(group.id) === groupId)
      if (!selectedGroup || selectedGroup.status !== 'active') {
        setError('当前模型分组未启用，请选择启用中的分组')
        return
      }
      if (!values.base_url.trim() || !values.model_name.trim()) {
        setError('请填写 Base URL 和模型名称')
        return
      }
      setSubmitting(true)
      try {
        const textPayload = {
          group_id: groupId,
          base_url: values.base_url.trim(),
          model_name: values.model_name.trim(),
        }
        const saved = editing
          ? await adminApi.updateTextModel(model.id, textPayload)
          : await adminApi.createTextModel(textPayload)
        onSaved(saved?.model || saved, editing ? '文本模型配置已更新' : '文本模型已创建')
      } catch (requestError) {
        setError(getErrorMessage(requestError, '文本模型保存失败'))
      } finally {
        setSubmitting(false)
      }
      return
    }
    if (!values.group_id || !values.model_name.trim() || !values.display_name.trim()) {
      setError('请选择模型分组并填写模型名称和展示名称')
      return
    }
    const displayOrder = Number(values.display_order)
    if (!Number.isInteger(displayOrder) || displayOrder < 0) {
      setError('展示顺序必须是大于或等于 0 的整数')
      return
    }
    let jsonFields
    try {
      jsonFields = {
        config_schema: parseJsonObject(jsonValues.config_schema, '配置 Schema'),
        default_parameters: parseJsonObject(jsonValues.default_parameters, '默认参数'),
        metadata: parseJsonObject(jsonValues.metadata, 'Metadata'),
      }
    } catch (jsonError) {
      setError(jsonError.message)
      return
    }
    let { config_schema: configSchema, default_parameters: defaultParameters, metadata } = jsonFields
    if (values.kind === 'image' && imageSizeMatrix.resolutions.length) {
      const normalizedMatrix = withValidImageSizeDefaults({
        ...imageSizeMatrix,
        default_resolution_key: imageSizeMatrix.default_resolution_key.trim(),
        default_aspect_ratio_key: imageSizeMatrix.default_aspect_ratio_key.trim(),
        resolutions: imageSizeMatrix.resolutions.map((resolution) => ({
          ...resolution,
          key: resolution.key.trim(),
          display_name: resolution.display_name.trim(),
          ratios: resolution.ratios.map((ratio) => ({ ...ratio, key: ratio.key.trim(), display_name: ratio.display_name.trim(), size: ratio.size.trim() })),
        })),
      })
      try {
        validateImageSizeMatrix(normalizedMatrix)
      } catch (matrixError) {
        setError(matrixError.message)
        return
      }
      const sizes = [...new Set(normalizedMatrix.resolutions.flatMap((resolution) => resolution.ratios.map((ratio) => ratio.size.trim())))]
      const defaultResolution = normalizedMatrix.resolutions.find((resolution) => resolution.key === normalizedMatrix.default_resolution_key)
      const defaultRatio = defaultResolution?.ratios.find((ratio) => ratio.key === normalizedMatrix.default_aspect_ratio_key)
      metadata = { ...metadata, image_size_matrix: normalizedMatrix }
      const properties = configSchema.properties && typeof configSchema.properties === 'object' && !Array.isArray(configSchema.properties) ? configSchema.properties : {}
      configSchema = {
        ...configSchema,
        type: configSchema.type || 'object',
        properties: {
          ...properties,
          size: { ...(properties.size || {}), type: 'string', title: properties.size?.title || '实际分辨率', enum: sizes },
        },
      }
      defaultParameters = { ...defaultParameters, size: defaultRatio.size.trim() }
    } else if (imageSizeMatrixTouched || (values.kind !== 'image' && Boolean(model?.metadata?.image_size_matrix))) {
      metadata = { ...metadata }
      delete metadata.image_size_matrix
      const cleaned = removeDerivedImageSizeFields(configSchema, defaultParameters)
      configSchema = cleaned.configSchema
      defaultParameters = cleaned.defaultParameters
    }
    if (values.kind === 'image' && imageQualityOptions.options.length) {
      const normalizedQuality = withValidImageQualityDefault({
        ...imageQualityOptions,
        provider_parameter: imageQualityOptions.provider_parameter.trim(),
        default_key: imageQualityOptions.default_key.trim(),
        options: imageQualityOptions.options.map((option) => ({
          key: option.key.trim(),
          display_name: option.display_name.trim(),
          provider_value: option.provider_value.trim(),
        })),
      })
      try {
        validateImageQualityOptions(normalizedQuality)
      } catch (qualityError) {
        setError(qualityError.message)
        return
      }
      const providerValues = [...new Set(normalizedQuality.options.map((option) => option.provider_value))]
      const defaultOption = normalizedQuality.options.find((option) => option.key === normalizedQuality.default_key)
      const previousProviderParameter = normalizeImageQualityOptions(model?.metadata?.image_quality_options).provider_parameter
      if (previousProviderParameter !== normalizedQuality.provider_parameter) {
        const cleaned = removeDerivedImageQualityFields(configSchema, defaultParameters, previousProviderParameter)
        configSchema = cleaned.configSchema
        defaultParameters = cleaned.defaultParameters
      }
      const providerParameter = normalizedQuality.provider_parameter
      metadata = { ...metadata, image_quality_options: normalizedQuality }
      const properties = configSchema.properties && typeof configSchema.properties === 'object' && !Array.isArray(configSchema.properties) ? configSchema.properties : {}
      configSchema = {
        ...configSchema,
        type: configSchema.type || 'object',
        properties: {
          ...properties,
          [providerParameter]: { ...(properties[providerParameter] || {}), type: 'string', title: properties[providerParameter]?.title || '图片质量', enum: providerValues },
        },
      }
      defaultParameters = { ...defaultParameters, [providerParameter]: defaultOption.provider_value }
    } else if (imageQualityOptionsTouched || (values.kind !== 'image' && Boolean(model?.metadata?.image_quality_options))) {
      metadata = { ...metadata }
      delete metadata.image_quality_options
      const previousProviderParameter = normalizeImageQualityOptions(model?.metadata?.image_quality_options).provider_parameter
      const cleaned = removeDerivedImageQualityFields(configSchema, defaultParameters, previousProviderParameter)
      configSchema = cleaned.configSchema
      defaultParameters = cleaned.defaultParameters
    }
    const payload = {
      group_id: values.group_id,
      model_name: values.model_name.trim(),
      base_url: values.base_url.trim(),
      display_name: values.display_name.trim(),
      kind: values.kind,
      description: values.description.trim(),
      status: values.status,
      display_order: displayOrder,
      config_schema: configSchema,
      default_parameters: defaultParameters,
      metadata,
    }
    setSubmitting(true)
    try {
      const saved = editing ? await adminApi.updateModel(model.id, payload) : await adminApi.createModel(payload)
      onSaved(saved?.model || saved, editing ? '模型配置已更新' : '模型已创建')
    } catch (requestError) {
      setError(getErrorMessage(requestError, '模型保存失败'))
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="drawer-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget && !submitting) onClose() }}>
      <aside className="form-drawer" aria-label={editing ? '编辑模型' : '新建模型'}>
        <header className="drawer-header">
          <div><span className="drawer-kicker">Model configuration</span><h2>{editing ? '编辑模型' : '新建模型'}</h2></div>
          <button className="icon-button" type="button" aria-label="关闭模型表单" disabled={submitting} onClick={onClose}><X size={18} /></button>
        </header>
        <form className="model-form" onSubmit={submit}>
          <div className="form-grid two-columns">
            {values.kind === 'text' ? (
              <label>
                <span>模型分组 *</span>
                <select value={values.group_id} onChange={(event) => update('group_id', event.target.value)}>
                  <option value="">请选择启用中的模型分组</option>
                  {inactiveCurrentTextGroup && <option value={inactiveCurrentTextGroup.id} disabled>{inactiveCurrentTextGroup.name}（{STATUS_LABELS[inactiveCurrentTextGroup.status] || '未启用'}，必须更换）</option>}
                  {activeGroups.map((group) => <option key={group.id} value={group.id}>{group.name}</option>)}
                </select>
              </label>
            ) : (
              <label><span>模型分组 *</span><select value={values.group_id} onChange={(event) => update('group_id', event.target.value)}><option value="">请选择分组</option>{groups.filter((group) => group.status !== 'archived').map((group) => <option key={group.id} value={group.id}>{group.name}</option>)}</select></label>
            )}
            <label><span>模型类型 *</span><select value={values.kind} onChange={(event) => update('kind', event.target.value)}><option value="text">文本</option><option value="image">图片</option><option value="video">视频</option><option value="audio">音频</option></select></label>
          </div>
          {values.kind === 'text' ? (
            <>
              <div className="inline-note text-model-note"><ShieldCheck size={15} /><span>文本模型默认使用 Chat Completions API。系统会自动补全 <code>/chat/completions</code>；管理员只配置模型地址和名称，API Key 由用户在模型设置中填写。</span></div>
              <label><span>Base URL *</span><input type="url" value={values.base_url} onChange={(event) => update('base_url', event.target.value)} placeholder="https://api.example.com/v1" /></label>
              <label><span>模型名称 *</span><input value={values.model_name} onChange={(event) => update('model_name', event.target.value)} placeholder="gpt-5.5" /></label>
            </>
          ) : (
            <>
              <label><span>供应商模型名称 *</span><input value={values.model_name} onChange={(event) => update('model_name', event.target.value)} placeholder="provider/model-name" /></label>
              <label><span>用户端展示名称 *</span><input value={values.display_name} onChange={(event) => update('display_name', event.target.value)} placeholder="Flux Vision Pro" /></label>
              <label><span>Base URL</span><input type="url" value={values.base_url} onChange={(event) => update('base_url', event.target.value)} placeholder="https://api.example.com/v1" /></label>
              <label><span>描述</span><textarea rows="3" value={values.description} onChange={(event) => update('description', event.target.value)} placeholder="说明模型能力、限制与适用场景" /></label>
              <div className="form-grid two-columns">
                <label><span>状态</span><select value={values.status} onChange={(event) => update('status', event.target.value)} disabled={values.status === 'archived'}><option value="draft">草稿</option><option value="active">启用</option><option value="disabled">停用</option>{values.status === 'archived' && <option value="archived">不可用（历史状态）</option>}</select></label>
                <label><span>展示顺序</span><input type="number" min="0" step="1" value={values.display_order} onChange={(event) => update('display_order', event.target.value)} /></label>
              </div>
            </>
          )}
          {values.kind === 'image' && (
            <section className="image-size-editor" aria-labelledby="image-size-editor-title">
              <div className="image-size-editor-heading">
                <div><span className="drawer-kicker">Image size matrix</span><h3 id="image-size-editor-title">清晰度与图片比例</h3><p>用户选择清晰度和比例后，系统会把对应的实际分辨率作为 <code>size</code> 发送给模型。</p></div>
                <button className="secondary-button" type="button" onClick={addResolution}><Plus size={15} />添加清晰度</button>
              </div>
              {!imageSizeMatrix.resolutions.length && <div className="image-size-empty">尚未配置尺寸矩阵。旧模型仍会继续使用下方 JSON 中的 <code>size</code> 配置。</div>}
              {imageSizeMatrix.resolutions.map((resolution, resolutionIndex) => (
                <article className="resolution-card" key={resolutionIndex}>
                  <div className="resolution-card-heading">
                    <strong>{resolution.display_name || `清晰度 ${resolutionIndex + 1}`}</strong>
                    <button className="icon-button danger-icon-button" type="button" aria-label={`删除清晰度 ${resolution.display_name || resolutionIndex + 1}`} onClick={() => removeResolution(resolutionIndex)}><X size={15} /></button>
                  </div>
                  <div className="form-grid two-columns compact-fields">
                    <label><span>稳定 Key *</span><input value={resolution.key} onChange={(event) => updateResolution(resolutionIndex, 'key', event.target.value)} placeholder="2k" /></label>
                    <label><span>用户端展示名称 *</span><input value={resolution.display_name} onChange={(event) => updateResolution(resolutionIndex, 'display_name', event.target.value)} placeholder="2K" /></label>
                  </div>
                  <div className="ratio-list">
                    <div className="ratio-list-heading"><span>该清晰度支持的比例</span><button className="text-button" type="button" onClick={() => addRatio(resolutionIndex)}><Plus size={14} />添加比例</button></div>
                    {resolution.ratios.map((ratio, ratioIndex) => (
                      <div className="ratio-row" key={ratioIndex}>
                        <label><span>比例 Key *</span><input value={ratio.key} onChange={(event) => updateRatio(resolutionIndex, ratioIndex, 'key', event.target.value)} placeholder="16:9" /></label>
                        <label><span>展示名称 *</span><input value={ratio.display_name} onChange={(event) => updateRatio(resolutionIndex, ratioIndex, 'display_name', event.target.value)} placeholder="16:9" /></label>
                        <label><span>实际分辨率 *</span><input value={ratio.size} onChange={(event) => updateRatio(resolutionIndex, ratioIndex, 'size', event.target.value)} placeholder="2048x1152" /></label>
                        <button className="icon-button danger-icon-button ratio-delete" type="button" aria-label={`删除比例 ${ratio.display_name || ratioIndex + 1}`} onClick={() => removeRatio(resolutionIndex, ratioIndex)}><X size={14} /></button>
                      </div>
                    ))}
                  </div>
                </article>
              ))}
              {!!imageSizeMatrix.resolutions.length && (
                <div className="form-grid two-columns matrix-defaults">
                  <label><span>默认清晰度</span><select value={imageSizeMatrix.default_resolution_key} onChange={(event) => updateImageSizeMatrix((current) => ({ ...current, default_resolution_key: event.target.value, default_aspect_ratio_key: '' }))}>{imageSizeMatrix.resolutions.map((resolution, index) => <option key={index} value={resolution.key}>{resolution.display_name || resolution.key}</option>)}</select></label>
                  <label><span>默认比例</span><select value={imageSizeMatrix.default_aspect_ratio_key} onChange={(event) => updateImageSizeMatrix((current) => ({ ...current, default_aspect_ratio_key: event.target.value }))}>{(imageSizeMatrix.resolutions.find((resolution) => resolution.key === imageSizeMatrix.default_resolution_key)?.ratios || []).map((ratio, index) => <option key={index} value={ratio.key}>{ratio.display_name || ratio.key} · {ratio.size || '未填写尺寸'}</option>)}</select></label>
                </div>
              )}
              <p className="image-size-note">结构化配置保存时会覆盖 Metadata 中的 <code>image_size_matrix</code>，并同步配置 Schema 的 <code>size.enum</code> 与默认参数的 <code>size</code>；其它 JSON 字段保持不变。</p>
            </section>
          )}
          {values.kind === 'image' && (
            <section className="image-size-editor image-quality-editor" aria-labelledby="image-quality-editor-title">
              <div className="image-size-editor-heading">
                <div>
                  <span className="drawer-kicker">Image quality</span>
                  <h3 id="image-quality-editor-title">图片质量</h3>
                  <p>分别配置用户看到的名称，以及实际发送给模型接口的字段和值。</p>
                </div>
                <div className="editor-heading-actions">
                  <button className="secondary-button compact-button" type="button" onClick={() => updateImageQualityOptions(normalizeImageQualityOptions(IMAGE_QUALITY_EXAMPLE))}>填入 low / medium / high</button>
                  <button className="secondary-button compact-button" type="button" onClick={addQualityOption}><Plus size={14} />添加质量</button>
                </div>
              </div>
              {!imageQualityOptions.options.length && <div className="image-size-empty">尚未配置图片质量。没有质量项时，用户端不会展示质量选择器。</div>}
              {!!imageQualityOptions.options.length && (
                <div className="quality-option-list">
                  <label className="quality-default-field"><span>实际请求字段 *</span><input value={imageQualityOptions.provider_parameter} onChange={(event) => updateImageQualityOptions((current) => ({ ...current, provider_parameter: event.target.value }))} placeholder="quality" /></label>
                  {imageQualityOptions.options.map((option, optionIndex) => (
                    <div className="quality-option-row" key={`${option.key}-${optionIndex}`}>
                      <label><span>稳定 Key *</span><input value={option.key} onChange={(event) => updateQualityOption(optionIndex, 'key', event.target.value)} placeholder="medium" /></label>
                      <label><span>用户端展示名称 *</span><input value={option.display_name} onChange={(event) => updateQualityOption(optionIndex, 'display_name', event.target.value)} placeholder="标准质量" /></label>
                      <label><span>实际请求值 *</span><input value={option.provider_value} onChange={(event) => updateQualityOption(optionIndex, 'provider_value', event.target.value)} placeholder="medium" /></label>
                      <button className="icon-button danger-icon-button quality-delete" type="button" aria-label={`删除质量 ${option.display_name || optionIndex + 1}`} onClick={() => removeQualityOption(optionIndex)}><X size={14} /></button>
                    </div>
                  ))}
                  <label className="quality-default-field"><span>默认质量</span><select value={imageQualityOptions.default_key} onChange={(event) => updateImageQualityOptions((current) => ({ ...current, default_key: event.target.value }))}>{imageQualityOptions.options.map((option, index) => <option key={index} value={option.key}>{option.display_name || option.key} · {option.provider_value || '未填写请求值'}</option>)}</select></label>
                </div>
              )}
              <p className="image-size-note">保存时会写入 Metadata 的 <code>image_quality_options.provider_parameter</code>，并将质量值同步到配置 Schema 与默认参数中的对应请求字段。旧字段 <code>parameter_name</code> 可继续读取，但再次保存时会自动升级。</p>
            </section>
          )}
          {values.kind !== 'text' && <><label><span>配置 Schema</span><textarea className="code-input" rows="7" spellCheck="false" value={jsonValues.config_schema} onChange={(event) => setJsonValues((current) => ({ ...current, config_schema: event.target.value }))} /></label>
          <label><span>默认参数</span><textarea className="code-input" rows="7" spellCheck="false" value={jsonValues.default_parameters} onChange={(event) => setJsonValues((current) => ({ ...current, default_parameters: event.target.value }))} /></label>
          <div className="metadata-field">
            <div className="metadata-field-header">
              <span>Metadata</span>
              <div>
                <button className="text-button" type="button" disabled={!isEmptyJsonObject(jsonValues.metadata)} title={isEmptyJsonObject(jsonValues.metadata) ? '填入合法的 OpenAI 图片接口示例' : '已有内容，不会自动覆盖'} onClick={() => setJsonValues((current) => ({ ...current, metadata: METADATA_EXAMPLE_TEXT }))}>填入示例</button>
                <button className="text-button" type="button" onClick={() => setMetadataExpanded(true)}><Maximize2 size={13} />放大编辑</button>
              </div>
            </div>
            <textarea className="code-input" rows="7" spellCheck="false" value={jsonValues.metadata} onChange={(event) => setJsonValues((current) => ({ ...current, metadata: event.target.value }))} aria-label="Metadata JSON" />
            <details className="metadata-example">
              <summary>查看 Metadata 合法示例</summary>
              <pre>{METADATA_EXAMPLE_TEXT}</pre>
            </details>
          </div></>}
          {error && <div className="inline-error" role="alert"><CircleAlert size={15} />{error}</div>}
          <footer className="drawer-actions">
            <button className="secondary-button" type="button" disabled={submitting} onClick={onClose}>取消</button>
            <button className="primary-button" type="submit" disabled={submitting}>{submitting ? <LoaderCircle className="spin" size={16} /> : <Save size={16} />}{submitting ? '正在保存' : '保存模型'}</button>
          </footer>
        </form>
      </aside>
      {metadataExpanded && <MetadataEditorModal value={jsonValues.metadata} onCancel={() => setMetadataExpanded(false)} onApply={(metadata) => { setJsonValues((current) => ({ ...current, metadata })); setMetadataExpanded(false) }} />}
    </div>
  )
}

const EMPTY_GROUP = { name: '', description: '', status: 'draft', display_order: 0 }

function ModelGroupFormDrawer({ group, onClose, onSaved }) {
  const editing = Boolean(group)
  const [values, setValues] = useState(() => ({ ...EMPTY_GROUP, ...group }))
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState('')
  const update = (field, value) => setValues((current) => ({ ...current, [field]: value }))

  const submit = async (event) => {
    event.preventDefault()
    setError('')
    const displayOrder = Number(values.display_order)
    if (!values.name.trim()) { setError('请输入分组名称'); return }
    if (!Number.isInteger(displayOrder) || displayOrder < 0) { setError('展示顺序必须是大于或等于 0 的整数'); return }
    setSubmitting(true)
    try {
      const payload = { name: values.name.trim(), description: values.description.trim(), status: values.status, display_order: displayOrder }
      const saved = editing ? await adminApi.updateModelGroup(group.id, payload) : await adminApi.createModelGroup(payload)
      onSaved(saved?.group || saved, editing ? '模型分组已更新' : '模型分组已创建')
    } catch (requestError) {
      setError(getErrorMessage(requestError, '模型分组保存失败'))
    } finally { setSubmitting(false) }
  }

  return (
    <div className="drawer-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget && !submitting) onClose() }}>
      <aside className="form-drawer compact-drawer" aria-label={editing ? '编辑模型分组' : '新建模型分组'}>
        <header className="drawer-header"><div><span className="drawer-kicker">Model group</span><h2>{editing ? '编辑模型分组' : '新建模型分组'}</h2></div><button className="icon-button" type="button" aria-label="关闭分组表单" disabled={submitting} onClick={onClose}><X size={18} /></button></header>
        <form className="model-form" onSubmit={submit}>
          <label><span>分组名称 *</span><input value={values.name} onChange={(event) => update('name', event.target.value)} placeholder="例如：OpenAI、JJboneTv CH1" /></label>
          <label><span>描述</span><textarea rows="4" value={values.description} onChange={(event) => update('description', event.target.value)} placeholder="说明该分组对应的服务商或 API Key 使用范围" /></label>
          <div className="form-grid two-columns">
            <label><span>状态</span><select value={values.status} onChange={(event) => update('status', event.target.value)} disabled={values.status === 'archived'}><option value="draft">草稿</option><option value="active">启用</option><option value="disabled">停用</option>{values.status === 'archived' && <option value="archived">不可用（历史状态）</option>}</select></label>
            <label><span>展示顺序</span><input type="number" min="0" step="1" value={values.display_order} onChange={(event) => update('display_order', event.target.value)} /></label>
          </div>
          {error && <div className="inline-error" role="alert"><CircleAlert size={15} />{error}</div>}
          <footer className="drawer-actions"><button className="secondary-button" type="button" disabled={submitting} onClick={onClose}>取消</button><button className="primary-button" type="submit" disabled={submitting}>{submitting ? <LoaderCircle className="spin" size={16} /> : <Save size={16} />}{submitting ? '正在保存' : '保存分组'}</button></footer>
        </form>
      </aside>
    </div>
  )
}

function ConfirmDialog({ action, loading, onCancel, onConfirm }) {
  if (!action) return null
  const item = action.model || action.group || action.user
  const isGroup = Boolean(action.group)
  const isUser = Boolean(action.user)
  const deleting = action.type === 'delete'
  const copy = deleting
    ? isGroup
      ? '此操作会直接从数据库删除该分组，无法撤销。若分组仍包含模型，系统会拒绝删除并说明原因。'
      : '此操作会直接从数据库删除该模型配置，无法撤销。已有项目和历史画布记录不会被前端改写。'
    : isUser
      ? action.isActive
        ? '启用后，该用户可以重新登录并继续使用用户端功能。'
        : '禁用后，该用户将不能继续登录或使用受保护功能；已有项目和资产不会被删除。'
      : action.nextStatus === 'active'
        ? isGroup ? '启用后，用户可以为该分组配置 API Key。' : '启用后，已配置对应分组 Key 的用户可以选择该模型。'
        : isGroup ? '停用后，该分组下所有模型都会从用户端隐藏。' : '停用后，用户端将不能再选择该模型，已有画布记录不受影响。'
  const itemName = item.display_name || item.name || item.email
  const dangerous = deleting || (isUser && !action.isActive)
  return (
    <div className="modal-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget && !loading) onCancel() }}>
      <section className="confirm-dialog" role="dialog" aria-modal="true" aria-labelledby="confirm-title">
        <span className="confirm-icon"><CircleAlert size={20} /></span>
        <h2 id="confirm-title">{action.label}“{itemName}”</h2>
        <p>{copy}</p>
        <div className="confirm-actions">
          <button className="secondary-button" type="button" disabled={loading} onClick={onCancel}>取消</button>
          <button className={`primary-button ${dangerous ? 'danger-button' : ''}`} type="button" disabled={loading} onClick={onConfirm}>{loading && <LoaderCircle className="spin" size={16} />}{action.label}</button>
        </div>
      </section>
    </div>
  )
}

function ModelGroupsView({ notify }) {
  const [state, setState] = useState({ loading: true, error: '', groups: [] })
  const [editingGroup, setEditingGroup] = useState(undefined)
  const [confirmAction, setConfirmAction] = useState(null)
  const [actionLoading, setActionLoading] = useState(false)
  const load = useCallback(async () => {
    setState((current) => ({ ...current, loading: true, error: '' }))
    try { setState({ loading: false, error: '', groups: listItems(await adminApi.listModelGroups()) }) }
    catch (error) { setState({ loading: false, error: getErrorMessage(error, '模型分组加载失败'), groups: [] }) }
  }, [])
  useEffect(() => { load() }, [load])

  const saved = (group, message) => {
    setState((current) => {
      const exists = current.groups.some((item) => item.id === group.id)
      return { ...current, groups: exists ? current.groups.map((item) => item.id === group.id ? { ...item, ...group } : item) : [...current.groups, group] }
    })
    setEditingGroup(undefined)
    notify(message)
  }
  const requestStatus = (group, nextStatus) => setConfirmAction({ group, nextStatus, label: nextStatus === 'active' ? '启用' : '停用' })
  const requestDelete = (group) => setConfirmAction({ type: 'delete', group, label: '删除' })
  const confirmStatus = async () => {
    if (!confirmAction) return
    setActionLoading(true)
    try {
      if (confirmAction.type === 'delete') {
        await adminApi.deleteModelGroup(confirmAction.group.id)
        setState((current) => ({ ...current, groups: current.groups.filter((group) => group.id !== confirmAction.group.id) }))
      } else {
        const payload = await adminApi.updateModelGroup(confirmAction.group.id, { status: confirmAction.nextStatus })
        const updated = payload?.group || payload || {}
        setState((current) => ({ ...current, groups: current.groups.map((group) => group.id === confirmAction.group.id ? { ...group, ...updated, status: updated.status || confirmAction.nextStatus } : group) }))
      }
      notify(`${confirmAction.group.name}已${confirmAction.label}`)
      setConfirmAction(null)
    } catch (error) { notify(getErrorMessage(error, confirmAction.type === 'delete' ? '分组删除失败' : '分组状态更新失败'), 'error') }
    finally { setActionLoading(false) }
  }

  return (
    <section className="workspace-view">
      <header className="view-header"><div><span className="eyebrow">Model groups</span><h1>模型分组</h1><p>定义用户 API Key 的配置范围，并组织同一服务商下的模型。</p></div><div className="header-actions"><button className="secondary-button" type="button" onClick={load} disabled={state.loading}><RefreshCw className={state.loading ? 'spin' : ''} size={15} />刷新</button><button className="primary-button" type="button" onClick={() => setEditingGroup(null)}><Plus size={16} />新建分组</button></div></header>
      <div className="table-surface group-table-surface">
        {state.loading && <LoadingState label="正在加载模型分组" />}
        {!state.loading && state.error && <ErrorState message={state.error} onRetry={load} />}
        {!state.loading && !state.error && !state.groups.length && <EmptyState title="还没有模型分组" description="先创建分组，再为分组添加模型。" />}
        {!state.loading && !state.error && state.groups.length > 0 && <div className="table-scroll"><table className="groups-table"><thead><tr><th>顺序</th><th>分组</th><th>模型数量</th><th>状态</th><th>更新时间</th><th><span className="sr-only">操作</span></th></tr></thead><tbody>{state.groups.map((group) => <tr key={group.id}><td className="order-cell">{group.display_order ?? 0}</td><td><div className="primary-cell"><span className="kind-icon"><Layers3 size={15} /></span><span><strong>{group.name}</strong><small>{group.description || '未填写描述'}</small></span></div></td><td>{group.model_count ?? 0}</td><td><StatusBadge status={group.status} /></td><td>{formatDate(group.updated_at, true)}</td><td className="row-actions model-actions"><button className="icon-button" type="button" aria-label={`编辑 ${group.name}`} title={group.status === 'archived' ? '历史不可用分组不可编辑' : '编辑'} disabled={group.status === 'archived'} onClick={() => setEditingGroup(group)}><Edit3 size={16} /></button>{group.status !== 'archived' && group.status !== 'active' && <button className="icon-button" type="button" aria-label={`启用 ${group.name}`} title="启用" onClick={() => requestStatus(group, 'active')}><Play size={16} /></button>}{group.status === 'active' && <button className="icon-button" type="button" aria-label={`停用 ${group.name}`} title="停用" onClick={() => requestStatus(group, 'disabled')}><Pause size={16} /></button>}<button className="icon-button delete-action" type="button" aria-label={`删除 ${group.name}`} title="永久删除" onClick={() => requestDelete(group)}><Trash2 size={16} /></button></td></tr>)}</tbody></table></div>}
      </div>
      {editingGroup !== undefined && <ModelGroupFormDrawer group={editingGroup} onClose={() => setEditingGroup(undefined)} onSaved={saved} />}
      <ConfirmDialog action={confirmAction} loading={actionLoading} onCancel={() => setConfirmAction(null)} onConfirm={confirmStatus} />
    </section>
  )
}

function ModelsView({ notify }) {
  const [state, setState] = useState({ loading: true, error: '', models: [], groups: [] })
  const [query, setQuery] = useState('')
  const [statusFilter, setStatusFilter] = useState('all')
  const [editingModel, setEditingModel] = useState(undefined)
  const [confirmAction, setConfirmAction] = useState(null)
  const [actionLoading, setActionLoading] = useState(false)

  const load = useCallback(async () => {
    setState((current) => ({ ...current, loading: true, error: '' }))
    try {
      const [modelsPayload, groupsPayload] = await Promise.all([adminApi.listModels(), adminApi.listModelGroups()])
      setState({ loading: false, error: '', models: listItems(modelsPayload), groups: listItems(groupsPayload) })
    } catch (error) {
      setState({ loading: false, error: getErrorMessage(error, '模型列表加载失败'), models: [], groups: [] })
    }
  }, [])

  useEffect(() => { load() }, [load])

  const visibleModels = useMemo(() => {
    const keyword = query.trim().toLowerCase()
    return state.models.filter((model) => {
      const matchesStatus = statusFilter === 'all' || model.status === statusFilter
      const groupName = state.groups.find((group) => group.id === model.group_id)?.name || ''
      const haystack = `${model.display_name || ''} ${model.model_name || ''} ${groupName}`.toLowerCase()
      return matchesStatus && (!keyword || haystack.includes(keyword))
    })
  }, [query, state.groups, state.models, statusFilter])

  const requestStatus = (model, nextStatus) => {
    const label = nextStatus === 'active' ? '启用' : '停用'
    setConfirmAction({ model, nextStatus, label })
  }
  const requestDelete = (model) => setConfirmAction({ type: 'delete', model, label: '删除' })

  const confirmStatus = async () => {
    if (!confirmAction) return
    setActionLoading(true)
    try {
      if (confirmAction.type === 'delete') {
        await adminApi.deleteModel(confirmAction.model.id)
        setState((current) => ({ ...current, models: current.models.filter((model) => model.id !== confirmAction.model.id) }))
      } else {
        const payload = await adminApi.updateModel(confirmAction.model.id, { status: confirmAction.nextStatus })
        const updated = payload?.model || payload || {}
        setState((current) => ({ ...current, models: current.models.map((model) => model.id === confirmAction.model.id ? { ...model, ...updated, status: updated.status || confirmAction.nextStatus } : model) }))
      }
      notify(`${confirmAction.model.display_name}已${confirmAction.label}`)
      setConfirmAction(null)
    } catch (error) {
      notify(getErrorMessage(error, confirmAction.type === 'delete' ? '模型删除失败' : '模型状态更新失败'), 'error')
    } finally {
      setActionLoading(false)
    }
  }

  const saved = (model, message) => {
    setState((current) => {
      const exists = current.models.some((item) => item.id === model.id)
      return { ...current, models: exists ? current.models.map((item) => item.id === model.id ? model : item) : [...current.models, model] }
    })
    setEditingModel(undefined)
    notify(message)
  }

  return (
    <section className="workspace-view">
      <header className="view-header">
        <div><span className="eyebrow">Models</span><h1>模型管理</h1><p>维护用户端可用模型、默认参数和展示顺序。</p></div>
        <div className="header-actions">
          <button className="secondary-button" type="button" onClick={load} disabled={state.loading}><RefreshCw className={state.loading ? 'spin' : ''} size={15} />刷新</button>
          <button className="primary-button" type="button" onClick={() => setEditingModel(null)} disabled={!state.groups.some((group) => group.status !== 'archived')}><Plus size={16} />新建模型</button>
        </div>
      </header>
      <div className="toolbar-row">
        <div className="search-control model-search"><Search size={16} /><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索模型名称、供应商名称或分组" aria-label="搜索模型" /></div>
        <div className="segmented-control" aria-label="模型状态筛选">
          {['all', 'draft', 'active', 'disabled'].map((status) => <button key={status} className={statusFilter === status ? 'active' : ''} type="button" onClick={() => setStatusFilter(status)}>{status === 'all' ? '全部' : STATUS_LABELS[status]}</button>)}
        </div>
      </div>
      <div className="table-surface">
        {state.loading && <LoadingState label="正在加载模型配置" />}
        {!state.loading && state.error && <ErrorState message={state.error} onRetry={load} />}
        {!state.loading && !state.error && !visibleModels.length && <EmptyState title="没有匹配的模型" description={state.models.length ? '调整搜索词或状态筛选。' : '创建第一条模型配置后，启用它即可在用户端使用。'} />}
        {!state.loading && !state.error && visibleModels.length > 0 && (
          <div className="table-scroll">
            <table className="models-table">
              <thead><tr><th>顺序</th><th>模型</th><th>类型</th><th>状态</th><th>更新时间</th><th><span className="sr-only">操作</span></th></tr></thead>
              <tbody>
                {visibleModels.map((model) => (
                  <tr key={model.id}>
                    <td className="order-cell">{model.display_order ?? 0}</td>
                    <td><div className="primary-cell"><span className={`kind-icon kind-${model.kind}`}>{(model.kind || 'M').slice(0, 1).toUpperCase()}</span><span><strong>{model.display_name}</strong><small>{state.groups.find((group) => group.id === model.group_id)?.name || '未分组'} · {model.model_name}</small></span></div></td>
                    <td>{KIND_LABELS[model.kind] || model.kind}</td>
                    <td><StatusBadge status={model.status} /></td>
                    <td>{formatDate(model.updated_at, true)}</td>
                    <td className="row-actions model-actions">
                      <button className="icon-button" type="button" aria-label={`编辑 ${model.display_name}`} title={model.status === 'archived' ? '历史不可用模型不可编辑' : '编辑'} disabled={model.status === 'archived'} onClick={() => setEditingModel(model)}><Edit3 size={16} /></button>
                      {model.status !== 'archived' && model.status !== 'active' && <button className="icon-button" type="button" aria-label={`启用 ${model.display_name}`} title="启用" onClick={() => requestStatus(model, 'active')}><Play size={16} /></button>}
                      {model.status === 'active' && <button className="icon-button" type="button" aria-label={`停用 ${model.display_name}`} title="停用" onClick={() => requestStatus(model, 'disabled')}><Pause size={16} /></button>}
                      <button className="icon-button delete-action" type="button" aria-label={`删除 ${model.display_name}`} title="永久删除" onClick={() => requestDelete(model)}><Trash2 size={16} /></button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
      {editingModel !== undefined && <ModelFormDrawer model={editingModel} groups={state.groups} onClose={() => setEditingModel(undefined)} onSaved={saved} />}
      <ConfirmDialog action={confirmAction} loading={actionLoading} onCancel={() => setConfirmAction(null)} onConfirm={confirmStatus} />
    </section>
  )
}

function AdminShell({ admin, onLogout, activeView }) {
  const navigate = useNavigate()
  const [sidebarOpen, setSidebarOpen] = useState(false)
  const [toast, setToast] = useState(null)
  const [loggingOut, setLoggingOut] = useState(false)

  const notify = useCallback((message, type = 'success') => setToast({ message, type, id: Date.now() }), [])

  const logout = async () => {
    setLoggingOut(true)
    try { await adminApi.logout() } catch { /* Cookie is cleared locally by leaving the authenticated shell. */ }
    onLogout()
  }

  const selectView = (id) => { navigate(`/${id}`); setSidebarOpen(false) }

  return (
    <div className="admin-shell">
      <aside className={`sidebar ${sidebarOpen ? 'open' : ''}`}>
        <div className="sidebar-brand"><span className="brand-mark compact"><img src="/assets/branding/jjbonetv-logo.png" alt="" /></span><span><strong>JJboneTv</strong><small>ADMIN</small></span></div>
        <nav aria-label="管理后台导航">
          {NAV_ITEMS.map((item) => {
            const Icon = item.icon
            return <button key={item.id} className={activeView === item.id ? 'active' : ''} type="button" onClick={() => selectView(item.id)}><Icon size={17} /><span>{item.label}</span></button>
          })}
        </nav>
        <div className="sidebar-account">
          <span className="mini-avatar"><ShieldCheck size={15} /></span>
          <span><strong>{admin?.email || admin?.name || '管理员'}</strong><small>独立管理会话</small></span>
          <button className="icon-button" type="button" aria-label="退出登录" title="退出登录" disabled={loggingOut} onClick={logout}>{loggingOut ? <LoaderCircle className="spin" size={16} /> : <LogOut size={16} />}</button>
        </div>
      </aside>
      {sidebarOpen && <button className="sidebar-scrim" type="button" aria-label="关闭导航" onClick={() => setSidebarOpen(false)} />}
      <main className="admin-main">
        <header className="mobile-header"><button className="icon-button" type="button" aria-label="打开导航" onClick={() => setSidebarOpen(true)}><Menu size={19} /></button><strong>JJboneTv Admin</strong></header>
        {activeView === 'users' && <UsersView notify={notify} />}
        {activeView === 'model-groups' && <ModelGroupsView notify={notify} />}
        {activeView === 'models' && <ModelsView notify={notify} />}
      </main>
      <Toast toast={toast} onClose={() => setToast(null)} />
    </div>
  )
}

function AppRoutes() {
  const [session, setSession] = useState({ checking: true, admin: null })

  useEffect(() => {
    let active = true
    adminApi.me()
      .then((payload) => { if (active) setSession({ checking: false, admin: payload?.admin || payload?.user || payload }) })
      .catch(() => { if (active) setSession({ checking: false, admin: null }) })
    const unauthorized = () => setSession({ checking: false, admin: null })
    window.addEventListener('admin:unauthorized', unauthorized)
    return () => { active = false; window.removeEventListener('admin:unauthorized', unauthorized) }
  }, [])

  if (session.checking) return <div className="app-loading"><span className="brand-mark"><img src="/assets/branding/jjbonetv-logo.png" alt="" /></span><LoaderCircle className="spin" size={19} /><span>正在验证管理员会话</span></div>
  if (!session.admin) return <Routes><Route path="/login" element={<LoginRoute onAuthenticated={(admin) => setSession({ checking: false, admin })} />} /><Route path="*" element={<Navigate to="/login" replace />} /></Routes>
  const logout = () => setSession({ checking: false, admin: null })
  return <Routes><Route path="/users" element={<AdminShell admin={session.admin} onLogout={logout} activeView="users" />} /><Route path="/model-groups" element={<AdminShell admin={session.admin} onLogout={logout} activeView="model-groups" />} /><Route path="/models" element={<AdminShell admin={session.admin} onLogout={logout} activeView="models" />} /><Route path="*" element={<Navigate to="/users" replace />} /></Routes>
}

function LoginRoute({ onAuthenticated }) {
  const navigate = useNavigate()
  return <LoginScreen onAuthenticated={(admin) => { onAuthenticated(admin); navigate('/users', { replace: true }) }} />
}

export function App() {
  return <BrowserRouter><AppRoutes /></BrowserRouter>
}
