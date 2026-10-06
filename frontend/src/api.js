// Typed-ish API client for the RAT backend.

export class ApiError extends Error {
  constructor(status, detail, payload) {
    super(typeof detail === 'string' ? detail : detail?.detail || `Request failed (${status})`)
    this.status = status
    this.detail = detail
    this.payload = payload
  }
}

async function request(path, { method = 'GET', body, isForm = false } = {}) {
  const opts = { method, headers: {} }
  if (body !== undefined) {
    if (isForm) {
      opts.body = body
    } else {
      opts.headers['Content-Type'] = 'application/json'
      opts.body = JSON.stringify(body)
    }
  }
  let res
  try {
    res = await fetch(path, opts)
  } catch (err) {
    throw new ApiError(0, 'Cannot reach the RAT server. Is it running?')
  }
  if (res.status === 204) return null
  const text = await res.text()
  let data = null
  try { data = text ? JSON.parse(text) : null } catch { data = text }
  if (!res.ok) throw new ApiError(res.status, data, data)
  return data
}

const qs = (params = {}) => {
  const sp = new URLSearchParams()
  Object.entries(params).forEach(([k, v]) => {
    if (v !== undefined && v !== null && v !== '') sp.set(k, v)
  })
  const s = sp.toString()
  return s ? `?${s}` : ''
}

export const api = {
  health: () => request('/api/health'),
  listRepos: () => request('/api/repos'),
  addRepoUrl: (url) => request('/api/repos/url', { method: 'POST', body: { url } }),
  addRepoZip: (file) => {
    const fd = new FormData()
    fd.append('file', file, file.name)
    return request('/api/repos/zip', { method: 'POST', body: fd, isForm: true })
  },
  getRepo: (id) => request(`/api/repos/${id}`),
  reanalyze: (id) => request(`/api/repos/${id}/reanalyze`, { method: 'POST' }),
  deleteRepo: (id) => request(`/api/repos/${id}`, { method: 'DELETE' }),
  summary: (id) => request(`/api/repos/${id}/summary`),
  authors: (id) => request(`/api/repos/${id}/authors`),
  mergeAuthors: (id, members, into) =>
    request(`/api/repos/${id}/authors/merge`, { method: 'POST', body: { members, into } }),
  unmergeAuthors: (id, members) =>
    request(`/api/repos/${id}/authors/unmerge`, { method: 'POST', body: { members } }),
  applyMailmap: (id) => request(`/api/repos/${id}/authors/apply-mailmap`, { method: 'POST' }),
  metrics: (id, body) => request(`/api/repos/${id}/metrics`, { method: 'POST', body }),
  objects: (id, body, params) =>
    request(`/api/repos/${id}/objects${qs(params)}`, { method: 'POST', body }),
  hotspots: (id, body, params) =>
    request(`/api/repos/${id}/hotspots${qs(params)}`, { method: 'POST', body }),
  tree: (id, body, params) => request(`/api/repos/${id}/tree${qs(params)}`, { method: 'POST', body }),
  search: (id, q) => request(`/api/repos/${id}/search${qs({ q })}`),
  commits: (id, params) => request(`/api/repos/${id}/commits${qs(params)}`),
}

export function exportUrl(repoId, dataset, fmt, body) {
  // POST body handled via a hidden form for large commit lists; keep simple:
  // exports are triggered with fetch + blob download in the UI.
  return `/api/repos/${repoId}/export${qs({ dataset, fmt })}`
}

export async function downloadExport(repoId, { dataset, fmt, body }) {
  const res = await fetch(`/api/repos/${repoId}/export${qs({ dataset, fmt })}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!res.ok) {
    let detail = `Export failed (${res.status})`
    try { detail = (await res.json()).detail ?? detail } catch { /* ignore */ }
    throw new ApiError(res.status, detail)
  }
  const blob = await res.blob()
  const cd = res.headers.get('Content-Disposition') || ''
  const m = cd.match(/filename="([^"]+)"/)
  const name = m ? m[1] : `export.${fmt}`
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = name
  document.body.appendChild(a)
  a.click()
  a.remove()
  URL.revokeObjectURL(url)
}
