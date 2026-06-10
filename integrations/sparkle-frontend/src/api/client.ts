/**
 * WeChat Moments API 客户端
 *
 * 与 Python API Server 通信。
 * 默认连接 http://127.0.0.1:18080
 */

const API_BASE = import.meta.env.VITE_WECHAT_API || "http://127.0.0.1:18080";

interface RequestOptions {
  method?: string;
  body?: unknown;
}

async function request<T = any>(endpoint: string, opts: RequestOptions = {}): Promise<T> {
  const url = `${API_BASE}${endpoint}`;
  const res = await fetch(url, {
    method: opts.method || "GET",
    headers: { "Content-Type": "application/json" },
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });

  if (!res.ok) {
    const text = await res.text();
    throw new Error(`API ${res.status}: ${text}`);
  }

  return res.json();
}

// ═══════════════════════════════════════════════════════════════
// 发布
// ═══════════════════════════════════════════════════════════════

export interface PublishRequest {
  text: string;
  images: string[];
  schedule_at?: string;
}

export interface PublishResponse {
  success: boolean;
  task_id?: string;
  elapsed_seconds: number;
  step_times: Record<string, number>;
  error: string;
}

export function publish(data: PublishRequest): Promise<PublishResponse> {
  return request<PublishResponse>("/api/publish", {
    method: "POST",
    body: data,
  });
}

// ═══════════════════════════════════════════════════════════════
// 状态
// ═══════════════════════════════════════════════════════════════

export interface SystemStatus {
  status: string;
  version: string;
  wechat: {
    logged_in: boolean;
    page: string;
    window_visible: boolean;
  };
  risk: {
    level: string;
    consecutive_events: number;
    cooldown_remaining: number;
  };
  daily: {
    posts_used: number;
    posts_limit: number;
  };
  templates_count: number;
  uptime_seconds: number;
}

export function getStatus(): Promise<SystemStatus> {
  return request<SystemStatus>("/api/status");
}

// ═══════════════════════════════════════════════════════════════
// 定时任务
// ═══════════════════════════════════════════════════════════════

export interface ScheduleItem {
  id: string;
  text: string;
  images: string[];
  cron: string;
  enabled: boolean;
  created_at: string;
  next_run?: string;
}

export function createSchedule(data: {
  text: string;
  images: string[];
  cron: string;
}): Promise<ScheduleItem> {
  return request<ScheduleItem>("/api/schedule", {
    method: "POST",
    body: data,
  });
}

export function listSchedules(): Promise<ScheduleItem[]> {
  return request<ScheduleItem[]>("/api/schedule");
}

export function deleteSchedule(id: string): Promise<void> {
  return request<void>(`/api/schedule/${id}`, { method: "DELETE" });
}

// ═══════════════════════════════════════════════════════════════
// 历史
// ═══════════════════════════════════════════════════════════════

export interface HistoryItem {
  task_id: string;
  text: string;
  success: boolean;
  elapsed_seconds: number;
  timestamp: string;
  error: string;
}

export function getHistory(limit: number = 50): Promise<HistoryItem[]> {
  return request<HistoryItem[]>(`/api/history?limit=${limit}`);
}

// ═══════════════════════════════════════════════════════════════
// 模板
// ═══════════════════════════════════════════════════════════════

export function scanTemplates(): Promise<{ success: boolean; count: number }> {
  return request("/api/templates/scan", { method: "POST" });
}

// ═══════════════════════════════════════════════════════════════
// WebSocket 事件流
// ═══════════════════════════════════════════════════════════════

export interface WsEvent {
  type: string;
  source: string;
  payload: Record<string, unknown>;
  timestamp: number;
}

export function createEventStream(onEvent: (event: WsEvent) => void): () => void {
  const wsUrl = API_BASE.replace("http://", "ws://").replace("https://", "wss://") + "/ws/events";
  const ws = new WebSocket(wsUrl);

  ws.onopen = () => console.log("[WS] 已连接");
  ws.onmessage = (msg) => {
    try {
      const event: WsEvent = JSON.parse(msg.data);
      onEvent(event);
    } catch {}
  };
  ws.onclose = () => console.log("[WS] 已断开");
  ws.onerror = (e) => console.error("[WS] 错误", e);

  return () => ws.close();
}
