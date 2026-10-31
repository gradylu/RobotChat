<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import Controls from '../components/Controls.vue'
import { api } from '../api/client'
import { sessionState } from '../store'
import type { LanguageSnapshot, WakeSnapshot } from '../types'

// 实时会话面板（T-6.6）：状态机、唤醒窗口、语种、音频活动、转写与打断时间线
const wake = ref<WakeSnapshot | null>(null)
const language = ref<LanguageSnapshot | null>(null)
const recentFinals = computed(() => sessionState.finals.slice(-12))
const levelPercent = computed(() => {
  const min = -60
  return Math.max(0, Math.min(100, ((sessionState.rmsDb - min) / -min) * 100))
})
const stateClass = computed(() => {
  if (['ERROR', 'DEGRADED'].includes(sessionState.state)) return 'error'
  if (['LISTENING', 'THINKING', 'SPEAKING', 'EXECUTING', 'WAKING'].includes(sessionState.state)) return 'live'
  return 'warn'
})
const acceptedRate = computed(() => {
  const total = sessionState.bargeIns.length
  if (!total) return '—'
  const accepted = sessionState.bargeIns.filter((item) => item.accepted).length
  return `${((accepted / total) * 100).toFixed(0)}%`
})
const wakeRemaining = computed(() => {
  const value = sessionState.wake?.active_window_remaining_s ?? wake.value?.active_window_remaining_s
  return value === undefined || value === null ? '—' : `${value}s`
})

async function refresh(): Promise<void> {
  try {
    wake.value = await api.wake()
    language.value = await api.languages()
    if (!sessionState.wake) sessionState.wake = wake.value
  } catch {
    // 后端未就绪时由 WS 连接状态提示
  }
}

onMounted(() => {
  void refresh()
})
</script>

<template>
  <section class="stack">
    <Controls />

    <div class="grid cols-3">
      <div class="card">
        <div class="card-title"><span>状态机</span><span class="small muted">{{ sessionState.sessionId }}</span></div>
        <div class="row">
          <span class="state-badge" :class="stateClass">{{ sessionState.state }}</span>
          <span class="small muted">{{ sessionState.running ? '会话运行中' : '待机 / 未运行' }}</span>
        </div>
        <dl class="kv" style="margin-top: 12px">
          <dt>语言模式</dt>
          <dd>{{ language?.mode ?? '—' }}</dd>
          <dt>当前语种</dt>
          <dd>{{ sessionState.asrLang || language?.current || '—' }}</dd>
          <dt>活跃窗口剩余</dt>
          <dd>{{ wakeRemaining }}</dd>
          <dt>唤醒词</dt>
          <dd>{{ (wake?.enabled ?? sessionState.wake?.enabled) ? '已启用' : '未启用' }}</dd>
          <dt>麦克风</dt>
          <dd>{{ sessionState.micMuted ? '静音' : '收音中' }}</dd>
        </dl>
      </div>

      <div class="card">
        <div class="card-title"><span>音频活动</span><span class="badge" :class="sessionState.speaking ? 'ok' : ''">{{ sessionState.speaking ? '检测到语音' : '静默' }}</span></div>
        <div class="level-track">
          <div class="level-fill" :style="{ width: `${levelPercent}%` }"></div>
        </div>
        <dl class="kv" style="margin-top: 12px">
          <dt>当前电平</dt>
          <dd>{{ sessionState.rmsDb.toFixed(1) }} dBFS</dd>
          <dt>噪声底</dt>
          <dd>{{ sessionState.noiseFloorDb.toFixed(1) }} dBFS</dd>
          <dt>播报状态</dt>
          <dd>{{ sessionState.playback.playing ? '播放中' : '空闲' }}（队列 {{ sessionState.playback.queuedMs }} ms）</dd>
          <dt>打断接受率</dt>
          <dd>{{ acceptedRate }}</dd>
        </dl>
      </div>

      <div class="card">
        <div class="card-title"><span>打断时间线</span><span class="small muted">最近 {{ sessionState.bargeIns.length }} 次</span></div>
        <ul class="list scroll">
          <li v-for="(item, index) in sessionState.bargeIns.slice(0, 8)" :key="index" class="timeline-item" :class="item.accepted ? 'warn' : ''">
            <span class="mono small">{{ new Date(item.ts).toLocaleTimeString() }}</span>
            <span>{{ item.trigger }} · {{ item.accepted ? '已接受' : '已拒绝' }}</span>
            <span class="small muted">{{ JSON.stringify(item.stage_latency ?? {}) }}</span>
          </li>
          <li v-if="!sessionState.bargeIns.length" class="small muted">暂无打断事件</li>
        </ul>
      </div>
    </div>

    <div class="card">
      <div class="card-title">
        <span>对话转写</span>
        <span class="small muted">ASR 流式 + LLM 流式</span>
      </div>
      <div class="transcript">
        <div v-for="(item, index) in recentFinals" :key="index" class="bubble user">
          <span class="small muted">用户 · {{ item.lang || '—' }} · turn {{ item.turnId ?? '—' }}</span>
          <div>{{ item.text }}</div>
        </div>
        <div v-if="sessionState.asrPartial" class="bubble user" style="opacity: 0.75">
          <span class="small muted">识别中…</span>
          <div>{{ sessionState.asrPartial }}</div>
        </div>
        <div v-if="sessionState.llmText" class="bubble assistant">
          <span class="small muted">助手</span>
          <div>{{ sessionState.llmText }}</div>
        </div>
        <span v-if="!recentFinals.length && !sessionState.asrPartial && !sessionState.llmText" class="small muted">
          暂无对话内容；开始会话或直接输入文本即可。
        </span>
      </div>
    </div>

    <div class="card">
      <div class="card-title"><span>状态流转与事件</span><span class="small muted">{{ sessionState.lastError || '正常' }}</span></div>
      <div class="timeline">
        <div v-for="(item, index) in sessionState.timeline" :key="index" class="timeline-item" :class="item.tone">
          <span class="mono small muted">{{ new Date(item.ts).toLocaleTimeString() }}</span>
          <span>{{ item.text }}</span>
          <span class="small muted" style="margin-left: auto">{{ item.type }}</span>
        </div>
        <span v-if="!sessionState.timeline.length" class="small muted">暂无事件</span>
      </div>
    </div>
  </section>
</template>
