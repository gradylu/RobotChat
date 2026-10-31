<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { api } from '../api/client'
import { sessionChannel } from '../api/ws'
import { sessionState } from '../store'

// 运行控制（T-6.7）：待机态开始会话 / 文本输入均视作显式唤醒（FR-11.2）
const text = ref('')
const notice = ref('')
const supported = ref<string[]>([])
const currentLang = ref('')
const mode = ref<'auto' | 'fixed'>('auto')

const running = computed(() => sessionState.running)
const muted = computed(() => sessionState.micMuted)

async function loadLanguages(): Promise<void> {
  try {
    const snapshot = await api.languages()
    supported.value = snapshot.supported ?? []
    currentLang.value = snapshot.current ?? ''
    mode.value = snapshot.mode === 'fixed' ? 'fixed' : 'auto'
  } catch {
    // 后端未启动时静默，页面其余部分仍可用
  }
}

function send(type: string, payload: Record<string, unknown> = {}): boolean {
  const ok = sessionChannel.send(type, payload)
  if (!ok) notice.value = 'WebSocket 未连接，请稍候或刷新页面'
  return ok
}

function startSession(): void {
  notice.value = ''
  // 待机态（含 IDLE）点击开始 = 显式唤醒，服务端会开启活跃窗口
  send('session.start')
}

function stopSession(): void {
  notice.value = ''
  send('session.stop')
}

function triggerWake(): void {
  notice.value = ''
  send('wake.trigger')
}

function sleepNow(): void {
  notice.value = ''
  send('session.sleep')
}

function interrupt(): void {
  notice.value = ''
  send('interrupt', { reason: 'manual' })
}

function toggleMute(): void {
  notice.value = ''
  send(muted.value ? 'mic.unmute' : 'mic.mute')
}

function submitText(): void {
  const value = text.value.trim()
  if (!value) return
  notice.value = ''
  if (!send('text.input', { text: value, lang: currentLang.value || undefined })) return
  text.value = ''
}

async function applyLanguage(): Promise<void> {
  notice.value = ''
  try {
    await api.setLanguage({ mode: mode.value, lang: mode.value === 'fixed' ? currentLang.value : undefined })
    notice.value = `语言已切换：${mode.value === 'fixed' ? currentLang.value : '自动'}`
  } catch (error) {
    notice.value = (error as Error).message
  }
}

async function falsePositive(): Promise<void> {
  notice.value = ''
  try {
    const result = await api.markFalseWake()
    notice.value = `已记录误唤醒，累计 ${result.wake_false_positive ?? '?'} 次`
  } catch (error) {
    notice.value = (error as Error).message
  }
}

onMounted(() => {
  void loadLanguages()
})
</script>

<template>
  <section class="card">
    <div class="card-title">
      <span>运行控制</span>
      <span class="row">
        <button class="btn btn-primary" :disabled="!running" @click="startSession">开始会话</button>
        <button class="btn" @click="stopSession">结束会话</button>
        <button class="btn btn-danger" @click="interrupt">手动打断</button>
        <button class="btn" @click="toggleMute">{{ muted ? '取消静音' : '麦克风静音' }}</button>
      </span>
    </div>

    <div class="row" style="margin-bottom: 12px">
      <button class="btn" @click="triggerWake">手动唤醒</button>
      <button class="btn" @click="sleepNow">立即休眠</button>
      <button class="btn" @click="falsePositive">标记误唤醒</button>
      <span class="small muted">待机态点击「开始会话」或直接输入文本即视为显式唤醒。</span>
    </div>

    <div class="row">
      <div class="field" style="min-width: 150px">
        <span>语言模式</span>
        <select v-model="mode" class="select">
          <option value="auto">自动</option>
          <option value="fixed">固定</option>
        </select>
      </div>
      <div class="field" style="min-width: 150px">
        <span>生效语言</span>
        <select v-model="currentLang" class="select" :disabled="mode !== 'fixed'">
          <option v-for="lang in supported" :key="lang" :value="lang">{{ lang }}</option>
        </select>
      </div>
      <button class="btn" style="align-self: flex-end" @click="applyLanguage">应用语言</button>
    </div>

    <div class="row" style="margin-top: 12px">
      <input
        v-model="text"
        class="input"
        style="flex: 1; min-width: 240px"
        placeholder="文本输入（免唤醒直接对话）"
        @keyup.enter="submitText"
      />
      <button class="btn btn-primary" @click="submitText">发送</button>
    </div>

    <p v-if="notice" class="small" style="margin: 10px 0 0">{{ notice }}</p>
  </section>
</template>
