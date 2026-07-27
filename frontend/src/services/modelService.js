import { requestJson } from './httpClient'

export const modelService = {
  listGroups: () => requestJson('/api/model-groups'),
  saveCredential: (groupId, apiKey) => requestJson(`/api/model-groups/${encodeURIComponent(groupId)}/credential`, {
    method: 'PUT',
    body: JSON.stringify({ api_key: apiKey }),
  }),
  deleteCredential: (groupId) => requestJson(`/api/model-groups/${encodeURIComponent(groupId)}/credential`, {
    method: 'DELETE',
  }),
}
