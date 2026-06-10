# Sparkle 集成指南

## 文件映射

将本目录下的文件复制到 sparkle-ref 项目的对应位置：

```
integrations/sparkle-frontend/
│
├── src/utils/wechat-ipc.ts
│   → sparkle-ref/src/renderer/src/utils/wechat-ipc.ts
│   (新建文件 — IPC 调用封装)
│
├── src/components/sider/wechat-card.tsx
│   → sparkle-ref/src/renderer/src/components/sider/wechat-card.tsx
│   (新建文件 — 侧边栏卡片)
│
├── src/pages/wechat-moments.tsx
│   → sparkle-ref/src/renderer/src/pages/wechat-moments.tsx
│   (新建文件 — 朋友圈管理页面)
│
└── src/main-ipc.ts
    → sparkle-ref/src/main/wechat-ipc-handlers.ts
    (新建文件 — 主进程 IPC 处理器)
```

## sparkle-ref 需要修改的 3 个文件

### 1. `sparkle-ref/src/renderer/src/routes/index.tsx` — 添加路由

在 imports 中添加：
```tsx
import WechatMoments from '@renderer/pages/wechat-moments'
```

在 routes 数组中添加：
```tsx
{
  path: '/wechat',
  element: <WechatMoments />
},
```

### 2. `sparkle-ref/src/renderer/src/App.tsx` — 注册侧边栏卡片

在 imports 中添加：
```tsx
import WechatCard from '@renderer/components/sider/wechat-card'
```

在 `siderCardRouteMap` 中添加：
```tsx
'wechat-card': '/wechat',
```

在 `componentMap` 中添加：
```tsx
wechat: WechatCard,
```

在 `defaultSiderOrder` 数组末尾添加：
```tsx
'wechat',
```

在 `siderCardSelector` 的处理中，确认 `wechat-card` 类名在列表中：
```tsx
// siderCardRouteMap 的 Object.keys 会自动包含新添加的 'wechat-card'
```

### 3. `sparkle-ref/src/main/index.ts` — 注册 IPC 处理器

在文件顶部 import 区域添加：
```ts
import { registerWechatIpcHandlers } from './wechat-ipc-handlers'
```

在 `app.whenReady()` 后添加：
```ts
registerWechatIpcHandlers()
```

## 运行前提

Python API Server 必须在 localhost:18080 运行：

```bash
cd wechat-moments-automation
pip install fastapi uvicorn croniter
python -m uvicorn src.api.server:app --host 127.0.0.1 --port 18080
```

## sparkle-ref 技术栈兼容性确认

wechat-card.tsx 和 wechat-moments.tsx 使用的组件全部来自 sparkle-ref 已有依赖：

| 组件/功能 | 来源 | sparkle-ref 已有 |
|-----------|------|:---:|
| Card, CardBody, Chip, Button, Tooltip | @heroui/react | ✅ |
| Textarea, Input, Tabs, Tab, Badge, Divider | @heroui/react | ✅ |
| Avatar | @heroui-v3/react | ✅ |
| BasePage | @renderer/components/base/base-page | ✅ |
| useAppConfig | @renderer/hooks/use-app-config | ✅ |
| useSortable | @dnd-kit/sortable | ✅ |
| useSWR, mutate | swr | ✅ |
| react-icons (Fi*) | react-icons | ✅ |
| react-router-dom | react-router-dom | ✅ |
| Tailwind utility classes | tailwindcss | ✅ |
