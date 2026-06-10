# Sparkle 前端集成指南

## 概览

将 wechat-moments-automation 的前端页面集成到 sparkle-ref Electron 应用中。

**目标项目**: `C:\Users\woshi\Downloads\sparkle-ref`（基于 Electron + React 19 + HeroUI + Tailwind CSS）

## 集成步骤

### 1. 复制前端文件

将以下文件复制到 sparkle-ref 项目中：

```
integrations/sparkle-frontend/
├── src/api/client.ts           → sparkle-ref/src/renderer/src/api/wechatClient.ts
├── src/pages/Dashboard.tsx     → sparkle-ref/src/renderer/src/pages/wechat/Dashboard.tsx
├── src/pages/Composer.tsx      → sparkle-ref/src/renderer/src/pages/wechat/Composer.tsx
└── src/pages/SchedulePage.tsx  → sparkle-ref/src/renderer/src/pages/wechat/SchedulePage.tsx
```

### 2. 安装额外依赖

```bash
cd C:\Users\woshi\Downloads\sparkle-ref
pnpm add react-icons  # 如果还没安装
```

### 3. 添加路由

编辑 `sparkle-ref/src/renderer/src/App.tsx`，在路由表中添加：

```tsx
import Dashboard from "./pages/wechat/Dashboard";
import Composer from "./pages/wechat/Composer";
import SchedulePage from "./pages/wechat/SchedulePage";

// 在 <Routes> 中添加：
<Route path="/wechat" element={<Dashboard />} />
<Route path="/wechat/compose" element={<Composer />} />
<Route path="/wechat/schedule" element={<SchedulePage />} />
```

### 4. 添加侧边栏菜单项

编辑 sparkle-ref 的侧边栏组件，添加微信朋友圈菜单：

```tsx
<SidebarItem
  icon={<FiSend />}
  label="朋友圈"
  to="/wechat"
/>
```

### 5. 添加 Electron preload API（可选，用于文件选择对话框）

在 `sparkle-ref/src/preload/index.ts` 中添加：

```typescript
import { contextBridge, ipcRenderer, dialog } from "electron";

contextBridge.exposeInMainWorld("electron", {
  openFileDialog: () => ipcRenderer.invoke("open-file-dialog"),
});
```

在 `sparkle-ref/src/main/index.ts` 中添加 IPC 处理器：

```typescript
ipcMain.handle("open-file-dialog", async () => {
  const result = await dialog.showOpenDialog({
    properties: ["openFile", "multiSelections"],
    filters: [{ name: "Images", extensions: ["jpg", "jpeg", "png", "gif"] }],
  });
  return result.filePaths;
});
```

### 6. 环境变量

在 `sparkle-ref/.env.development` 中添加：

```
VITE_WECHAT_API=http://127.0.0.1:18080
```

### 7. 启动开发

```bash
# 终端 1：启动 Python API Server
cd wechat-moments-automation
python -m uvicorn src.api.server:app --host 127.0.0.1 --port 18080

# 终端 2：启动 Electron 开发模式
cd sparkle-ref
pnpm dev
```

## 页面结构

```
/wechat              → Dashboard (系统概览)
/wechat/compose       → Composer (编辑发布)
/wechat/schedule      → SchedulePage (定时任务)
```

## 技术栈兼容性

| 组件 | wechat-moments | sparkle-ref | 兼容 |
|------|---------------|-------------|------|
| React | 19.x | 19.2.6 | ✅ |
| UI 库 | HeroUI v3 | @heroui/react 2.8.10 + @heroui-v3/react 3.0.4 | ✅ |
| CSS | Tailwind 4 | Tailwind 4.3.0 | ✅ |
| 图标 | react-icons | react-icons 5.6.0 | ✅ |
| 路由 | react-router-dom | react-router-dom 7.15.0 | ✅ |
| Bundler | Vite | Vite 8.0 | ✅ |
| TypeScript | 6.0 | 6.0.3 | ✅ |
