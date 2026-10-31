<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { api, getToken, setToken } from './api/client'
import { sessionChannel } from './api/ws'
import { sessionState } from './store'

const route = useRoute()
const pageTitle = computed(() => (route.meta.title as string) ?? '控制台')

const tokenDraft = ref(getToken())
const showToken = ref(false)
const authEnabled = ref(false)
const authLocked = ref(false)
const authMessage = ref('')

const wsClass = computed(() => (sessionState.connected ? 'dot live' : 'dot down'))
const wsText = computed(() => (sessionState.connected ? '已连接' : '连接断开'))
const stateClass = computed(() => {
  if (['ERROR', 'DEGRADED'].includes(sessionState.state)) return 'error'
  if (['LISTENING', 'THINKING', 'SPEAKING', 'EXECUTING', 'WAKING', 'INTERRUPTING'].includes(sessionState.state)) {
    return 'live'
  }
  return 'warn'
})

function saveToken(): void {
  setToken(tokenDraft.value.trim())
  showToken.value = false
  authMessage.value = '口令已保存，写操作将携带该口令'
  window.location.reload()
}

async function refreshAuth(): Promise<void> {
  try {
    const status = await api.authStatus()
    authEnabled.value = Boolean(status.enabled)
    authLocked.value = Boolean(status.locked)
  } catch {
    authMessage.value = '后端未就绪，稍后自动重试'
  }
}

function bootstrap(): void {
  sessionChannel.connect(sessionState.sessionId || 'default')
}

onMounted(() => {
  void refreshAuth()
  bootstrap()
})

onBeforeUnmount(() => {
  sessionChannel.close()
})

watch(
  () => route.path,
  () => {
    authMessage.value = ''
  },
)
</script>

<template>
  <div class="shell">
    <aside class="sidebar">
      <div class="brand">
        <div class="brand-mark">RC</div>
        <div>
          <div class="brand-title">RobotChat</div>
          <div class="brand-sub">全双工语音控制台</div>
        </div>
      </div>
      <nav class="nav">
        <RouterLink class="nav-item" to="/session">实时会话</RouterLink>
        <RouterLink class="nav-item" to="/config">模型配置</RouterLink>
        <RouterLink class="nav-item" to="/tools">任务与工具</RouterLink>
        <RouterLink class="nav-item" to="/memory">记忆管理</RouterLink>
        <RouterLink class="nav-item" to="/metrics">监控</RouterLink>
        <RouterLink class="nav-item" to="/diagnostics">运维诊断</RouterLink>
      </nav>
      <div class="stack" style="margin-top: auto">
        <span class="badge">
          <span :class="wsClass"></span>
          WS {{ wsText }}
        </span>
        <span class="badge" :class="authEnabled ? 'warn' : 'ok'">
          {{ authEnabled ? (authLocked ? '口令已锁定' : '写操作需口令') : '鉴权关闭（开发模式）' }}
        </span>
      </div>
    </aside>

    <main class="main">
      <header class="topbar">
        <div class="row">
          <h1>{{ pageTitle }}</h1>
          <span class="state-badge" :class="stateClass">{{ sessionState.state }}</span>
        </div>
        <div class="topbar-actions">
          <span class="small muted">会话 {{ sessionState.sessionId || '—' }}</span>
          <button class="btn" @click="showToken = !showToken">口令</button>
        </div>
      </header>

      <div v-if="showToken" class="token-pop">
        <div class="field">
          <span>API 口令（保存后立即生效，不会写入配置）</span>
          <input v-model="tokenDraft" class="input" type="password" placeholder="至少 8 位" />
        </div>
        <div class="row">
          <button class="btn btn-primary" @click="saveToken">保存</button>
          <button class="btn" @click="showToken = false">取消</button>
        </div>
        <span class="small muted">未配置口令时后端处于开发模式，所有写操作放行。</span>
      </div>

      <RouterView />

      <div v-if="authMessage" class="toast">{{ authMessage }}</div>
    </main>
  </div>
</template>
