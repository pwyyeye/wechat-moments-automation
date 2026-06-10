/*
 * WeChatUIA — 微信窗口交互微服务
 *
 * 职责：
 *   1. 附着微信窗口，触发无障碍模式（解锁完整 UIA 控件树）
 *   2. dump 完整控件树为 JSON（供 Python 端消费）
 *   3. 监控窗口位置变化（SetWinEventHook）
 *   4. 窗口激活/置顶/恢复操作
 *
 * 使用方式（从 Python 调用）：
 *   WeChatUIA.exe dump-tree              # 输出控件树 JSON 到 stdout
 *   WeChatUIA.exe activate               # 激活微信窗口到前台
 *   WeChatUIA.exe monitor                # 持续监控窗口位置，变化时输出 JSON
 *   WeChatUIA.exe check-login            # 检测是否掉线（OCR 关键词快速扫描）
 *   WeChatUIA.exe get-window-rect        # 获取窗口位置和尺寸
 *
 * 编译：
 *   dotnet publish -c Release -o publish
 *   (输出 publish/WeChatUIA.exe)
 *
 * 依赖：
 *   .NET 10.0 SDK
 *   NuGet: FlaUI.UIA3
 */

#nullable enable

using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Linq;
using System.Runtime.InteropServices;
using System.Text.Json;
using System.Threading;
using FlaUI.Core;
using FlaUI.UIA3;
using FlaUI.Core.AutomationElements;
using FlaUI.Core.Definitions;
using FlaUI.Core.Tools;

namespace WeChatUIA
{
    // ═══════════════════════════════════════════════════════
    // JSON 输出模型
    // ═══════════════════════════════════════════════════════

    public class UIElementInfo
    {
        public string ControlType { get; set; } = "";
        public string Name { get; set; } = "";
        public string AutomationId { get; set; } = "";
        public string ClassName { get; set; } = "";
        public int X { get; set; }
        public int Y { get; set; }
        public int Width { get; set; }
        public int Height { get; set; }
        public bool IsEnabled { get; set; }
        public bool IsOffscreen { get; set; }
        public List<UIElementInfo> Children { get; set; } = new();
    }

    public class WindowInfo
    {
        public int Left { get; set; }
        public int Top { get; set; }
        public int Right { get; set; }
        public int Bottom { get; set; }
        public int Width => Right - Left;
        public int Height => Bottom - Top;
        public string Title { get; set; } = "";
        public string ClassName { get; set; } = "";
        public bool IsMinimized { get; set; }
        public bool IsVisible { get; set; }
        public long Timestamp { get; set; }
    }

    public class TreeOutput
    {
        public WindowInfo Window { get; set; } = new();
        public UIElementInfo RootElement { get; set; } = new();
        public int TotalElements { get; set; }
        public long Timestamp { get; set; }
        public string Version { get; set; } = "1.0";
    }

    public class LoginCheckResult
    {
        public bool IsLoggedIn { get; set; }
        public string DetectedPage { get; set; } = "";
        public List<string> NavLabels { get; set; } = new();
        public long Timestamp { get; set; }
    }

    // ═══════════════════════════════════════════════════════
    // 主程序
    // ═══════════════════════════════════════════════════════

    class Program
    {
        // Win32 API
        [DllImport("user32.dll")]
        static extern IntPtr FindWindow(string lpClassName, string lpWindowName);

        [DllImport("user32.dll")]
        static extern bool IsIconic(IntPtr hWnd);

        [DllImport("user32.dll")]
        static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);

        [DllImport("user32.dll")]
        static extern bool SetForegroundWindow(IntPtr hWnd);

        [DllImport("user32.dll")]
        static extern bool GetWindowRect(IntPtr hWnd, out RECT lpRect);

        [DllImport("user32.dll")]
        static extern bool SetWindowPos(IntPtr hWnd, IntPtr hWndInsertAfter,
            int X, int Y, int cx, int cy, uint uFlags);

        [DllImport("user32.dll")]
        static extern IntPtr SetWinEventHook(uint eventMin, uint eventMax,
            IntPtr hmodWinEventProc, WinEventDelegate lpfnWinEventProc,
            int idProcess, int idThread, uint dwFlags);

        delegate void WinEventDelegate(IntPtr hWinEventHook, uint eventType,
            IntPtr hwnd, int idObject, int idChild, uint dwEventThread, uint dwmsEventTime);

        [StructLayout(LayoutKind.Sequential)]
        struct RECT
        {
            public int Left, Top, Right, Bottom;
        }

        // 常量
        const int SW_RESTORE = 9;
        const int SW_SHOW = 5;
        static readonly IntPtr HWND_TOPMOST = new IntPtr(-1);
        static readonly IntPtr HWND_NOTOPMOST = new IntPtr(-2);
        const uint SWP_NOMOVE = 0x0002;
        const uint SWP_NOSIZE = 0x0001;
        const uint EVENT_OBJECT_LOCATIONCHANGE = 0x800B;
        const uint EVENT_OBJECT_NAMECHANGE = 0x800C;
        const uint WINEVENT_OUTOFCONTEXT = 0x0000;
        const uint WINEVENT_SKIPOWNPROCESS = 0x0002;

        // 全局状态
        static AutomationBase? _automation;
        static readonly JsonSerializerOptions _jsonOpts = new()
        {
            WriteIndented = false,
            PropertyNamingPolicy = JsonNamingPolicy.CamelCase,
        };

        static int Main(string[] args)
        {
            if (args.Length == 0)
            {
                Console.Error.WriteLine("用法: WeChatUIA.exe <command>");
                Console.Error.WriteLine("命令: dump-tree | activate | monitor | check-login | get-window-rect");
                return 1;
            }

            try
            {
                switch (args[0].ToLower())
                {
                    case "dump-tree":
                        return CmdDumpTree();
                    case "activate":
                        return CmdActivateWindow();
                    case "monitor":
                        return CmdMonitorWindow();
                    case "check-login":
                        return CmdCheckLogin();
                    case "get-window-rect":
                        return CmdGetWindowRect();
                    default:
                        Console.Error.WriteLine($"未知命令: {args[0]}");
                        return 1;
                }
            }
            catch (Exception ex)
            {
                var error = new { error = ex.Message, trace = ex.StackTrace };
                Console.WriteLine(JsonSerializer.Serialize(error, _jsonOpts));
                return 2;
            }
        }

        // ═════════════════════════════════════════════════════
        // 命令实现
        // ═════════════════════════════════════════════════════

        /// <summary>
        /// dump-tree: 输出完整控件树 JSON
        /// </summary>
        static int CmdDumpTree()
        {
            if (!AttachToWeChat(out var window, out var root))
            {
                Console.Error.WriteLine("无法附着微信窗口");
                return 1;
            }

            var treeInfo = BuildElementTree(root);
            var rect = GetWindowRect(window);

            var output = new TreeOutput
            {
                Window = new WindowInfo
                {
                    Left = rect.Left, Top = rect.Top,
                    Right = rect.Right, Bottom = rect.Bottom,
                    Title = window.Title,
                    ClassName = window.ClassName,
                    IsMinimized = IsIconic(window.Properties.NativeWindowHandle),
                    IsVisible = !window.IsOffscreen,
                    Timestamp = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds(),
                },
                RootElement = treeInfo,
                TotalElements = CountElements(treeInfo),
                Timestamp = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds(),
            };

            Console.WriteLine(JsonSerializer.Serialize(output, _jsonOpts));
            return 0;
        }

        /// <summary>
        /// activate: 激活微信窗口到前台
        /// </summary>
        static int CmdActivateWindow()
        {
            var hwnd = FindWindow("WeChatMainWndForPC", "微信");
            if (hwnd == IntPtr.Zero)
            {
                Console.Error.WriteLine("未找到微信窗口");
                return 1;
            }

            if (IsIconic(hwnd))
                ShowWindow(hwnd, SW_RESTORE);

            // 短暂置顶后取消（确保窗口可见但不霸占屏幕）
            SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE);
            Thread.Sleep(100);
            SetForegroundWindow(hwnd);
            Thread.Sleep(100);
            SetWindowPos(hwnd, HWND_NOTOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE);

            var result = new { success = true, message = "微信窗口已激活" };
            Console.WriteLine(JsonSerializer.Serialize(result, _jsonOpts));
            return 0;
        }

        /// <summary>
        /// monitor: 持续监控窗口位置变化
        /// </summary>
        static int CmdMonitorWindow()
        {
            var hwnd = FindWindow("WeChatMainWndForPC", "微信");
            if (hwnd == IntPtr.Zero)
            {
                Console.Error.WriteLine("未找到微信窗口");
                return 1;
            }

            // 注册窗口事件 Hook
            uint processId;
            GetWindowThreadProcessId(hwnd, out processId);
            uint threadId = GetWindowThreadProcessId(hwnd, out _);

            var hook = SetWinEventHook(
                EVENT_OBJECT_LOCATIONCHANGE,
                EVENT_OBJECT_LOCATIONCHANGE,
                IntPtr.Zero,
                (hHook, eventType, hWnd, idObject, idChild, dwEventThread, dwmsEventTime) =>
                {
                    if (hWnd != hwnd) return;

                    var rect = new RECT();
                    if (GetWindowRect(hwnd, out rect))
                    {
                        var info = new WindowInfo
                        {
                            Left = rect.Left, Top = rect.Top,
                            Right = rect.Right, Bottom = rect.Bottom,
                            Title = "微信",
                            IsMinimized = IsIconic(hwnd),
                            Timestamp = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds(),
                        };
                        Console.WriteLine(JsonSerializer.Serialize(info, _jsonOpts));
                    }
                },
                0, 0,
                WINEVENT_OUTOFCONTEXT
            );

            if (hook == IntPtr.Zero)
            {
                Console.Error.WriteLine("无法注册窗口事件 Hook");
                return 1;
            }

            // 初始输出
            var initialRect = new RECT();
            GetWindowRect(hwnd, out initialRect);
            var initial = new WindowInfo
            {
                Left = initialRect.Left, Top = initialRect.Top,
                Right = initialRect.Right, Bottom = initialRect.Bottom,
                Title = "微信",
                IsMinimized = IsIconic(hwnd),
                Timestamp = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds(),
            };
            Console.WriteLine(JsonSerializer.Serialize(initial, _jsonOpts));

            // 持续运行，等待外部 kill
            Console.Error.WriteLine("监控中... (按 Ctrl+C 停止)");
            while (true)
            {
                Thread.Sleep(100);
                // Windows 消息循环由 SetWinEventHook 内部处理
            }
        }

        /// <summary>
        /// check-login: 检查微信是否已登录（通过控件树快速扫描）
        /// </summary>
        static int CmdCheckLogin()
        {
            if (!AttachToWeChat(out var window, out var root))
            {
                var result = new LoginCheckResult
                {
                    IsLoggedIn = false,
                    DetectedPage = "微信未运行",
                    Timestamp = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds(),
                };
                Console.WriteLine(JsonSerializer.Serialize(result, _jsonOpts));
                return 0;
            }

            // 遍历控件树，查找导航栏标签
            // 已登录的微信首页有聊天/通讯录/朋友圈等导航按钮
            var navLabels = new List<string>();
            var expectedLabels = new[] { "聊天", "通讯录", "朋友圈" };

            CollectNavLabels(root, navLabels, expectedLabels);

            bool isLoggedIn = expectedLabels.Any(l => navLabels.Contains(l));

            string detectedPage = isLoggedIn ? "微信主界面" : "登录页面或未知页面";

            var loginResult = new LoginCheckResult
            {
                IsLoggedIn = isLoggedIn,
                DetectedPage = detectedPage,
                NavLabels = navLabels,
                Timestamp = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds(),
            };

            Console.WriteLine(JsonSerializer.Serialize(loginResult, _jsonOpts));
            return 0;
        }

        /// <summary>
        /// get-window-rect: 获取窗口位置和尺寸
        /// </summary>
        static int CmdGetWindowRect()
        {
            var hwnd = FindWindow("WeChatMainWndForPC", "微信");
            if (hwnd == IntPtr.Zero)
            {
                Console.Error.WriteLine("未找到微信窗口");
                return 1;
            }

            var rect = new RECT();
            GetWindowRect(hwnd, out rect);

            var info = new WindowInfo
            {
                Left = rect.Left, Top = rect.Top,
                Right = rect.Right, Bottom = rect.Bottom,
                Title = "微信",
                ClassName = "WeChatMainWndForPC",
                IsMinimized = IsIconic(hwnd),
                Timestamp = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds(),
            };

            Console.WriteLine(JsonSerializer.Serialize(info, _jsonOpts));
            return 0;
        }

        // ═════════════════════════════════════════════════════
        // 核心辅助方法
        // ═════════════════════════════════════════════════════

        /// <summary>
        /// 附着微信窗口 + 触发无障碍模式 + 返回根元素
        /// </summary>
        static bool AttachToWeChat(out Window window, out AutomationElement root)
        {
            window = null!;
            root = null!;

            try
            {
                var processes = Process.GetProcessesByName("WeChat");
                if (processes.Length == 0)
                {
                    Console.Error.WriteLine("微信进程未运行");
                    return false;
                }

                var process = processes[0];
                var hwnd = process.MainWindowHandle;
                if (hwnd == IntPtr.Zero)
                {
                    Console.Error.WriteLine("微信主窗口句柄为空");
                    return false;
                }

                // 初始化 UIA3 automation
                _automation = new UIA3Automation();

                // 附着到微信窗口 —— 这一步触发无障碍模式
                var app = FlaUI.Core.Application.Attach(process.Id);
                window = app.GetMainWindow(_automation);

                if (window == null)
                {
                    Console.Error.WriteLine("无法附着到微信主窗口");
                    return false;
                }

                // 确保窗口可见
                if (IsIconic(hwnd))
                {
                    ShowWindow(hwnd, SW_RESTORE);
                    Thread.Sleep(300);
                    window = app.GetMainWindow(_automation)!;
                }

                root = window;
                return true;
            }
            catch (Exception ex)
            {
                Console.Error.WriteLine($"附着失败: {ex.Message}");
                return false;
            }
        }

        /// <summary>
        /// 递归构建控件树信息
        /// </summary>
        static UIElementInfo BuildElementTree(AutomationElement element, int maxDepth = 20)
        {
            var info = new UIElementInfo();

            try
            {
                info.ControlType = element.ControlType.ToString();
                info.Name = element.Name ?? "";
                info.AutomationId = element.AutomationId ?? "";
                info.ClassName = element.ClassName ?? "";
                info.IsEnabled = element.IsEnabled;
                info.IsOffscreen = element.IsOffscreen;

                var rect = element.BoundingRectangle;
                if (!rect.IsEmpty)
                {
                    info.X = (int)rect.X;
                    info.Y = (int)rect.Y;
                    info.Width = (int)rect.Width;
                    info.Height = (int)rect.Height;
                }
            }
            catch { /* 某些属性可能不可访问 */ }

            // 递归子元素
            if (maxDepth > 0)
            {
                try
                {
                    // 使用 ControlViewWalker 过滤布局元素
                    var walker = _automation!.TreeWalkerFactory.GetControlViewWalker();
                    var child = walker.GetFirstChild(element);
                    int childCount = 0;

                    while (child != null && childCount < 500) // 限制防止溢出
                    {
                        var childInfo = BuildElementTree(child, maxDepth - 1);
                        if (childInfo.ControlType != "")
                            info.Children.Add(childInfo);

                        child = walker.GetNextSibling(child);
                        childCount++;
                    }
                }
                catch { /* 遍历可能不完全 */ }
            }

            return info;
        }

        /// <summary>
        /// 收集导航栏标签（用于登录检测）
        /// </summary>
        static void CollectNavLabels(AutomationElement element, List<string> found,
            string[] targets, int depth = 0)
        {
            if (depth > 8 || found.Count >= targets.Length) return;

            try
            {
                var name = element.Name ?? "";
                if (targets.Any(t => t == name))
                    found.Add(name);

                var walker = _automation!.TreeWalkerFactory.GetControlViewWalker();
                var child = walker.GetFirstChild(element);
                int count = 0;

                while (child != null && count < 300)
                {
                    CollectNavLabels(child, found, targets, depth + 1);
                    child = walker.GetNextSibling(child);
                    count++;
                }
            }
            catch { }
        }

        /// <summary>
        /// 统计控件树元素总数
        /// </summary>
        static int CountElements(UIElementInfo element)
        {
            int count = 1;
            foreach (var child in element.Children)
                count += CountElements(child);
            return count;
        }

        /// <summary>
        /// 获取窗口矩形
        /// </summary>
        static RECT GetWindowRect(Window window)
        {
            var rect = new RECT();
            var hwnd = window.Properties.NativeWindowHandle;
            if (hwnd != IntPtr.Zero)
                GetWindowRect(hwnd, out rect);
            return rect;
        }

        [DllImport("user32.dll")]
        static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint lpdwProcessId);
    }
}
