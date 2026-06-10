/**
 * 朋友圈管理页面 — 遵循 sparkle-ref 页面模式
 *
 * 参考: src/renderer/src/pages/proxies.tsx
 *
 * Tab 切换:
 *  - 仪表盘: 状态概览 + 实时事件
 *  - 编辑: 文字输入 + 图片选择 + 发布
 *  - 历史: 发布记录列表
 *  - 定时: Cron 定时任务管理
 */

import { Button, Card, CardBody, Chip, Divider, Textarea, Input, Tab, Tabs, Tooltip, Badge } from '@heroui/react'
import { Avatar } from '@heroui-v3/react'
import BasePage from '@renderer/components/base/base-page'
import { useAppConfig } from '@renderer/hooks/use-app-config'
import { platform } from '@renderer/utils/init'
import {
  wechatPublish, wechatStatus, wechatHistory, wechatScheduleList,
  wechatScheduleCreate, wechatScheduleDelete, wechatScanTemplates
} from '@renderer/utils/wechat-ipc'
import { memo, useEffect, useState, useCallback } from 'react'
import { FiSend, FiClock, FiList, FiBarChart2, FiRefreshCw, FiPlus, FiTrash2, FiAlertTriangle, FiCheckCircle } from 'react-icons/fi'
import useSWR, { mutate } from 'swr'

// ═══════════════════════════════════════════════════════════════
// 状态数据 Fetcher
// ═══════════════════════════════════════════════════════════════

const STATUS_FETCHER = () => wechatStatus().catch(() => null)
const HISTORY_FETCHER = () => wechatHistory(30)
const SCHEDULE_FETCHER = () => wechatScheduleList()

// ═══════════════════════════════════════════════════════════════
// 仪表盘 Tab
// ═══════════════════════════════════════════════════════════════

const DashboardTab: React.FC = memo(function DashboardTab() {
  const { data: status, mutate: refreshStatus } = useSWR('wechat:dashboard:status', STATUS_FETCHER, {
    refreshInterval: 15000
  })
  const { data: history } = useSWR('wechat:dashboard:history', HISTORY_FETCHER)

  const riskColor = (level: string) => {
    switch (level) {
      case 'SAFE': return 'success' as const
      case 'SUSPICIOUS': return 'warning' as const
      case 'WARNING': return 'warning' as const
      case 'DANGER': case 'CRITICAL': return 'danger' as const
      default: return 'default' as const
    }
  }

  if (!status) {
    return <div className="flex items-center justify-center h-64 text-default-400">加载中...</div>
  }

  return (
    <div className="p-4 space-y-4">
      {/* 状态卡片 */}
      <div className="grid grid-cols-4 gap-3">
        <Card shadow="sm" radius="sm">
          <CardBody className="p-3">
            <div className="text-xs text-default-500">微信状态</div>
            <div className="flex items-center gap-1.5 mt-1.5">
              {status.wechat?.logged_in ? (
                <FiCheckCircle className="text-success" size={16} />
              ) : (
                <FiAlertTriangle className="text-danger" size={16} />
              )}
              <span className="font-semibold text-sm">
                {status.wechat?.logged_in ? '已登录' : '未登录'}
              </span>
            </div>
          </CardBody>
        </Card>

        <Card shadow="sm" radius="sm">
          <CardBody className="p-3">
            <div className="text-xs text-default-500">风控等级</div>
            <Chip color={riskColor(status.risk?.level)} variant="flat" size="sm" className="mt-1.5">
              {status.risk?.level || '未知'}
            </Chip>
          </CardBody>
        </Card>

        <Card shadow="sm" radius="sm">
          <CardBody className="p-3">
            <div className="text-xs text-default-500">今日发布</div>
            <div className="text-xl font-bold mt-0.5">
              {status.daily?.posts_used ?? 0}
              <span className="text-sm text-default-400 font-normal">/{status.daily?.posts_limit ?? 10}</span>
            </div>
          </CardBody>
        </Card>

        <Card shadow="sm" radius="sm">
          <CardBody className="p-3">
            <div className="text-xs text-default-500">运行时长</div>
            <div className="text-sm font-semibold mt-1.5">
              {Math.floor((status.uptime_seconds ?? 0) / 3600)}h{' '}
              {Math.floor(((status.uptime_seconds ?? 0) % 3600) / 60)}m
            </div>
          </CardBody>
        </Card>
      </div>

      {/* 最近发布 */}
      <Card shadow="sm" radius="sm">
        <CardBody className="p-0 max-h-64 overflow-auto">
          {(!history || history.length === 0) ? (
            <div className="p-4 text-center text-default-400 text-sm">暂无发布记录</div>
          ) : (
            history.map((item) => (
              <div key={item.task_id} className="flex items-center gap-2 px-3 py-2 hover:bg-default-100 border-b last:border-0 text-sm">
                {item.success ? (
                  <FiCheckCircle className="text-success shrink-0" size={14} />
                ) : (
                  <FiAlertTriangle className="text-danger shrink-0" size={14} />
                )}
                <span className="truncate flex-1">{item.text}</span>
                <span className="text-xs text-default-400 shrink-0">{item.timestamp?.substring(11, 19)}</span>
              </div>
            ))
          )}
        </CardBody>
      </Card>
    </div>
  )
})

// ═══════════════════════════════════════════════════════════════
// 编辑发布 Tab
// ═══════════════════════════════════════════════════════════════

const CRON_PRESETS = [
  { label: '每天 9:00', value: '0 9 * * *' },
  { label: '每天 12:00', value: '0 12 * * *' },
  { label: '每天 18:00', value: '0 18 * * *' },
  { label: '每周一 9:00', value: '0 9 * * 1' },
  { label: '每小时', value: '0 * * * *' }
]

const ComposerTab: React.FC = memo(function ComposerTab() {
  const [text, setText] = useState('')
  const [images, setImages] = useState<string[]>([])
  const [publishing, setPublishing] = useState(false)

  const handlePublish = useCallback(async () => {
    if (!text.trim() && images.length === 0) return
    setPublishing(true)
    try {
      const result = await wechatPublish({ text: text.trim(), images })
      if (result.success) {
        setText('')
        setImages([])
        await Promise.all([
          mutate('wechat:dashboard:history'),
          mutate('wechat:dashboard:status')
        ])
      }
    } catch {}
    setPublishing(false)
  }, [text, images])

  const handleSchedule = async () => {
    if (!text.trim()) return
    try {
      await wechatScheduleCreate({ text: text.trim(), images, cron: '0 9 * * *' })
      setText('')
      setImages([])
      await mutate('wechat:schedule:list')
    } catch {}
  }

  return (
    <div className="p-4 space-y-4">
      <Textarea
        label="朋友圈文字"
        placeholder="这一刻的想法..."
        value={text}
        onValueChange={setText}
        minRows={3}
        maxRows={6}
        maxLength={2000}
        description={`${text.length}/2000`}
      />

      {images.length > 0 && (
        <div className="flex gap-2 flex-wrap">
          {images.map((path, i) => (
            <Chip
              key={i}
              onClose={() => setImages(prev => prev.filter((_, j) => j !== i))}
              variant="flat"
              size="sm"
            >
              {path.split('\\').pop()}
            </Chip>
          ))}
        </div>
      )}

      <div className="flex gap-2">
        <Button
          color="primary"
          size="sm"
          startContent={<FiSend size={14} />}
          onPress={handlePublish}
          isLoading={publishing}
          isDisabled={!text.trim() && images.length === 0}
        >
          立即发布
        </Button>
        <Button
          variant="flat"
          size="sm"
          startContent={<FiClock size={14} />}
          onPress={handleSchedule}
          isDisabled={!text.trim()}
        >
          定时发布
        </Button>
      </div>
    </div>
  )
})

// ═══════════════════════════════════════════════════════════════
// 发布历史 Tab
// ═══════════════════════════════════════════════════════════════

const HistoryTab: React.FC = memo(function HistoryTab() {
  const { data: history, mutate: refreshHistory } = useSWR('wechat:history:tab', HISTORY_FETCHER)

  return (
    <div className="p-4">
      <div className="flex items-center justify-between mb-3">
        <span className="text-sm text-default-500">
          {history ? `${history.length} 条记录` : '加载中...'}
        </span>
        <Button
          size="sm"
          variant="light"
          isIconOnly
          onPress={() => refreshHistory()}
        >
          <FiRefreshCw size={14} />
        </Button>
      </div>

      {(!history || history.length === 0) ? (
        <div className="text-center text-default-400 py-8">暂无发布记录</div>
      ) : (
        <div className="space-y-1">
          {history.map((item) => (
            <div key={item.task_id} className="flex items-center gap-2 px-3 py-2 rounded-lg hover:bg-default-100 text-sm">
              {item.success ? (
                <FiCheckCircle className="text-success shrink-0" size={14} />
              ) : (
                <FiAlertTriangle className="text-danger shrink-0" size={14} />
              )}
              <div className="min-w-0 flex-1">
                <div className="truncate">{item.text}</div>
                {item.error && <div className="text-xs text-danger truncate">{item.error}</div>}
              </div>
              <div className="flex items-center gap-3 text-xs text-default-400 shrink-0">
                <span>{item.elapsed_seconds.toFixed(1)}s</span>
                <span>{item.timestamp?.substring(5, 16)?.replace('T', ' ')}</span>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  )
})

// ═══════════════════════════════════════════════════════════════
// 定时任务 Tab
// ═══════════════════════════════════════════════════════════════

const ScheduleTab: React.FC = memo(function ScheduleTab() {
  const { data: schedules, mutate: refreshSchedules } = useSWR('wechat:schedule:list', SCHEDULE_FETCHER)
  const [text, setText] = useState('')
  const [cron, setCron] = useState('0 9 * * *')

  const handleCreate = async () => {
    if (!text.trim() || !cron.trim()) return
    try {
      await wechatScheduleCreate({ text: text.trim(), images: [], cron })
      setText('')
      await refreshSchedules()
    } catch {}
  }

  const handleDelete = async (id: string) => {
    try {
      await wechatScheduleDelete(id)
      await refreshSchedules()
    } catch {}
  }

  return (
    <div className="p-4 space-y-4">
      {/* 新建 */}
      <Card shadow="sm" radius="sm">
        <CardBody className="p-3 space-y-3">
          <div className="flex gap-2">
            <Input
              size="sm"
              value={text}
              onValueChange={setText}
              placeholder="朋友圈内容"
              className="flex-1"
            />
            <Button
              size="sm"
              color="primary"
              startContent={<FiPlus size={14} />}
              onPress={handleCreate}
              isDisabled={!text.trim()}
            >
              添加
            </Button>
          </div>
          <div className="flex gap-2 flex-wrap">
            {CRON_PRESETS.map(preset => (
              <Chip
                key={preset.value}
                size="sm"
                variant={cron === preset.value ? 'solid' : 'flat'}
                color={cron === preset.value ? 'primary' : 'default'}
                className="cursor-pointer"
                onClick={() => setCron(preset.value)}
              >
                {preset.label}
              </Chip>
            ))}
          </div>
          <Input
            size="sm"
            value={cron}
            onValueChange={setCron}
            placeholder="0 9 * * *"
            className="max-w-40"
          />
        </CardBody>
      </Card>

      {/* 列表 */}
      {(!schedules || schedules.length === 0) ? (
        <div className="text-center text-default-400 py-4 text-sm">暂无定时任务</div>
      ) : (
        schedules.map(item => (
          <div key={item.id} className="flex items-center justify-between px-3 py-2 rounded-lg hover:bg-default-100 text-sm">
            <div className="min-w-0 flex-1">
              <div className="flex items-center gap-2">
                <span className={`w-1.5 h-1.5 rounded-full ${item.enabled ? 'bg-success' : 'bg-default-300'}`} />
                <code className="text-xs bg-default-100 px-1.5 py-0.5 rounded">{item.cron}</code>
              </div>
              <div className="truncate mt-0.5">{item.text}</div>
            </div>
            <Tooltip content="删除">
              <Button size="sm" isIconOnly variant="light" color="danger" onPress={() => handleDelete(item.id)}>
                <FiTrash2 size={13} />
              </Button>
            </Tooltip>
          </div>
        ))
      )}
    </div>
  )
})

// ═══════════════════════════════════════════════════════════════
// 主页面
// ═══════════════════════════════════════════════════════════════

const WechatMoments: React.FC = () => {
  const { appConfig } = useAppConfig()
  const { disableAnimation = false } = appConfig || {}

  return (
    <BasePage
      title="朋友圈"
      header={
        <Button
          size="sm"
          variant="light"
          isIconOnly
          onPress={() => {
            mutate('wechat:dashboard:status')
            mutate('wechat:dashboard:history')
            mutate('wechat:schedule:list')
          }}
        >
          <FiRefreshCw size={16} />
        </Button>
      }
    >
      <Tabs
        aria-label="朋友圈管理"
        className="px-2 pt-1"
        defaultSelectedKey="dashboard"
        disableAnimation={disableAnimation}
      >
        <Tab
          key="dashboard"
          title={
            <div className="flex items-center gap-1.5">
              <FiBarChart2 size={14} />
              <span>仪表盘</span>
            </div>
          }
        >
          <DashboardTab />
        </Tab>

        <Tab
          key="composer"
          title={
            <div className="flex items-center gap-1.5">
              <FiSend size={14} />
              <span>编辑</span>
            </div>
          }
        >
          <ComposerTab />
        </Tab>

        <Tab
          key="history"
          title={
            <div className="flex items-center gap-1.5">
              <FiList size={14} />
              <span>历史</span>
            </div>
          }
        >
          <HistoryTab />
        </Tab>

        <Tab
          key="schedule"
          title={
            <div className="flex items-center gap-1.5">
              <FiClock size={14} />
              <span>定时</span>
            </div>
          }
        >
          <ScheduleTab />
        </Tab>
      </Tabs>
    </BasePage>
  )
}

export default WechatMoments
