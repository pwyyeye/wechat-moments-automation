/**
 * 微信朋友圈 IPC 工具 — 遵循 sparkle-ref utils/ipc.ts 模式
 *
 * 所有函数通过 window.electron.ipcRenderer.invoke() 与主进程通信。
 * 主进程需要注册对应的 ipcMain.handle() 处理器，转发请求到 Python API Server。
 */

// eslint-disable-next-line @typescript-eslint/no-explicit-any
function ipcErrorWrapper(response: any): any {
  if (typeof response === 'object' && 'invokeError' in response) {
    throw response.invokeError
  }
  return response
}

// ═══════════════════════════════════════════════════════════════
// 发布
// ═══════════════════════════════════════════════════════════════

export interface PublishParams {
  text: string
  images: string[]
}

export interface PublishResult {
  success: boolean
  task_id?: string
  elapsed_seconds: number
  step_times: Record<string, number>
  error: string
}

export async function wechatPublish(params: PublishParams): Promise<PublishResult> {
  return ipcErrorWrapper(await window.electron.ipcRenderer.invoke('wechat:publish', params))
}

// ═══════════════════════════════════════════════════════════════
// 状态
// ═══════════════════════════════════════════════════════════════

export interface WechatStatus {
  status: string
  version: string
  wechat: {
    logged_in: boolean
    page: string
    window_visible: boolean
  }
  risk: {
    level: string
    consecutive_events: number
    cooldown_remaining: number
  }
  daily: {
    posts_used: number
    posts_limit: number
  }
  templates_count: number
  uptime_seconds: number
}

export async function wechatStatus(): Promise<WechatStatus> {
  return ipcErrorWrapper(await window.electron.ipcRenderer.invoke('wechat:status'))
}

// ═══════════════════════════════════════════════════════════════
// 定时任务
// ═══════════════════════════════════════════════════════════════

export interface ScheduleItem {
  id: string
  text: string
  images: string[]
  cron: string
  enabled: boolean
  created_at: string
  next_run?: string
}

export async function wechatScheduleList(): Promise<ScheduleItem[]> {
  return ipcErrorWrapper(await window.electron.ipcRenderer.invoke('wechat:schedule:list'))
}

export async function wechatScheduleCreate(params: {
  text: string
  images: string[]
  cron: string
}): Promise<ScheduleItem> {
  return ipcErrorWrapper(await window.electron.ipcRenderer.invoke('wechat:schedule:create', params))
}

export async function wechatScheduleDelete(id: string): Promise<void> {
  return ipcErrorWrapper(await window.electron.ipcRenderer.invoke('wechat:schedule:delete', id))
}

// ═══════════════════════════════════════════════════════════════
// 历史
// ═══════════════════════════════════════════════════════════════

export interface HistoryItem {
  task_id: string
  text: string
  success: boolean
  elapsed_seconds: number
  timestamp: string
  error: string
}

export async function wechatHistory(limit?: number): Promise<HistoryItem[]> {
  return ipcErrorWrapper(
    await window.electron.ipcRenderer.invoke('wechat:history', { limit: limit ?? 50 })
  )
}

// ═══════════════════════════════════════════════════════════════
// 模板
// ═══════════════════════════════════════════════════════════════

export async function wechatScanTemplates(): Promise<{ success: boolean; count: number }> {
  return ipcErrorWrapper(await window.electron.ipcRenderer.invoke('wechat:templates:scan'))
}
