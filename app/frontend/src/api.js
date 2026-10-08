async function request(path, params = {}) {
  const query = new URLSearchParams(
    Object.entries(params).filter(([, value]) => value !== '' && value !== undefined && value !== null),
  )
  const response = await fetch(`/api/${path}${query.size ? `?${query}` : ''}`)
  const body = await response.json().catch(() => ({}))
  if (!response.ok) {
    throw new Error(body.error || `Request failed (${response.status})`)
  }
  return body
}

export const api = {
  patients: (params) => request('patients', params),
  patient: (id) => request(`patients/${id}`),
  observations: (id, params) => request(`patients/${id}/observations`, params),
  latestReport: () => request('runs/latest'),
}
