<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { api } from '../api/client'
import { applyMetrics, sessionState } from '../store'
import type { MetricsResponse, StageSummary } from '../types'

// 监控页（T-6.10 / C-46、C-96）：延迟分位数、打断成功率、错误率、状态流转日志
const metrics = ref<MetricsResponse | null>(null)
const notice = ref('')
let timer: number | null = null

const stageOrder = [
  'endpoint',
  'asr_partial',
  'asr_final',
  'llm_first_token',
  'tts_first_chunk',
  'e2e_first_response',
  'barge_in',
  'tool_execution',
  'wake_latency_ms',
]

const stages = computed<StageSummary[]>(() => {
  const source = metrics.value?.stages ?? {}
  const rows = Object.values(source)
  return rows.sort((a, b) => {
    const left = stageOrder.indexOf(a.stage)
    const right = stageOrder.indexOf(b.stage)
    return (left < 0 ? 99 : left) - (right < 0 ? 99 : right)
  })
})

const counters = computed(() => metrics.value?.counters ?? {})
const bargeAccepted = computed(() => counters.value.barge_in_accepted ?? 0)
const bargeRejected = computed(() => counters.value.barge_in_rejected ?? 0)
const bargeSuppressed = computed(() => counters.value.barge_in_echo_suppressed ?? 0)
const bargeRate = computed(() => {
  const total = bargeAccepted.value + bargeRejected.value
  if (!total) return '—'
  return `${((bargeAccepted.value / total) * 100).toFixed(1)}%`
})
const errorCount = computed(() => metrics.value?.errors ?? 0)
const wakeCount = computed(() => counters.value.wake_count ?? 0)
const falsePositive = computed(() => counters.value.wake_false_positive ?? 0)
const stateEvents = computed(() => sessionState.timeline.filter((item) => item.type === 'state.changed'))

async function refresh(): Promise<void> {
  try {
    const data = await api.metrics()
    metrics.value = data
    applyMetrics(data)
    notice.value = ''
  } catch (error) {
    notice.value = (error as Error).message
  }
}

onMounted(() => {
  void refresh()
  timer = window.setInterval(() => {
    void refresh()
  }, 5000)
})

onBeforeUnmount(() => {
  if (timer !== null) window.clearInterval(timer)
})
</script>

<template>
  <section class="stack">
    <div class="grid cols-3">
      <div class="card">
        <div class="card-title"><span>打断成功率</span><span class="small muted">接受 / (接受 + 拒绝)</span></div>
        <div style="font-size: 30px; font-weight: 700">{{ bargeRate }}</div>
        <dl class="kv" style="margin-top: 10px">
          <dt>接受</dt><dd>{{ bargeAccepted }}</dd>
          <dt>拒绝</dt><dd>{{ bargeRejected }}</dd>
          <dt>回声抑制</dt><dd>{{ bargeSuppressed }}</dd>
        </dl>
      </div>
      <div class="card">
        <div class="card-title"><span>错误与唤醒</span><span class="small muted">累计计数</span></div>
        <div style="font-size: 30px; font-weight: 700">{{ errorCount }}</div>
        <dl class="kv" style="margin-top: 10px">
          <dt>唤醒命中</dt><dd>{{ wakeCount }}</dd>
          <dt>误唤醒</dt><dd>{{ falsePositive }}</dd>
          <dt>活跃窗口过期</dt><dd>{{ counters.active_window_expired ?? 0 }}</dd>
        </dl>
      </div>
      <div class="card">
        <div class="card-title"><span>采集状态</span><span class="small muted">{{ notice || '每 5s 自动刷新' }}</span></div>
        <dl class="kv">
          <dt>会话状态</dt><dd>{{ sessionState.state }}</dd>
          <dt>ASR 阶段样本</dt><dd>{{ metrics?.stages?.asr_final?.count ?? 0 }}</dd>
          <dt>首响样本</dt><dd>{{ metrics?.stages?.e2e_first_response?.count ?? 0 }}</dd>
          <dt>RAG</dt><dd class="small">{{ JSON.stringify(metrics?.rag ?? {}) }}</dd>
        </dl>
      </div>
    </div>

    <div class="card">
      <div class="card-title">
        <span>延迟分位数（ms）</span>
        <span class="row">
          <button class="btn" @click="refresh">立即刷新</button>
        </span>
      </div>
      <div class="scroll">
        <table>
          <thead>
            <tr><th>阶段</th><th>样本</th><th>P50</th><th>P90</th><th>P95</th><th>P99</th><th>最小</th><th>最大</th></tr>
          </thead>
          <tbody>
            <tr v-for="stage in stages" :key="stage.stage">
              <td class="mono">{{ stage.stage }}</td>
              <td>{{ stage.count }}</td>
              <td>{{ stage.p50 }}</td>
              <td>{{ stage.p90 }}</td>
              <td>{{ stage.p95 }}</td>
              <td>{{ stage.p99 }}</td>
              <td>{{ stage.min }}</td>
              <td>{{ stage.max }}</td>
            </tr>
            <tr v-if="!stages.length">
              <td colspan="8" class="small muted">暂无延迟样本</td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <div class="grid cols-2">
      <div class="card">
        <div class="card-title"><span>计数指标</span><span class="small muted">FR-14.13</span></div>
        <div class="scroll">
          <table>
            <thead><tr><th>指标</th><th>值</th></tr></thead>
            <tbody>
              <tr v-for="(value, key) in counters" :key="key">
                <td class="mono">{{ key }}</td>
                <td>{{ value }}</td>
              </tr>
              <tr v-if="!Object.keys(counters).length"><td colspan="2" class="small muted">暂无计数</td></tr>
            </tbody>
          </table>
        </div>
      </div>

      <div class="card">
        <div class="card-title"><span>状态流转日志</span><span class="small muted">来自实时事件流</span></div>
        <div class="timeline">
          <div v-for="(item, index) in stateEvents" :key="index" class="timeline-item" :class="item.tone">
            <span class="mono small muted">{{ new Date(item.ts).toLocaleTimeString() }}</span>
            <span>{{ item.text }}</span>
          </div>
          <span v-if="!stateEvents.length" class="small muted">暂无状态流转；打开实时会话页后开始记录</span>
        </div>
      </div>
    </div>
  </section>
</template>
