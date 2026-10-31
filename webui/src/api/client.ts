// REST 客户端：统一口令注入、错误归一化与下载（T-6.4 / T-6.5）

const TOKEN_KEY = 'robotchat.token'

export class ApiError extends Error {
  status: number
  payload: Record<string, unknown>

  constructor(status: number, message: string, payload: Record<string, unknown> = {}) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.payload = payload
  }
}

export function getToken(): string {
  return localStorage.getItem(TOKEN_KEY) ?? ''
}

export function setToken(token: string): void {
  if (token) {
    localStorage.setItem(TOKEN_KEY, token)
  } else {
    localStorage.removeItem(TOKEN_KEY)
  }
}

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const headers = new Headers(options.headers)
  headers.set('Accept', 'application/json')
  if (options.body !== undefined && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json')
  }
  const token = getToken()
  if (token) headers.set('X-API-Token', token)

  const response = await fetch(path, { ...options, headers })
  const text = await response.text()
  let body: unknown = null
  if (text) {
    try {
      body = JSON.parse(text)
    } catch {
      body = text
    }
  }
  if (!response.ok) {
    const payload = typeof body === 'object' && body !== null ? (body as Record<string, unknown>) : {}
    const detail =
      (payload.detail as string | undefined) ??
      (typeof body === 'string' ? body : `请求失败（HTTP ${response.status}）`)
    throw new ApiError(response.status, detail, payload)
  }
  return body as T
}

function jsonBody(value: unknown): RequestInit {
  return { method: 'POST', body: JSON.stringify(value ?? {}) }
}

export const api = {
  base: '/api',

  health: () => request<Record<string, any>>('/api/health'),
  ready: () => request<Record<string, any>>('/api/ready'),
  providers: () => request<import('../types').ProvidersResponse>('/api/providers'),
  switchProvider: (kind: string, driver: string) =>
    request<Record<string, any>>(`/api/providers/${kind}/switch`, jsonBody({ driver })),
  testProvider: (kind: string, driver: string) =>
    request<Record<string, any>>(`/api/providers/${kind}/${driver}/test`, { method: 'POST' }),

  getConfig: () => request<Record<string, any>>('/api/config'),
  putConfig: (patch: Record<string, unknown>) =>
    request<Record<string, any>>('/api/config', { method: 'PUT', body: JSON.stringify(patch) }),
  rollbackConfig: () =>
    request<Record<string, any>>('/api/config/rollback', { method: 'POST' }),

  authStatus: () => request<Record<string, any>>('/api/auth/status'),

  tools: () => request<Record<string, any>>('/api/tools'),
  invokeTool: (name: string, args: Record<string, unknown>) =>
    request<Record<string, any>>(`/api/tools/${name}/invoke`, jsonBody({ args })),

  metrics: () => request<import('../types').MetricsResponse>('/api/metrics'),
  wake: () => request<import('../types').WakeSnapshot>('/api/wake'),
  markFalseWake: () => request<Record<string, any>>('/api/wake/false-positive', { method: 'POST' }),

  languages: () => request<import('../types').LanguageSnapshot>('/api/languages'),
  setLanguage: (payload: { mode?: string; lang?: string }) =>
    request<Record<string, any>>('/api/languages/active', jsonBody(payload)),

  bargeIn: () => request<Record<string, any>>('/api/barge-in'),
  setBargeIn: (patch: Record<string, unknown>) =>
    request<Record<string, any>>('/api/barge-in', jsonBody(patch)),

  sessions: (limit = 200) => request<Record<string, any>>(`/api/sessions?limit=${limit}`),
  createSession: (sessionId?: string) =>
    request<Record<string, any>>('/api/sessions', jsonBody({ session_id: sessionId })),
  session: (id: string) => request<Record<string, any>>(`/api/sessions/${encodeURIComponent(id)}`),
  deleteSession: (id: string) =>
    request<Record<string, any>>(`/api/sessions/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  turns: (id: string) =>
    request<Record<string, any>>(`/api/sessions/${encodeURIComponent(id)}/turns`),
  deleteTurn: (id: string, seq: number) =>
    request<Record<string, any>>(`/api/sessions/${encodeURIComponent(id)}/turns/${seq}`, {
      method: 'DELETE',
    }),
  messages: (id: string, limit = 100) =>
    request<Record<string, any>>(
      `/api/sessions/${encodeURIComponent(id)}/messages?limit=${limit}`,
    ),
  clearMessages: (id: string, turnId?: number) => {
    const suffix = turnId === undefined ? '' : `?turn_id=${turnId}`
    return request<Record<string, any>>(
      `/api/sessions/${encodeURIComponent(id)}/messages${suffix}`,
      { method: 'DELETE' },
    )
  },

  events: (id?: string, limit = 500) => {
    const query = new URLSearchParams({ limit: String(limit) })
    if (id) query.set('session_id', id)
    return request<Record<string, any>>(`/api/logs/events?${query.toString()}`)
  },
  downloadLogs: async (id?: string) => {
    const query = new URLSearchParams()
    if (id) query.set('session_id', id)
    const headers = new Headers()
    const token = getToken()
    if (token) headers.set('X-API-Token', token)
    const response = await fetch(`/api/logs/download?${query.toString()}`, { headers })
    if (!response.ok) throw new ApiError(response.status, `下载失败（HTTP ${response.status}）`)
    return response.blob()
  },

  selfcheck: () => request<import('../types').SelfCheckResponse>('/api/diagnostics/selfcheck', { method: 'POST' }),
  loopback: (durationS = 1.5) =>
    request<Record<string, any>>(`/api/diagnostics/audio-loopback?duration_s=${durationS}`, {
      method: 'POST',
    }),
  resetSession: (id: string) =>
    request<Record<string, any>>(`/api/session/${encodeURIComponent(id)}/reset`, { method: 'POST' }),
}
