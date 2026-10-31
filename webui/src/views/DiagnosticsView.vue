<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { api, ApiError } from '../api/client'
import { sessionState } from '../store'
import type { ProvidersResponse, SelfCheckResponse } from '../types'

// 运维诊断页（T-6.11 / C-99、C-123）：自检、音频回环、provider 测试、强制复位、日志导出
const health = ref<Record<string, any>>({})
const config = ref<Record<string, any>>({})
const providers = ref<ProvidersResponse | null>(null)
const selfcheck = ref<SelfCheckResponse | null>(null)
const loopback = ref<Record<string, any> | null>(null)
const notice = ref('')
const busy = ref('')
const providerKind = ref('asr')
const providerDriver = ref('')
const duration = ref(1.5)

const driverOptions = computed(() => {
  const section = (config.value.providers as Record<string, any> | undefined)?.[providerKind.value]
  return Object.keys(section?.drivers ?? {})
})

function statusClass(status?: string): string {
  if (status === 'PASS' || status === 'ok') return 'ok'
  if (status === 'WARN' || status === 'degraded') return 'warn'
  if (status === 'FAIL' || status === 'unavailable') return 'error'
  return ''
}

function handle(error: unknown): void {
  notice.value = error instanceof ApiError ? `${error.message}` : (error as Error).message
}

async function load(): Promise<void> {
  try {
    const [healthData, providersData, configData] = await Promise.all([
      api.health(),
      api.providers(),
      api.getConfig(),
    ])
    health.value = healthData
    providers.value = providersData
    config.value = configData
    const names = Object.keys(providersData.providers ?? {})
    if (names.length && !names.includes(providerKind.value)) providerKind.value = names[0]
  } catch (error) {
    handle(error)
  }
}

async function runSelfcheck(): Promise<void> {
  busy.value = 'selfcheck'
  try {
    selfcheck.value = await api.selfcheck()
    notice.value = `自检完成：${selfcheck.value.overall}`
  } catch (error) {
    handle(error)
  } finally {
    busy.value = ''
  }
}

async function runLoopback(): Promise<void> {
  busy.value = 'loopback'
  try {
    loopback.value = await api.loopback(duration.value)
    notice.value = loopback.value.ok ? '音频回环通过' : `音频回环失败：${loopback.value.detail ?? ''}`
  } catch (error) {
    handle(error)
  } finally {
    busy.value = ''
  }
}

async function testProvider(): Promise<void> {
  if (!providerDriver.value) {
    notice.value = '请选择要测试的驱动'
    return
  }
  busy.value = 'test'
  try {
    loopback.value = await api.testProvider(providerKind.value, providerDriver.value)
    notice.value = `驱动测试完成：${providerKind.value}/${providerDriver.value}`
    await load()
  } catch (error) {
    handle(error)
  } finally {
    busy.value = ''
  }
}

async function resetSession(): Promise<void> {
  busy.value = 'reset'
  const id = sessionState.sessionId || 'default'
  try {
    const result = await api.resetSession(id)
    notice.value = `已强制复位：${result.state ?? 'done'}`
  } catch (error) {
    handle(error)
  } finally {
    busy.value = ''
  }
}

async function exportLogs(): Promise<void> {
  try {
    const blob = await api.downloadLogs(sessionState.sessionId || undefined)
    const url = URL.createObjectURL(blob)
    const anchor = document.createElement('a')
    anchor.href = url
    anchor.download = 'robotchat-logs.jsonl'
    anchor.click()
    URL.revokeObjectURL(url)
    notice.value = '日志已导出（JSONL）'
  } catch (error) {
    handle(error)
  }
}

onMounted(() => {
  void load()
})
</script>

<template>
  <section class="stack">
    <div class="card">
      <div class="card-title">
        <span>系统健康</span>
        <span class="badge" :class="statusClass(health.status)">{{ health.status ?? '—' }}</span>
      </div>
      <dl class="kv">
        <dt>就绪</dt>
        <dd>{{ health.ready ? '是' : '否' }}</dd>
        <dt>运行时长</dt>
        <dd>{{ health.uptime_s ?? 0 }} s</dd>
        <dt>离线状态</dt>
        <dd>{{ health.subsystems?.offline?.state ?? '—' }} {{ health.subsystems?.offline?.detail }}</dd>
        <dt>音频传输</dt>
        <dd>{{ health.subsystems?.audio?.transport ?? '—' }}</dd>
        <dt>AEC 实际模式</dt>
        <dd>{{ health.subsystems?.audio?.aec?.effective ?? '—' }}（请求 {{ health.subsystems?.audio?.aec?.requested ?? '—' }}）</dd>
        <dt>驱动</dt>
        <dd class="small">
          <span v-for="(item, key) in health.subsystems?.drivers ?? {}" :key="key" class="badge" :class="statusClass(item.state)" style="margin-right: 6px">
            {{ key }}: {{ item.state }}
          </span>
        </dd>
      </dl>
    </div>

    <div class="grid cols-2">
      <div class="card">
        <div class="card-title">
          <span>一键自检</span>
          <button class="btn btn-primary" :disabled="busy === 'selfcheck'" @click="runSelfcheck">运行自检</button>
        </div>
        <div class="scroll">
          <table>
            <thead><tr><th>检查项</th><th>结论</th><th>说明 / 建议</th></tr></thead>
            <tbody>
              <tr v-for="item in selfcheck?.items ?? []" :key="item.name">
                <td>{{ item.name }}</td>
                <td><span class="badge" :class="statusClass(item.status)">{{ item.status }}</span></td>
                <td class="small muted">{{ item.detail }} {{ item.suggestion }}</td>
              </tr>
              <tr v-if="!selfcheck">
                <td colspan="3" class="small muted">尚未运行自检</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>

      <div class="card">
        <div class="card-title"><span>音频回环 / 驱动测试</span></div>
        <div class="stack">
          <div class="row">
            <div class="field" style="width: 140px">
              <span>回环时长（秒）</span>
              <input v-model.number="duration" class="input" type="number" step="0.5" min="0.5" />
            </div>
            <button class="btn" :disabled="busy === 'loopback'" @click="runLoopback">音频回环</button>
          </div>
          <div class="row">
            <div class="field" style="width: 140px">
              <span>驱动类别</span>
              <select v-model="providerKind" class="select">
                <option v-for="(_, key) in providers?.providers ?? {}" :key="key" :value="key">{{ key }}</option>
              </select>
            </div>
            <div class="field" style="width: 160px">
              <span>驱动</span>
              <select v-model="providerDriver" class="select">
                <option v-for="name in driverOptions" :key="name" :value="name">{{ name }}</option>
              </select>
            </div>
            <button class="btn" :disabled="busy === 'test'" @click="testProvider">测试驱动</button>
          </div>
          <pre v-if="loopback" class="mono card" style="white-space: pre-wrap; box-shadow: none">{{ JSON.stringify(loopback, null, 2) }}</pre>
        </div>
      </div>
    </div>

    <div class="card">
      <div class="card-title"><span>运维动作</span><span class="small muted">{{ notice || '所有动作均写入事件日志' }}</span></div>
      <div class="row">
        <button class="btn btn-danger" :disabled="busy === 'reset'" @click="resetSession">强制复位会话</button>
        <button class="btn" @click="exportLogs">导出日志</button>
        <button class="btn" @click="load">刷新健康</button>
      </div>
    </div>
  </section>
</template>
