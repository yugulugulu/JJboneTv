import { requestJson } from './httpClient'

const AUTH_BASE_URL = '/api/auth'

export const authService = {
  async login(credentials) {
    const payload = await requestJson(`${AUTH_BASE_URL}/login`, {
      method: 'POST',
      body: JSON.stringify(credentials),
      suppressUnauthorizedEvent: true,
    })
    return payload.user
  },

  async register(credentials) {
    const payload = await requestJson(`${AUTH_BASE_URL}/register`, {
      method: 'POST',
      body: JSON.stringify(credentials),
      suppressUnauthorizedEvent: true,
    })
    return payload.user
  },

  async getCurrentUser() {
    const payload = await requestJson(`${AUTH_BASE_URL}/me`, {
      suppressUnauthorizedEvent: true,
    })
    return payload.user
  },

  logout() {
    return requestJson(`${AUTH_BASE_URL}/logout`, {
      method: 'POST',
      suppressUnauthorizedEvent: true,
    })
  },
}
