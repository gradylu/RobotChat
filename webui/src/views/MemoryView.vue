<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { api } from '../api/client'
import { sessionState } from '../store'
import type { MessageInfo, SessionInfo, TurnInfo } from '../types'

// 记忆管理页（T-6.9 / C-74）：会话列表、历史消息、清除单轮 / 整会话
const sessions = ref<SessionInfo[]>([])
const selected = ref(sessionState.sessionId || 'default')
const messages = ref<MessageInfo[]>([])
const turns = ref<TurnInfo[]>([])
const notice = ref('')
const newSession = ref('')

const totalChars = computed(() => messages.value.reduce((sum, item) => sum + item.content.length, 0))

async function loadSessions(): Promise<void> {
  try {
    const data = await api.sessions()
    sessions.value = (data.sessions as SessionInfo[]) ?? []
    if (!selected.value && sessions.value.length) selected.value = sessions.value[0].id
  } catch (error) {
    notice.value = (error as Error).message
  }
}

async function loadDetail(): Promise<void> {
  if (!selected.value) return
  try {
    const [messageData, turnData] = await Promise.all([
      api.messages(selected.value),
      api.turns(selected.value),
    ])
    messages.value = (messageData.messages as MessageInfo[]) ?? []
    turns.value = (turnData.turns as TurnInfo[]) ?? []
  } catch (error) {
    messages.value = []
    turns.value = []
    notice.value = (error as Error).message
  }
}

async function createSession(): Promise<void> {
  const id = newSession.value.trim()
  try {
    const result = await api.createSession(id || undefined)
    newSession.value = ''
    await loadSessions()
    selected.value = String(result.session_id)
    await loadDetail()
    notice.value = `已创建会话 ${result.session_id}`
  } catch (error) {
    notice.value = (error as Error).message
  }
}

async function clearAll(): Promise<void> {
  if (!selected.value) return
  if (!window.confirm(`清除会话 ${selected.value} 的全部记忆？`)) return
  try {
    const result = await api.clearMessages(selected.value)
    notice.value = `已清除 ${result.removed ?? 0} 条消息`
    await loadDetail()
  } catch (error) {
    notice.value = (error as Error).message
  }
}

async function clearTurn(seq: number): Promise<void> {
  if (!selected.value) return
  try {
    await api.clearMessages(selected.value, seq)
    await api.deleteTurn(selected.value, seq)
    notice.value = `已清除轮次 ${seq}`
    await loadDetail()
  } catch (error) {
    notice.value = (error as Error).message
  }
}

async function removeSession(id: string): Promise<void> {
  if (!window.confirm(`删除会话 ${id}（含事件 / 轮次 / 消息）？`)) return
  try {
    await api.deleteSession(id)
    notice.value = `已删除会话 ${id}`
    if (selected.value === id) {
      selected.value = ''
      messages.value = []
      turns.value = []
    }
    await loadSessions()
  } catch (error) {
    notice.value = (error as Error).message
  }
}

async function downloadLogs(): Promise<void> {
  try {
    const blob = await api.downloadLogs(selected.value)
    const url = URL.createObjectURL(blob)
    const anchor = document.createElement('a')
    anchor.href = url
    anchor.download = `robotchat-${selected.value || 'logs'}.jsonl`
    anchor.click()
    URL.revokeObjectURL(url)
  } catch (error) {
    notice.value = (error as Error).message
  }
}

onMounted(async () => {
  await loadSessions()
  await loadDetail()
})
</script>

<template>
  <section class="stack">
    <div class="card">
      <div class="card-title">
        <span>会话列表</span>
        <span class="row">
          <input v-model="newSession" class="input" placeholder="新建会话 ID（可选）" style="width: 200px" />
          <button class="btn" @click="createSession">新建</button>
          <button class="btn" @click="loadSessions">刷新</button>
        </span>
      </div>
      <div class="scroll">
        <table>
          <thead>
            <tr><th>会话</th><th>状态</th><th>轮次</th><th>创建时间</th><th></th></tr>
          </thead>
          <tbody>
            <tr v-for="item in sessions" :key="item.id">
              <td class="mono">{{ item.id }}</td>
              <td>{{ item.state ?? '—' }}</td>
              <td>{{ item.turn_count ?? 0 }}</td>
              <td class="small muted">{{ item.created_at ? new Date(item.created_at * 1000).toLocaleString() : '—' }}</td>
              <td class="row">
                <button class="btn" @click="selected = item.id; loadDetail()">查看</button>
                <button class="btn btn-danger" @click="removeSession(item.id)">删除</button>
              </td>
            </tr>
            <tr v-if="!sessions.length">
              <td colspan="5" class="small muted">暂无会话</td>
            </tr>
          </tbody>
        </table>
      </div>
      <p v-if="notice" class="small" style="margin: 10px 0 0">{{ notice }}</p>
    </div>

    <div class="card">
      <div class="card-title">
        <span>历史消息（{{ selected || '未选择' }}）</span>
        <span class="row">
          <span class="small muted">{{ messages.length }} 条 · {{ totalChars }} 字</span>
          <button class="btn" @click="downloadLogs">导出日志</button>
          <button class="btn btn-danger" :disabled="!selected" @click="clearAll">清除整会话</button>
        </span>
      </div>
      <div class="scroll">
        <table>
          <thead>
            <tr><th>轮次</th><th>角色</th><th>内容</th><th>时间</th></tr>
          </thead>
          <tbody>
            <tr v-for="(item, index) in messages" :key="index">
              <td>{{ item.turn_id ?? '—' }}</td>
              <td><span class="badge" :class="item.role === 'assistant' ? 'ok' : ''">{{ item.role }}</span></td>
              <td class="small" style="white-space: pre-wrap">{{ item.content }}</td>
              <td class="small muted">{{ item.ts ? new Date(item.ts * 1000).toLocaleTimeString() : '—' }}</td>
            </tr>
            <tr v-if="!messages.length">
              <td colspan="4" class="small muted">该会话暂无消息</td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <div class="card">
      <div class="card-title"><span>轮次</span><span class="small muted">可清除单轮记忆</span></div>
      <div class="scroll">
        <table>
          <thead>
            <tr><th>序号</th><th>状态</th><th>用户</th><th>助手</th><th>打断</th><th></th></tr>
          </thead>
          <tbody>
            <tr v-for="turn in turns" :key="String(turn.seq)" class="small">
              <td>{{ turn.seq ?? '—' }}</td>
              <td>{{ turn.status ?? '—' }}</td>
              <td>{{ turn.user_text ?? '' }}</td>
              <td>{{ turn.assistant_text ?? '' }}</td>
              <td>{{ turn.barge_in ? '是' : '否' }}</td>
              <td>
                <button class="btn btn-danger" :disabled="turn.seq === undefined" @click="clearTurn(turn.seq as number)">清除</button>
              </td>
            </tr>
            <tr v-if="!turns.length">
              <td colspan="6" class="small muted">暂无轮次</td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
  </section>
</template>
