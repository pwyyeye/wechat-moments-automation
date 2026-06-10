/**
 * Dashboard — 系统概览页面
 *
 * 显示：
 *  - 微信登录状态卡片
 *  - 风控等级卡片
 *  - 今日发布统计卡片
 *  - 系统运行时长卡片
 *  - 最近发布历史列表
 *  - 实时事件流
 */

import React, { useEffect, useState, useCallback } from "react";
import { Card, CardBody, CardHeader, Chip, Divider, Button } from "@heroui/react";
import {
  getStatus,
  getHistory,
  createEventStream,
  SystemStatus,
  HistoryItem,
  WsEvent,
} from "../api/client";
import { FiRefreshCw, FiCheckCircle, FiAlertTriangle, FiSend } from "react-icons/fi";

const RISK_COLORS: Record<string, "success" | "warning" | "danger" | "default"> = {
  SAFE: "success",
  SUSPICIOUS: "warning",
  WARNING: "warning",
  DANGER: "danger",
  CRITICAL: "danger",
};

export default function Dashboard() {
  const [status, setStatus] = useState<SystemStatus | null>(null);
  const [history, setHistory] = useState<HistoryItem[]>([]);
  const [events, setEvents] = useState<WsEvent[]>([]);
  const [loading, setLoading] = useState(true);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const [s, h] = await Promise.all([getStatus(), getHistory(10)]);
      setStatus(s);
      setHistory(h);
    } catch (e) {
      console.error("刷新失败", e);
    }
    setLoading(false);
  }, []);

  useEffect(() => {
    refresh();
    const closeWs = createEventStream((event) => {
      setEvents((prev) => [event, ...prev].slice(0, 50));
    });

    const timer = setInterval(refresh, 30000); // 每 30 秒刷新
    return () => {
      closeWs();
      clearInterval(timer);
    };
  }, [refresh]);

  if (!status) return <div className="p-8 text-center">加载中...</div>;

  return (
    <div className="p-6 space-y-6 max-w-6xl mx-auto">
      {/* 标题栏 */}
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold">朋友圈自动化</h1>
        <Button
          size="sm"
          variant="flat"
          startContent={<FiRefreshCw className={loading ? "animate-spin" : ""} />}
          onPress={refresh}
          isDisabled={loading}
        >
          刷新
        </Button>
      </div>

      {/* 状态卡片 */}
      <div className="grid grid-cols-4 gap-4">
        <Card>
          <CardBody className="p-4">
            <div className="text-sm text-default-500">微信状态</div>
            <div className="flex items-center gap-2 mt-2">
              {status.wechat.logged_in ? (
                <FiCheckCircle className="text-success text-xl" />
              ) : (
                <FiAlertTriangle className="text-danger text-xl" />
              )}
              <span className="text-lg font-semibold">
                {status.wechat.logged_in ? "已登录" : "未登录"}
              </span>
            </div>
          </CardBody>
        </Card>

        <Card>
          <CardBody className="p-4">
            <div className="text-sm text-default-500">风控等级</div>
            <Chip
              color={RISK_COLORS[status.risk.level] || "default"}
              variant="flat"
              className="mt-2"
            >
              {status.risk.level}
            </Chip>
            {status.risk.cooldown_remaining > 0 && (
              <div className="text-xs text-warning mt-1">
                冷却中: {Math.ceil(status.risk.cooldown_remaining)}s
              </div>
            )}
          </CardBody>
        </Card>

        <Card>
          <CardBody className="p-4">
            <div className="text-sm text-default-500">今日发布</div>
            <div className="text-2xl font-bold mt-1">
              {status.daily.posts_used}
              <span className="text-base text-default-400 font-normal">
                /{status.daily.posts_limit}
              </span>
            </div>
            <div className="w-full bg-default-100 rounded-full h-1.5 mt-2">
              <div
                className="bg-primary h-1.5 rounded-full transition-all"
                style={{
                  width: `${(status.daily.posts_used / status.daily.posts_limit) * 100}%`,
                }}
              />
            </div>
          </CardBody>
        </Card>

        <Card>
          <CardBody className="p-4">
            <div className="text-sm text-default-500">运行时长</div>
            <div className="text-lg font-semibold mt-2">
              {Math.floor(status.uptime_seconds / 3600)}h{" "}
              {Math.floor((status.uptime_seconds % 3600) / 60)}m
            </div>
            <div className="text-xs text-default-400">
              模板数: {status.templates_count}
            </div>
          </CardBody>
        </Card>
      </div>

      {/* 最近历史 + 事件流 */}
      <div className="grid grid-cols-2 gap-6">
        <Card>
          <CardHeader className="px-4 py-3">
            <div className="flex items-center gap-2">
              <FiSend size={16} />
              <span className="font-semibold">最近发布</span>
            </div>
          </CardHeader>
          <Divider />
          <CardBody className="p-0 max-h-80 overflow-auto">
            {history.length === 0 ? (
              <div className="p-4 text-center text-default-400">暂无发布记录</div>
            ) : (
              history.map((item) => (
                <div key={item.task_id} className="px-4 py-2 hover:bg-default-50 border-b last:border-0">
                  <div className="flex items-center gap-2">
                    {item.success ? (
                      <FiCheckCircle className="text-success" size={14} />
                    ) : (
                      <FiAlertTriangle className="text-danger" size={14} />
                    )}
                    <span className="text-sm truncate flex-1">{item.text}</span>
                    <span className="text-xs text-default-400">
                      {item.timestamp.substring(11, 19)}
                    </span>
                  </div>
                  {item.error && (
                    <div className="text-xs text-danger mt-1 ml-6">{item.error}</div>
                  )}
                </div>
              ))
            )}
          </CardBody>
        </Card>

        <Card>
          <CardHeader className="px-4 py-3">
            <span className="font-semibold">实时事件</span>
            <Chip size="sm" variant="flat" className="ml-2">{events.length}</Chip>
          </CardHeader>
          <Divider />
          <CardBody className="p-0 max-h-80 overflow-auto">
            {events.length === 0 ? (
              <div className="p-4 text-center text-default-400">等待事件...</div>
            ) : (
              events.map((event, i) => (
                <div key={i} className="px-4 py-1.5 text-xs font-mono hover:bg-default-50 border-b last:border-0">
                  <span className="text-default-400">
                    {new Date(event.timestamp * 1000).toLocaleTimeString()}
                  </span>{" "}
                  <span className="text-primary">{event.type}</span>{" "}
                  <span className="text-default-500">
                    {JSON.stringify(event.payload).substring(0, 60)}
                  </span>
                </div>
              ))
            )}
          </CardBody>
        </Card>
      </div>
    </div>
  );
}
