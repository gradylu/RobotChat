// WebUI 与后端共享的数据模型（REST 响应 / WS 事件，T-6.5）

export interface Envelope<T = Record<string, unknown>> {
  v: number
  type: string
  ts: number
  turn_id: number | null
  payload: T
}

export interface DriverSnapshot {
  kind?: string
  state?: string
  primary?: { name?: string; usable?: boolean; health?: { state?: string; detail?: string } | null }
  fallback?: { name?: string; usable?: boolean } | null
  breaker?: { state?: string; failures?: number; cooldown_remaining_s?: number }
  last_error?: string
  capabilities?: Record<string, unknown>
  features?: { streaming?: boolean; tools_enabled?: boolean; notes?: string[] }
}

export interface ProvidersResponse {
  providers: Record<string, DriverSnapshot>
  drivers: Record<string, { kind: string; active: string; state: string; detail?: string; circuit?: string; effective?: string }>
  offline: { state: string; detail: string }
}

export interface StageSummary {
  stage: string
  count: number
  p50: number
  p90: number
  p95: number
  p99: number
  min: number
  max: number
}

export interface MetricsResponse {
  stages: Record<string, StageSummary>
  counters: Record<string, number>
  rag: Record<string, unknown>
  languages: Record<string, unknown>
  barge_in: { counters: Record<string, number>; last: Record<string, unknown> | null }
  errors: number
}

export interface WakeSnapshot {
  enabled?: boolean
  state?: string
  active_window_ms?: number
  active_window_remaining_s?: number | null
  counters?: Record<string, number>
  kws?: Record<string, unknown> | null
}

export interface LanguageSnapshot {
  mode: string
  current: string
  fixed: string
  fallback: string
  supported: string[]
  pending?: string | null
  pending_count?: number
}

export interface ToolInfo {
  name: string
  risk?: string
  description?: string
  requires_confirm?: boolean
  enabled?: boolean
  [key: string]: unknown
}

export interface ToolResult {
  status: string
  detail?: string
  result?: unknown
  duration_ms?: number
  [key: string]: unknown
}

export interface SessionInfo {
  id: string
  created_at?: number
  ended_at?: number | null
  state?: string
  turn_count?: number
  [key: string]: unknown
}

export interface MessageInfo {
  id?: number
  session_id?: string
  role: string
  content: string
  turn_id?: number
  ts?: number
}

export interface TurnInfo {
  seq?: number
  lang?: string
  user_text?: string
  assistant_text?: string
  status?: string
  timings?: Record<string, number>
  barge_in?: boolean
  [key: string]: unknown
}

export interface SelfCheckItem {
  name: string
  status: 'PASS' | 'WARN' | 'FAIL'
  detail?: string
  suggestion?: string
}

export interface SelfCheckResponse {
  ok: boolean
  overall: string
  counts: Record<string, number>
  items: SelfCheckItem[]
  generated_at?: number
}

export interface BargeInEvent {
  trigger?: string
  stage_latency?: Record<string, number>
  accepted?: boolean
  ts: number
  turn_id: number | null
}

export interface StreamEvent {
  type: string
  ts: number
  turn_id: number | null
  payload: Record<string, unknown>
}
