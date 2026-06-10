/**
 * 微信朋友圈 IPC 处理器 — 放入 sparkle-ref/src/main/ 目录
 *
 * 此文件注册所有 wechat:* IPC handlers，转发请求到 Python API Server。
 *
 * 集成方式：在 sparkle-ref/src/main/index.ts 中添加：
 *   import './wechat-ipc-handlers'
 *
 * 前提：Python API Server 在 localhost:18080 运行
 */

import { ipcMain } from 'electron'

const API_BASE = 'http://127.0.0.1:18080'

async function apiCall<T = any>(endpoint: string, options: RequestInit = {}): Promise<T> {
  const url = `${API_BASE}${endpoint}`
  const response = await fetch(url, {
    ...options,
    headers: {
      'Content-Type': 'application/json',
      ...options.headers as Record<string, string>
    }
  })
  if (!response.ok) {
    const text = await response.text()
    throw new Error(`API ${response.status}: ${text}`)
  }
  return response.json()
}

export function registerWechatIpcHandlers(): void {
  // ═══════════════════════════════════════════════════════════
  // 发布
  // ═══════════════════════════════════════════════════════════

  ipcMain.handle('wechat:publish', async (_event, params: { text: string; images: string[] }) => {
    try {
      return await apiCall('/api/publish', {
        method: 'POST',
        body: JSON.stringify(params)
      })
    } catch (e: any) {
      return { invokeError: e.message }
    }
  })

  // ═══════════════════════════════════════════════════════════
  // 状态
  // ═══════════════════════════════════════════════════════════

  ipcMain.handle('wechat:status', async () => {
    try {
      return await apiCall('/api/status')
    } catch (e: any) {
      return { invokeError: e.message }
    }
  })

  // ═══════════════════════════════════════════════════════════
  // 定时任务
  // ═══════════════════════════════════════════════════════════

  ipcMain.handle('wechat:schedule:list', async () => {
    try {
      return await apiCall('/api/schedule')
    } catch (e: any) {
      return { invokeError: e.message }
    }
  })

  ipcMain.handle('wechat:schedule:create', async (_event, params: {
    text: string; images: string[]; cron: string
  }) => {
    try {
      return await apiCall('/api/schedule', {
        method: 'POST',
        body: JSON.stringify(params)
      })
    } catch (e: any) {
      return { invokeError: e.message }
    }
  })

  ipcMain.handle('wechat:schedule:delete', async (_event, id: string) => {
    try {
      await apiCall(`/api/schedule/${id}`, { method: 'DELETE' })
      return { success: true }
    } catch (e: any) {
      return { invokeError: e.message }
    }
  })

  // ═══════════════════════════════════════════════════════════
  // 历史
  // ═══════════════════════════════════════════════════════════

  ipcMain.handle('wechat:history', async (_event, params: { limit?: number }) => {
    try {
      return await apiCall(`/api/history?limit=${params?.limit ?? 50}`)
    } catch (e: any) {
      return { invokeError: e.message }
    }
  })

  // ═══════════════════════════════════════════════════════════
  // 模板扫描
  // ═══════════════════════════════════════════════════════════

  ipcMain.handle('wechat:templates:scan', async () => {
    try {
      return await apiCall('/api/templates/scan', { method: 'POST' })
    } catch (e: any) {
      return { invokeError: e.message }
    }
  })
}
