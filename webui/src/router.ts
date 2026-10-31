import { createRouter, createWebHashHistory } from 'vue-router'

// 使用 hash 路由：静态托管无需 SPA 回退规则（T-6.5）
const router = createRouter({
  history: createWebHashHistory(),
  routes: [
    { path: '/', redirect: '/session' },
    { path: '/session', component: () => import('./views/SessionView.vue'), meta: { title: '实时会话' } },
    { path: '/config', component: () => import('./views/ConfigView.vue'), meta: { title: '模型配置' } },
    { path: '/tools', component: () => import('./views/ToolsView.vue'), meta: { title: '任务与工具' } },
    { path: '/memory', component: () => import('./views/MemoryView.vue'), meta: { title: '记忆管理' } },
    { path: '/metrics', component: () => import('./views/MetricsView.vue'), meta: { title: '监控' } },
    { path: '/diagnostics', component: () => import('./views/DiagnosticsView.vue'), meta: { title: '运维诊断' } },
  ],
})

export default router
