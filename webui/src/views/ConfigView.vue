<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { api, ApiError } from '../api/client'
import { sessionState } from '../store'
import type { ProvidersResponse } from '../types'

// 模型配置页（T-6.8）：驱动查看 / 热切换 / 参数编辑 / 连通性测试 / 应用与回滚
const providers = ref<ProvidersResponse | null>(null)
const config = ref<Record<string, any>>({})
const notice = ref('')
const busy = ref('')
const selections = ref<Record<string, string>>({})
const tests = ref<Record<string, Record<string, unknown>>>({})
const editing = ref<Record<string, string>>({})
const editingText = ref<Record<string, string>>({})

const kinds = ['asr', 'llm', 'tts', 'knowledge', 'wakeword'] as const

const barge = ref({
  guard_ms: 0,
  threshold_boost_db: 0,
  confirm_frames: 0,
  semantic_check: true,
  echo_similarity_threshold: 0,
})

const providerSections = computed(() =>
  kinds
    .filter((kind) => (config.value.providers as Record<string, any> | undefined)?.[kind])
    .map((kind) => {
      const section = (config.value.providers as Record<string, any>)[kind]
      const drivers = Object.keys(section.drivers ?? {})
      return {
        kind,
        section,
        drivers,
        active: String(section.active ?? ''),
        snapshot: providers.value?.providers?.[kind],
        driverStatus: providers.value?.drivers?.[kind],
      }
    }),
)

function report(message: string): void {
  notice.value = message
}

async function load(): Promise<void> {
  try {
    const [providersData, configData, bargeData] = await Promise.all([
      api.providers(),
      api.getConfig(),
      api.bargeIn(),
    ])
    providers.value = providersData
    config.value = configData
    for (const kind of kinds) {
      const section = (configData.providers as Record<string, any> | undefined)?.[kind]
      if (section) selections.value[kind] = String(section.active ?? '')
    }
    barge.value = {
      guard_ms: Number(bargeData.guard_ms ?? 0),
      threshold_boost_db: Number(bargeData.threshold_boost_db ?? 0),
      confirm_frames: Number(bargeData.confirm_frames ?? 0),
      semantic_check: Boolean(bargeData.semantic_check),
      echo_similarity_threshold: Number(bargeData.echo_similarity_threshold ?? 0),
    }
  } catch (error) {
    report((error as Error).message)
  }
}

async function switchDriver(kind: string): Promise<void> {
  const driver = selections.value[kind]
  if (!driver) return
  busy.value = `switch-${kind}`
  try {
    await api.switchProvider(kind, driver)
    report(`已切换 providers.${kind} → ${driver}（会话不中断）`)
    await load()
  } catch (error) {
    report((error as Error).message)
  } finally {
    busy.value = ''
  }
}

async function testDriver(kind: string, driver: string): Promise<void> {
  busy.value = `test-${kind}-${driver}`
  try {
    tests.value[`${kind}:${driver}`] = await api.testProvider(kind, driver)
    report(`连通性测试完成：${kind}/${driver}`)
  } catch (error) {
    tests.value[`${kind}:${driver}`] = { ok: false, detail: (error as Error).message }
    report((error as Error).message)
  } finally {
    busy.value = ''
  }
}

function toggleEdit(kind: string, driver: string): void {
  const key = `${kind}:${driver}`
  if (editing.value[key]) {
    delete editing.value[key]
    return
  }
  const section = (config.value.providers as Record<string, any>)[kind]
  editingText.value[key] = JSON.stringify(section.drivers?.[driver] ?? {}, null, 2)
  editing.value[key] = key
}

async function applyDriverParams(kind: string, driver: string): Promise<void> {
  const key = `${kind}:${driver}`
  let parsed: unknown
  try {
    parsed = JSON.parse(editingText.value[key] || '{}')
  } catch {
    report('参数不是合法 JSON')
    return
  }
  try {
    const result = await api.putConfig({ providers: { [kind]: { drivers: { [driver]: parsed } } } })
    report(
      `参数已应用（热生效：${(result.applied as string[]).join(', ') || '无'}；需重启：${(result.restart_required as string[]).join(', ') || '无'}）`,
    )
    delete editing.value[key]
    await load()
  } catch (error) {
    report(formatError(error))
  }
}

async function applyBargeIn(): Promise<void> {
  try {
    const result = await api.setBargeIn({ ...barge.value })
    report(`打断参数已热更新（${Object.keys(result).length} 项）`)
  } catch (error) {
    report(formatError(error))
  }
}

async function rollback(): Promise<void> {
  try {
    const result = await api.rollbackConfig()
    report(`已回滚到上一版本（热生效：${(result.applied as string[]).join(', ') || '无'}）`)
    await load()
  } catch (error) {
    report(formatError(error))
  }
}

function formatError(error: unknown): string {
  if (error instanceof ApiError && error.payload.errors) {
    return `${error.message}：${JSON.stringify(error.payload.errors)}`
  }
  return (error as Error).message
}

onMounted(() => {
  void load()
})
</script>

<template>
  <section class="stack">
    <div class="card">
      <div class="card-title">
        <span>配置版本</span>
        <span class="row">
          <span class="small muted">密钥只显示来源环境变量名</span>
          <button class="btn" @click="rollback">回滚上一版本</button>
          <button class="btn" @click="load">刷新</button>
        </span>
      </div>
      <dl class="kv">
        <dt>离线状态</dt>
        <dd>{{ providers?.offline?.state ?? '—' }} {{ providers?.offline?.detail }}</dd>
        <dt>环境</dt>
        <dd>{{ config.app?.env ?? '—' }} / {{ config.app?.name ?? '—' }}</dd>
        <dt>协议版本</dt>
        <dd>{{ config.api?.protocol_version ?? '—' }}</dd>
        <dt>AEC 模式</dt>
        <dd>{{ config.audio?.aec?.mode ?? '—' }}</dd>
      </dl>
      <p v-if="notice" class="small" style="margin: 12px 0 0">{{ notice }}</p>
    </div>

    <div class="grid cols-2">
      <div v-for="item in providerSections" :key="item.kind" class="card">
        <div class="card-title">
          <span>{{ item.kind.toUpperCase() }}</span>
          <span class="badge" :class="item.driverStatus?.state === 'ok' ? 'ok' : item.driverStatus?.state === 'unavailable' ? 'error' : 'warn'">
            {{ item.driverStatus?.effective || item.active }}
          </span>
        </div>

        <dl class="kv">
          <dt>生效驱动</dt>
          <dd>{{ item.active }}</dd>
          <dt>熔断状态</dt>
          <dd>{{ item.snapshot?.breaker?.state ?? '—' }}（失败 {{ item.snapshot?.breaker?.failures ?? 0 }}）</dd>
          <dt>能力</dt>
          <dd class="small">{{ JSON.stringify(item.snapshot?.capabilities ?? {}) }}</dd>
          <dt v-if="item.snapshot?.features">生效功能</dt>
          <dd v-if="item.snapshot?.features" class="small">
            流式 {{ item.snapshot.features.streaming ? '是' : '否' }} · 工具 {{ item.snapshot.features.tools_enabled ? '是' : '否' }}
            <div v-for="(note, index) in item.snapshot.features.notes ?? []" :key="index" class="small muted">· {{ note }}</div>
          </dd>
        </dl>

        <div class="row" style="margin-top: 12px">
          <select v-model="selections[item.kind]" class="select" style="flex: 1">
            <option v-for="driver in item.drivers" :key="driver" :value="driver">{{ driver }}</option>
          </select>
          <button class="btn" :disabled="busy === `switch-${item.kind}`" @click="switchDriver(item.kind)">切换</button>
        </div>

        <div class="stack" style="margin-top: 12px">
          <div v-for="driver in item.drivers" :key="driver" class="card" style="box-shadow: none; padding: 10px 12px">
            <div class="row" style="justify-content: space-between">
              <span class="mono">{{ driver }}</span>
              <span class="row">
                <button class="btn" :disabled="busy.startsWith('test-')" @click="testDriver(item.kind, driver)">测试</button>
                <button class="btn" @click="toggleEdit(item.kind, driver)">
                  {{ editing[`${item.kind}:${driver}`] ? '收起' : '编辑参数' }}
                </button>
              </span>
            </div>
            <p v-if="tests[`${item.kind}:${driver}`]" class="small" style="margin: 8px 0 0">
              {{ JSON.stringify(tests[`${item.kind}:${driver}`]) }}
            </p>
            <div v-if="editing[`${item.kind}:${driver}`]" class="stack" style="margin-top: 10px">
              <textarea v-model="editingText[`${item.kind}:${driver}`]" class="textarea"></textarea>
              <div class="row">
                <button class="btn btn-primary" @click="applyDriverParams(item.kind, driver)">应用</button>
                <span class="small muted">修改 providers.{{ item.kind }}.{{ driver }} 参数并热生效</span>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>

    <div class="card">
      <div class="card-title">
        <span>打断参数（热更新）</span>
        <span class="small muted">无需重启，立即生效</span>
      </div>
      <div class="row">
        <div class="field" style="width: 160px">
          <span>保护窗口 guard_ms</span>
          <input v-model.number="barge.guard_ms" class="input" type="number" />
        </div>
        <div class="field" style="width: 160px">
          <span>阈值提升 dB</span>
          <input v-model.number="barge.threshold_boost_db" class="input" type="number" step="0.5" />
        </div>
        <div class="field" style="width: 140px">
          <span>确认帧数</span>
          <input v-model.number="barge.confirm_frames" class="input" type="number" />
        </div>
        <div class="field" style="width: 180px">
          <span>回声相似度阈值</span>
          <input v-model.number="barge.echo_similarity_threshold" class="input" type="number" step="0.05" />
        </div>
        <label class="field" style="width: 140px">
          <span>语义回声剔除</span>
          <select v-model="barge.semantic_check" class="select">
            <option :value="true">开启</option>
            <option :value="false">关闭</option>
          </select>
        </label>
        <button class="btn btn-primary" style="align-self: flex-end" @click="applyBargeIn">应用</button>
      </div>
      <p class="small muted" style="margin: 10px 0 0">
        会话状态：{{ sessionState.state }} · 打断计数见监控页。
      </p>
    </div>
  </section>
</template>
