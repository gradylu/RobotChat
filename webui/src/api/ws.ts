// WebSocket 客户端：协议版本握手、断线重连与事件分发（T-6.1 / T-6.5）

import { applyEvent, sessionState } from '../store'
import type { Envelope } from '../types'
import { getToken } from './client'

const PROTOCOL_VERSION = 1
const MAX_DELAY_MS = 10_000

export class SessionChannel {
  private socket: WebSocket | null = null
  private sessionId = ''
  private delay = 1_000
  private timer: number | null = null
  private manuallyClosed = false

  get active(): boolean {
    return this.socket !== null && this.socket.readyState === WebSocket.OPEN
  }

  connect(sessionId: string): void {
    this.sessionId = sessionId
    this.manuallyClosed = false
    this.open()
  }

  private open(): void {
    const socket = new WebSocket(this.url())
    this.socket = socket
    socket.onopen = () => {
      sessionState.connected = true
      sessionState.sessionId = this.sessionId
      this.delay = 1_000
    }
    socket.onmessage = (event: MessageEvent<string>) => {
      try {
        applyEvent(JSON.parse(event.data) as Envelope)
      } catch {
        sessionState.lastError = '收到非 JSON 消息'
      }
    }
    socket.onclose = () => {
      sessionState.connected = false
      this.socket = null
      if (!this.manuallyClosed) this.scheduleReconnect()
    }
    socket.onerror = () => {
      sessionState.lastError = 'WebSocket 连接异常'
    }
  }

  private scheduleReconnect(): void {
    if (this.timer !== null) return
    this.timer = window.setTimeout(() => {
      this.timer = null
      this.open()
      this.delay = Math.min(this.delay * 2, MAX_DELAY_MS)
    }, this.delay)
  }

  private url(): string {
    const scheme = window.location.protocol === 'https:' ? 'wss' : 'ws'
    const token = getToken()
    const query = token ? `?token=${encodeURIComponent(token)}` : ''
    return `${scheme}://${window.location.host}/ws/session/${encodeURIComponent(this.sessionId)}${query}`
  }

  send(type: string, payload: Record<string, unknown> = {}): boolean {
    if (!this.active || this.socket === null) return false
    this.socket.send(
      JSON.stringify({
        v: PROTOCOL_VERSION,
        type,
        ts: Date.now(),
        turn_id: null,
        payload,
      }),
    )
    return true
  }

  close(): void {
    this.manuallyClosed = true
    if (this.timer !== null) {
      window.clearTimeout(this.timer)
      this.timer = null
    }
    this.socket?.close()
    this.socket = null
    sessionState.connected = false
  }
}

export const sessionChannel = new SessionChannel()
