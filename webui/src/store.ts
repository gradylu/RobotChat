// 实时会话状态仓库：WS 事件驱动的单一数据源（T-6.6 / T-6.10）

import { reactive } from 'vue'
import type {
  BargeInEvent,
  Envelope,
  MetricsResponse,
  StageSummary,
  WakeSnapshot,
} from './types'

export interface TimelineItem {
  type: string
  ts: number
  turnId: number | null
  text: string
  tone: 'info' | 'ok' | 'warn' | 'error'
}

export interface TranscriptItem {
  turnId: number | null
  text: string
  lang: string
  ts: number
}

const MAX_TIMELINE = 200

export const sessionState = reactive({
  connected: false,
  sessionId: '',
  state: 'UNKNOWN',
  running: false,
  micMuted: false,
  asrPartial: '',
  asrLang: '',
  rmsDb: -60,
  noiseFloorDb: -60,
  speaking: false,
  finals: [] as TranscriptItem[],
  llmText: '',
  playback: { playing: false, queuedMs: 0 },
  bargeIns: [] as BargeInEvent[],
  timeline: [] as TimelineItem[],
  wake: null as WakeSnapshot | null,
  stages: {} as Record<string, StageSummary>,
  counters: {} as Record<string, number>,
  lastError: '',
})

function pushTimeline(item: Omit<TimelineItem, 'ts'>): void {
  sessionState.timeline.unshift({ ...item, ts: Date.now() })
  if (sessionState.timeline.length > MAX_TIMELINE) {
    sessionState.timeline.length = MAX_TIMELINE
  }
}

function accountBargeIn(): void {
  // 打断成功率的本地统计（服务端计数由 metrics 事件覆盖）
  sessionState.counters.barge_in_ui = sessionState.bargeIns.filter((item) => item.accepted).length
}

export function applyEvent(envelope: Envelope): void {
  const payload = envelope.payload ?? {}
  const turnId = envelope.turn_id
  switch (envelope.type) {
    case 'session.snapshot': {
      const snapshot = (payload.snapshot ?? {}) as Record<string, unknown>
      sessionState.sessionId = String(payload.active_session_id ?? payload.session_id ?? '')
      sessionState.state = String(snapshot.state ?? payload.state ?? 'UNKNOWN')
      sessionState.running = Boolean(snapshot.running ?? payload.running)
      sessionState.wake = (payload.wake as WakeSnapshot | null) ?? sessionState.wake
      sessionState.counters = { ...(payload.metrics as Record<string, number> | undefined) }
      pushTimeline({ type: 'session.snapshot', turnId, text: '连接已建立', tone: 'ok' })
      break
    }
    case 'state.changed': {
      sessionState.state = String(payload.to ?? sessionState.state)
      sessionState.running = !['IDLE', 'BOOTING', 'ERROR'].includes(sessionState.state)
      if (sessionState.state === 'LISTENING') {
        sessionState.asrPartial = ''
      }
      if (sessionState.state === 'SPEAKING') {
        sessionState.llmText = ''
      }
      pushTimeline({
        type: 'state.changed',
        turnId,
        text: `${payload.from ?? '?'} → ${payload.to ?? '?'}（${payload.event ?? payload.reason ?? '自动'}）`,
        tone: sessionState.state === 'ERROR' ? 'error' : 'info',
      })
      break
    }
    case 'wake.detected':
      sessionState.wake = { ...(sessionState.wake ?? {}), state: 'detected' }
      pushTimeline({
        type: 'wake.detected',
        turnId,
        text: `唤醒命中：${payload.phrase ?? ''}（score ${Number(payload.score ?? 0).toFixed(2)}，${payload.latency_ms ?? '?'} ms）`,
        tone: 'ok',
      })
      break
    case 'wake.active_window':
      sessionState.wake = {
        ...(sessionState.wake ?? {}),
        active_window_remaining_s: Math.round(Number(payload.remaining_ms ?? 0) / 100) / 10,
      }
      break
    case 'vad.level':
      sessionState.rmsDb = Number(payload.rms_db ?? -60)
      sessionState.noiseFloorDb = Number(payload.noise_floor_db ?? -60)
      sessionState.speaking = Boolean(payload.speaking)
      break
    case 'asr.partial':
      sessionState.asrPartial = String(payload.text ?? '')
      sessionState.asrLang = String(payload.lang ?? '')
      break
    case 'asr.final': {
      const text = String(payload.text ?? '').trim()
      sessionState.asrPartial = ''
      sessionState.asrLang = String(payload.lang ?? '')
      if (text) {
        sessionState.finals.push({
          turnId,
          text,
          lang: sessionState.asrLang,
          ts: Date.now(),
        })
        if (sessionState.finals.length > 50) sessionState.finals.shift()
      }
      pushTimeline({
        type: 'asr.final',
        turnId,
        text: `ASR：${text || '（空）'}（${payload.duration_ms ?? 0} ms）`,
        tone: 'info',
      })
      break
    }
    case 'language.changed':
      pushTimeline({
        type: 'language.changed',
        turnId,
        text: `语言：${payload.from ?? '?'} → ${payload.to ?? '?'}（${payload.reason ?? 'auto'}）`,
        tone: 'info',
      })
      break
    case 'llm.delta':
      sessionState.llmText += String(payload.text ?? '')
      break
    case 'llm.tool_call':
      pushTimeline({
        type: 'llm.tool_call',
        turnId,
        text: `工具调用：${payload.name ?? ''}(${JSON.stringify(payload.args ?? {})})`,
        tone: 'warn',
      })
      break
    case 'playback.state':
      sessionState.playback = {
        playing: Boolean(payload.playing),
        queuedMs: Number(payload.queued_ms ?? 0),
      }
      break
    case 'barge_in': {
      const accepted = Boolean(payload.accepted)
      sessionState.bargeIns.unshift({
        trigger: String(payload.trigger ?? ''),
        stage_latency: (payload.stage_latency as Record<string, number>) ?? {},
        accepted,
        ts: Date.now(),
        turn_id: turnId,
      })
      if (sessionState.bargeIns.length > 50) sessionState.bargeIns.pop()
      accountBargeIn()
      pushTimeline({
        type: 'barge_in',
        turnId,
        text: `打断${accepted ? '接受' : '拒绝'}：${payload.trigger ?? ''}（${payload.stage_latency ? JSON.stringify(payload.stage_latency) : '-'}）`,
        tone: accepted ? 'warn' : 'info',
      })
      break
    }
    case 'tool.result':
      pushTimeline({
        type: 'tool.result',
        turnId,
        text: `工具结果：${payload.name ?? ''} → ${payload.status ?? ''}`,
        tone: payload.status === 'ok' ? 'ok' : 'warn',
      })
      break
    case 'metrics.snapshot': {
      sessionState.stages = (payload.stages as Record<string, StageSummary>) ?? sessionState.stages
      if (payload.counters) {
        sessionState.counters = payload.counters as Record<string, number>
      }
      break
    }
    case 'mic.state':
      sessionState.micMuted = Boolean(payload.muted)
      pushTimeline({
        type: 'mic.state',
        turnId,
        text: sessionState.micMuted ? '麦克风静音' : '麦克风恢复',
        tone: 'info',
      })
      break
    case 'error':
      sessionState.lastError = String(payload.message ?? '未知错误')
      pushTimeline({
        type: 'error',
        turnId,
        text: `错误：${sessionState.lastError}`,
        tone: 'error',
      })
      break
    case 'command.ack':
      if (!payload.accepted) {
        sessionState.lastError = String(payload.reason ?? '指令被拒绝')
        pushTimeline({ type: 'command.ack', turnId, text: `指令被拒绝：${sessionState.lastError}`, tone: 'warn' })
      }
      break
    default:
      break
  }
}

export function applyMetrics(response: MetricsResponse): void {
  sessionState.stages = response.stages
  sessionState.counters = response.counters
}

export function resetSessionState(): void {
  sessionState.asrPartial = ''
  sessionState.llmText = ''
  sessionState.finals.splice(0, sessionState.finals.length)
  sessionState.timeline.splice(0, sessionState.timeline.length)
  sessionState.bargeIns.splice(0, sessionState.bargeIns.length)
}
