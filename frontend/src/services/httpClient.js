function parseRetryAfter(value) {
  if (!value) return null
  const seconds = Number.parseInt(value, 10)
  if (Number.isFinite(seconds) && seconds >= 0) return seconds
  const deadline = Date.parse(value)
  return Number.isNaN(deadline) ? null : Math.max(0, Math.ceil((deadline - Date.now()) / 1000))
}

export const AUTH_UNAUTHORIZED_EVENT = 'thinktv:auth-unauthorized'

export function notifyUnauthorized(detail = {}) {
  if (typeof window === 'undefined') return
  window.dispatchEvent(new CustomEvent(AUTH_UNAUTHORIZED_EVENT, { detail }))
}

export class HttpError extends Error {
  constructor(message, { status = 0, code = null, retryAfterSeconds = null, payload = null } = {}) {
    super(message)
    this.name = 'HttpError'
    this.status = status
    this.code = code
    this.retryAfterSeconds = retryAfterSeconds
    this.payload = payload
  }
}

export async function requestJson(url, options = {}) {
  const { headers, suppressUnauthorizedEvent = false, ...fetchOptions } = options
  let response
  try {
    response = await fetch(url, {
      credentials: 'include',
      ...fetchOptions,
      headers: {
        'Content-Type': 'application/json',
        ...headers,
      },
    })
  } catch (error) {
    throw new HttpError('无法连接服务器，请检查网络后重试', {
      code: 'NETWORK_ERROR',
      payload: error,
    })
  }

  if (!response.ok) {
    let message = '请求失败，请稍后重试'
    let payload = null
    try {
      payload = await response.json()
      const detail = payload?.detail
      message = typeof detail === 'string'
        ? detail
        : detail?.message || payload?.message || message
    } catch {
      // The fallback is appropriate for non-JSON server errors.
    }
    const detail = payload?.detail
    const bodyRetryAfter = detail?.retry_after_seconds
      ?? detail?.retry_after
      ?? payload?.retry_after_seconds
      ?? payload?.retry_after
    const code = detail?.code || payload?.code || null
    if (response.status === 401 && !suppressUnauthorizedEvent) {
      notifyUnauthorized({ url, status: response.status, code })
    }
    throw new HttpError(message, {
      status: response.status,
      code,
      retryAfterSeconds: parseRetryAfter(response.headers.get('Retry-After'))
        ?? (Number.isFinite(Number(bodyRetryAfter)) ? Math.max(0, Math.ceil(Number(bodyRetryAfter))) : null),
      payload,
    })
  }

  if (response.status === 204) return null
  return response.json()
}
