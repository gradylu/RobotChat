<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { api, ApiError } from '../api/client'

// 任务与工具页（T-6.9）：工具清单 / 参数 / 手动触发 / 最近调用历史
interface ToolDefinition {
  name: string
  description?: string
  risk?: string
  timeout_s?: number
  parameters?: Record<string, unknown>
}

const data = ref<Record<string, any>>({})
const definitions = computed<ToolDefinition[]>(() => (data.value.definitions as ToolDefinition[]) ?? [])
const recent = computed<Record<string, any>[]>(() => (data.value.recent as Record<string, any>[]) ?? [])
const selected = ref('')
const argsText = ref('{}')
const result = ref('')
const notice = ref('')

const current = computed(() => definitions.value.find((item) => item.name === selected.value))

function riskClass(risk?: string): string {
  if (risk === 'high' || risk === 'critical') return 'error'
  if (risk === 'medium') return 'warn'
  return 'ok'
}

async function load(): Promise<void> {
  try {
    data.value = await api.tools()
    if (!selected.value && definitions.value.length) {
      selected.value = definitions.value[0].name
    }
  } catch (error) {
    notice.value = (error as Error).message
  }
}

async function invoke(): Promise<void> {
  if (!selected.value) return
  let args: Record<string, unknown>
  try {
    args = JSON.parse(argsText.value || '{}')
  } catch {
    notice.value = '参数不是合法 JSON'
    return
  }
  notice.value = ''
  result.value = '执行中…'
  try {
    result.value = JSON.stringify(await api.invokeTool(selected.value, args), null, 2)
    await load()
  } catch (error) {
    const payload = error instanceof ApiError ? error.payload : {}
    result.value = JSON.stringify({ error: (error as Error).message, ...payload }, null, 2)
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
        <span>工具清单</span>
        <span class="row small muted">
          <span>调用 {{ data.calls ?? 0 }} · 失败 {{ data.failures ?? 0 }} · 超时 {{ data.timeouts ?? 0 }} · 拒绝 {{ data.denied ?? 0 }}</span>
          <button class="btn" @click="load">刷新</button>
        </span>
      </div>
      <div class="scroll">
        <table>
          <thead>
            <tr><th>名称</th><th>风险</th><th>适配器</th><th>超时</th><th>说明</th></tr>
          </thead>
          <tbody>
            <tr v-for="tool in definitions" :key="tool.name">
              <td class="mono">{{ tool.name }}</td>
              <td><span class="badge" :class="riskClass(tool.risk)">{{ tool.risk ?? '—' }}</span></td>
              <td>{{ (tool as any).adapter ?? '—' }}</td>
              <td>{{ tool.timeout_s ?? '—' }}s</td>
              <td class="small muted">{{ tool.description }}</td>
            </tr>
            <tr v-if="!definitions.length">
              <td colspan="5" class="small muted">工具注册表未初始化或未配置工具</td>
            </tr>
          </tbody>
        </table>
      </div>
      <p v-if="notice" class="small" style="margin: 10px 0 0">{{ notice }}</p>
    </div>

    <div class="grid cols-2">
      <div class="card">
        <div class="card-title"><span>手动触发（调试）</span><span class="small muted">绕过 LLM，仍受安全策略约束</span></div>
        <div class="stack">
          <div class="field">
            <span>工具</span>
            <select v-model="selected" class="select">
              <option v-for="tool in definitions" :key="tool.name" :value="tool.name">{{ tool.name }}</option>
            </select>
          </div>
          <div class="field">
            <span>参数（JSON）</span>
            <textarea v-model="argsText" class="textarea" style="min-height: 140px"></textarea>
          </div>
          <div class="row">
            <button class="btn btn-primary" :disabled="!selected" @click="invoke">执行</button>
            <span class="small muted" v-if="current">{{ current.description }}</span>
          </div>
          <pre v-if="result" class="mono card" style="white-space: pre-wrap; box-shadow: none">{{ result }}</pre>
        </div>
      </div>

      <div class="card">
        <div class="card-title"><span>最近调用</span><span class="small muted">最多 10 条</span></div>
        <div class="scroll">
          <table>
            <thead>
              <tr><th>时间</th><th>工具</th><th>状态</th><th>耗时</th></tr>
            </thead>
            <tbody>
              <tr v-for="(call, index) in recent" :key="index">
                <td class="small mono">{{ call.ts ? new Date(call.ts * 1000).toLocaleTimeString() : '—' }}</td>
                <td class="mono">{{ call.name ?? '—' }}</td>
                <td>
                  <span class="badge" :class="call.result?.ok ? 'ok' : 'error'">
                    {{ call.result?.ok ? 'ok' : call.result?.error_kind ?? 'failed' }}
                  </span>
                </td>
                <td>{{ call.result?.duration_ms ?? '—' }} ms</td>
              </tr>
              <tr v-if="!recent.length">
                <td colspan="4" class="small muted">暂无调用记录</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>
  </section>
</template>
