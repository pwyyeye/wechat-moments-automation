/**
 * SchedulePage — 定时发布管理页面
 */

import React, { useState, useEffect, useCallback } from "react";
import {
  Card, CardBody, CardHeader, Button, Input, Textarea,
  Chip, Divider, Switch, addToast, Badge,
} from "@heroui/react";
import {
  listSchedules, createSchedule, deleteSchedule, ScheduleItem,
} from "../api/client";
import { FiPlus, FiTrash2, FiClock } from "react-icons/fi";

const CRON_PRESETS = [
  { label: "每天早上 8:00", value: "0 8 * * *" },
  { label: "每天早上 9:00", value: "0 9 * * *" },
  { label: "每天中午 12:00", value: "0 12 * * *" },
  { label: "每天下午 6:00", value: "0 18 * * *" },
  { label: "每周一早上 9:00", value: "0 9 * * 1" },
  { label: "每小时", value: "0 * * * *" },
];

export default function SchedulePage() {
  const [schedules, setSchedules] = useState<ScheduleItem[]>([]);
  const [text, setText] = useState("");
  const [cron, setCron] = useState("0 9 * * *");
  const [loading, setLoading] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const list = await listSchedules();
      setSchedules(list);
    } catch {}
  }, []);

  useEffect(() => { refresh(); }, [refresh]);

  const handleCreate = async () => {
    if (!text.trim() || !cron.trim()) return;
    setLoading(true);
    try {
      await createSchedule({ text: text.trim(), images: [], cron });
      addToast({ title: "定时任务已创建", color: "success" });
      setText("");
      refresh();
    } catch (e: any) {
      addToast({ title: "创建失败", description: e.message, color: "danger" });
    }
    setLoading(false);
  };

  const handleDelete = async (id: string) => {
    try {
      await deleteSchedule(id);
      addToast({ title: "已取消", color: "success" });
      refresh();
    } catch (e: any) {
      addToast({ title: "取消失败", description: e.message, color: "danger" });
    }
  };

  return (
    <div className="p-6 max-w-4xl mx-auto space-y-6">
      <h1 className="text-2xl font-bold">定时发布</h1>

      {/* 新建定时任务 */}
      <Card>
        <CardHeader className="px-4 py-3">
          <div className="flex items-center gap-2">
            <FiPlus size={16} />
            <span className="font-semibold">新建定时任务</span>
          </div>
        </CardHeader>
        <Divider />
        <CardBody className="p-4 space-y-4">
          <Textarea
            label="朋友圈内容"
            value={text}
            onValueChange={setText}
            minRows={2}
            maxLength={2000}
          />

          <div>
            <label className="text-sm text-default-500 mb-2 block">
              Cron 表达式
            </label>
            <div className="flex gap-2 flex-wrap mb-2">
              {CRON_PRESETS.map((preset) => (
                <Chip
                  key={preset.value}
                  variant={cron === preset.value ? "solid" : "flat"}
                  color={cron === preset.value ? "primary" : "default"}
                  className="cursor-pointer"
                  onClick={() => setCron(preset.value)}
                >
                  {preset.label}
                </Chip>
              ))}
            </div>
            <Input
              value={cron}
              onValueChange={setCron}
              placeholder="0 9 * * *"
              size="sm"
            />
          </div>

          <Button
            color="primary"
            startContent={<FiClock />}
            onPress={handleCreate}
            isLoading={loading}
            isDisabled={!text.trim() || !cron.trim()}
          >
            创建定时任务
          </Button>
        </CardBody>
      </Card>

      {/* 定时任务列表 */}
      <Card>
        <CardHeader className="px-4 py-3">
          <span className="font-semibold">任务列表</span>
          <Badge content={schedules.length} color="primary" className="ml-2" />
        </CardHeader>
        <Divider />
        <CardBody className="p-0">
          {schedules.length === 0 ? (
            <div className="p-8 text-center text-default-400">
              暂无定时任务
            </div>
          ) : (
            schedules.map((item) => (
              <div
                key={item.id}
                className="px-4 py-3 hover:bg-default-50 border-b last:border-0"
              >
                <div className="flex items-start justify-between gap-4">
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2">
                      <span
                        className={`w-2 h-2 rounded-full ${
                          item.enabled ? "bg-success" : "bg-default-300"
                        }`}
                      />
                      <code className="text-sm bg-default-100 px-2 py-0.5 rounded">
                        {item.cron}
                      </code>
                    </div>
                    <div className="text-sm mt-1 truncate">{item.text}</div>
                    {item.next_run && (
                      <div className="text-xs text-default-400 mt-1">
                        下次执行: {new Date(item.next_run).toLocaleString()}
                      </div>
                    )}
                  </div>
                  <Button
                    isIconOnly
                    size="sm"
                    variant="light"
                    color="danger"
                    onPress={() => handleDelete(item.id)}
                  >
                    <FiTrash2 size={14} />
                  </Button>
                </div>
              </div>
            ))
          )}
        </CardBody>
      </Card>
    </div>
  );
}
