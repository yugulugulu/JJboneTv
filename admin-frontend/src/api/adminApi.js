const ADMIN_API_BASE = '/admin-api'

export class ApiError extends Error {
  constructor(message, status = 0, payload = null) {
    super(message)
    this.status = status
    this.payload = payload
  }
}

async function request(path, options = {}) {
  let response
  try {
    response = await fetch(`${ADMIN_API_BASE}${path}`, {
      credentials: 'include',
      ...options,
      headers: {
        'Content-Type': 'application/json',
        ...options.headers,
      },
    })
  } catch {
    throw new ApiError('无法连接管理服务，请检查网络后重试')
  }

  let payload = null
  if (response.status !== 204) {
    try {
      payload = await response.json()
    } catch {
      payload = null
    }
  }

  if (!response.ok) {
    const detail = payload?.detail || payload?.message
    if (response.status === 401) window.dispatchEvent(new Event('admin:unauthorized'))
    const message = typeof detail === 'string'
      ? detail
      : detail && typeof detail === 'object'
        ? detail.message || detail.detail || detail.error || JSON.stringify(detail)
        : `请求失败 (${response.status})`
    throw new ApiError(message, response.status, payload)
  }

  return payload
}

function withQuery(path, params = {}) {
  const query = new URLSearchParams()
  Object.entries(params).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== '') query.set(key, String(value))
  })
  const suffix = query.toString()
  return suffix ? `${path}?${suffix}` : path
}

export const adminApi = {
  login: (credentials) => request('/auth/login', { method: 'POST', body: JSON.stringify(credentials) }),
  me: () => request('/auth/me'),
  logout: () => request('/auth/logout', { method: 'POST' }),
  listUsers: (params) => request(withQuery('/users', params)),
  getUser: (id) => request(`/users/${encodeURIComponent(id)}`),
  updateUserStatus: (id, payload) => request(`/users/${encodeURIComponent(id)}/status`, { method: 'PATCH', body: JSON.stringify(payload) }),
  listModelGroups: () => request('/model-groups'),
  createModelGroup: (payload) => request('/model-groups', { method: 'POST', body: JSON.stringify(payload) }),
  updateModelGroup: (id, payload) => request(`/model-groups/${encodeURIComponent(id)}`, { method: 'PATCH', body: JSON.stringify(payload) }),
  deleteModelGroup: (id) => request(`/model-groups/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  listModels: (params) => request(withQuery('/models', params)),
  createTextModel: (payload) => request('/text-models', { method: 'POST', body: JSON.stringify(payload) }),
  updateTextModel: (id, payload) => request(`/text-models/${encodeURIComponent(id)}`, { method: 'PATCH', body: JSON.stringify(payload) }),
  createModel: (payload) => request('/models', { method: 'POST', body: JSON.stringify(payload) }),
  updateModel: (id, payload) => request(`/models/${encodeURIComponent(id)}`, { method: 'PATCH', body: JSON.stringify(payload) }),
  deleteModel: (id) => request(`/models/${encodeURIComponent(id)}`, { method: 'DELETE' }),
}

export function listItems(payload) {
  if (Array.isArray(payload)) return payload
  return Array.isArray(payload?.items) ? payload.items : []
}
