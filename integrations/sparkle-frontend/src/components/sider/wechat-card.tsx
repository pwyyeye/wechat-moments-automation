/**
 * 朋友圈侧边栏卡片 — 遵循 sparkle-ref sider card 模式
 *
 * 参考: src/renderer/src/components/sider/proxy-card.tsx
 *
 * 特性：
 *  - 支持 iconOnly 模式（侧边栏折叠时）
 *  - 支持 @dnd-kit useSortable 拖拽排序
 *  - 点击跳转到 /wechat 页面
 */

import { Button, Card, CardBody, Tooltip, Chip } from '@heroui/react'
import { useSortable } from '@dnd-kit/sortable'
import { CSS } from '@dnd-kit/utilities'
import { useLocation, useNavigate } from 'react-router-dom'
import { useAppConfig } from '@renderer/hooks/use-app-config'
import React from 'react'
import { FiSend } from 'react-icons/fi'
import useSWR from 'swr'
import { wechatStatus, WechatStatus } from '@renderer/utils/wechat-ipc'

interface Props {
  iconOnly?: boolean
}

const WECHAT_STATUS_FETCHER = (): Promise<WechatStatus> => wechatStatus().catch(() => ({
  status: 'offline',
  version: '',
  wechat: { logged_in: false, page: '', window_visible: false },
  risk: { level: 'UNKNOWN', consecutive_events: 0, cooldown_remaining: 0 },
  daily: { posts_used: 0, posts_limit: 10 },
  templates_count: 0,
  uptime_seconds: 0
}))

const WechatCard: React.FC<Props> = (props) => {
  const { appConfig } = useAppConfig()
  const { iconOnly } = props
  const { proxyCardStatus = 'col-span-2', disableAnimation = false } = appConfig || {}
  const location = useLocation()
  const navigate = useNavigate()
  const match = location.pathname.includes('/wechat')
  const { data: status } = useSWR('wechat:status', WECHAT_STATUS_FETCHER, {
    refreshInterval: 10000
  })

  const {
    attributes,
    listeners,
    setNodeRef,
    transform: tf,
    transition,
    isDragging
  } = useSortable({
    id: 'wechat'
  })
  const transform = tf ? { x: tf.x, y: tf.y, scaleX: 1, scaleY: 1 } : null

  if (iconOnly) {
    return (
      <div className={`${proxyCardStatus} flex justify-center`}>
        <Tooltip content="朋友圈" placement="right">
          <Button
            size="sm"
            isIconOnly
            color={match ? 'primary' : 'default'}
            variant={match ? 'solid' : 'light'}
            onPress={() => navigate('/wechat')}
          >
            <FiSend className="text-[20px]" />
          </Button>
        </Tooltip>
      </div>
    )
  }

  return (
    <div
      className={`${proxyCardStatus} wechat-card`}
      ref={setNodeRef}
      style={{
        transform: CSS.Transform.toString(transform),
        transition,
        opacity: isDragging ? 0.5 : 1
      }}
      {...attributes}
      {...listeners}
    >
      <Card
        isPressable
        disableAnimation={disableAnimation}
        shadow="sm"
        radius="sm"
        fullWidth
        className={`${match ? 'outline outline-primary' : ''} h-full`}
        onPress={() => navigate('/wechat')}
      >
        <CardBody className="p-2.5 overflow-hidden">
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-2.5">
              <FiSend className={`text-xl ${match ? 'text-primary' : 'text-default-600'}`} />
              <div className="flex flex-col gap-0.5 text-left">
                <span className="text-md font-bold">朋友圈</span>
              </div>
            </div>
            {status && (
              <Chip
                size="sm"
                color={status.wechat?.logged_in ? 'success' : 'default'}
                variant="flat"
              >
                {status.wechat?.logged_in ? '在线' : '离线'}
              </Chip>
            )}
          </div>
        </CardBody>
      </Card>
    </div>
  )
}

export default WechatCard
