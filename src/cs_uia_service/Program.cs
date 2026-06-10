/*
 * WeChatUIA v2 — 原生 UIAutomation 客户端，触发微信 4.x 无障碍模式。
 *
 * 核心原理:
 *   微信 4.x 检查是否有合规 UIA 客户端附着到其窗口。
 *   当检测到 UIAutomationClient.dll 的调用时，微信会加载完整的控件 Provider。
 *   用原生 System.Windows.Automation 替代 FlaUI，确保信号不被中间层阻断。
 *
 * 使用方式:
 *   WeChatUIA.exe dump-tree    — 输出控件树 JSON
 *   WeChatUIA.exe check-login  — 检测登录状态
 *   WeChatUIA.exe activate     — 激活微信窗口
 *   WeChatUIA.exe monitor      — 窗口位置监控
 *   WeChatUIA.exe get-rect     — 窗口位置
 */

#nullable enable

using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Linq;
using System.Runtime.InteropServices;
using System.Text.Json;
using System.Threading;
using System.Windows;
using System.Windows.Automation;

namespace WeChatUIA
{
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
        public string Title { get; set; } = "";
        public string ClassName { get; set; } = "";
        public bool IsMinimized { get; set; }
        public long Timestamp { get; set; }
    }

    public class TreeOutput
    {
        public WindowInfo Window { get; set; } = new();
        public UIElementInfo RootElement { get; set; } = new();
        public int TotalElements { get; set; }
        public long Timestamp { get; set; }
    }

    public class LoginCheckResult
    {
        public bool IsLoggedIn { get; set; }
        public string DetectedPage { get; set; } = "";
        public List<string> NavLabels { get; set; } = new();
        public long Timestamp { get; set; }
    }

    // ═══════════════════════════════════════════════════════════
    // Win32 API
    // ═══════════════════════════════════════════════════════════

    class Win32
    {
        [DllImport("user32.dll")]
        public static extern IntPtr FindWindow(string? lpClassName, string lpWindowName);

        [DllImport("user32.dll")]
        public static extern bool EnumWindows(EnumWindowsProc lpEnumFunc, IntPtr lParam);

        public delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);

        [DllImport("user32.dll")]
        public static extern int GetClassName(IntPtr hWnd, System.Text.StringBuilder lpClassName, int nMaxCount);

        [DllImport("user32.dll")]
        public static extern int GetWindowText(IntPtr hWnd, System.Text.StringBuilder lpString, int nMaxCount);

        [DllImport("user32.dll")]
        public static extern bool IsWindowVisible(IntPtr hWnd);

        [DllImport("user32.dll")]
        public static extern bool IsIconic(IntPtr hWnd);

        [DllImport("user32.dll")]
        public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);

        [DllImport("user32.dll")]
        public static extern bool SetForegroundWindow(IntPtr hWnd);

        [DllImport("user32.dll")]
        public static extern bool GetWindowRect(IntPtr hWnd, out RECT lpRect);

        [DllImport("user32.dll")]
        public static extern bool SetWindowPos(IntPtr hWnd, IntPtr hWndInsertAfter,
            int X, int Y, int cx, int cy, uint uFlags);

        [DllImport("user32.dll")]
        public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint lpdwProcessId);

        [DllImport("user32.dll")]
        public static extern IntPtr SetWinEventHook(uint eventMin, uint eventMax,
            IntPtr hmodWinEventProc, WinEventDelegate lpfnWinEventProc,
            uint idProcess, uint idThread, uint dwFlags);

        public delegate void WinEventDelegate(IntPtr hWinEventHook, uint eventType,
            IntPtr hwnd, int idObject, int idChild, uint dwEventThread, uint dwmsEventTime);

        [StructLayout(LayoutKind.Sequential)]
        public struct RECT
        {
            public int Left, Top, Right, Bottom;
        }

        public const int SW_RESTORE = 9;
        public static readonly IntPtr HWND_TOPMOST = new IntPtr(-1);
        public static readonly IntPtr HWND_NOTOPMOST = new IntPtr(-2);
        public const uint SWP_NOMOVE = 0x0002;
        public const uint SWP_NOSIZE = 0x0001;
        public const uint EVENT_OBJECT_LOCATIONCHANGE = 0x800B;
        public const uint WINEVENT_OUTOFCONTEXT = 0x0000;
    }

    // ═══════════════════════════════════════════════════════════
    // 微信窗口查找
    // ═══════════════════════════════════════════════════════════

    static class WeChatWindow
    {
        public static IntPtr Find()
        {
            IntPtr result = IntPtr.Zero;

            Win32.EnumWindows((hwnd, _) =>
            {
                if (!Win32.IsWindowVisible(hwnd)) return true;

                var cls = new System.Text.StringBuilder(256);
                Win32.GetClassName(hwnd, cls, 256);
                var cn = cls.ToString();

                // Qt 版本: 类名包含 Qt 且标题为 "微信"
                if (cn.Contains("Qt"))
                {
                    var title = new System.Text.StringBuilder(256);
                    Win32.GetWindowText(hwnd, title, 256);
                    if (title.ToString() == "微信" && result == IntPtr.Zero)
                    {
                        result = hwnd;
                    }
                }
                // 传统 Win32 版本
                else if (cn == "WeChatMainWndForPC")
                {
                    result = hwnd;
                }

                return true;
            }, IntPtr.Zero);

            return result;
        }

        public static bool Activate(IntPtr hwnd)
        {
            if (Win32.IsIconic(hwnd))
                Win32.ShowWindow(hwnd, Win32.SW_RESTORE);
            Win32.SetWindowPos(hwnd, Win32.HWND_TOPMOST, 0, 0, 0, 0, Win32.SWP_NOMOVE | Win32.SWP_NOSIZE);
            Thread.Sleep(100);
            Win32.SetForegroundWindow(hwnd);
            Thread.Sleep(100);
            Win32.SetWindowPos(hwnd, Win32.HWND_NOTOPMOST, 0, 0, 0, 0, Win32.SWP_NOMOVE | Win32.SWP_NOSIZE);
            return true;
        }

        public static Win32.RECT GetRect(IntPtr hwnd)
        {
            Win32.GetWindowRect(hwnd, out var rect);
            return rect;
        }
    }

    // ═══════════════════════════════════════════════════════════
    // 主程序
    // ═══════════════════════════════════════════════════════════

    class Program
    {
        static readonly JsonSerializerOptions _jsonOpts = new()
        {
            WriteIndented = false,
            PropertyNamingPolicy = JsonNamingPolicy.CamelCase,
        };

        [STAThread]
        static int Main(string[] args)
        {
            if (args.Length == 0)
            {
                Console.Error.WriteLine("用法: WeChatUIA.exe <command>");
                Console.Error.WriteLine("命令: dump-tree | check-login | activate | monitor | get-rect");
                return 1;
            }

            try
            {
                return args[0].ToLower() switch
                {
                    "dump-tree" => CmdDumpTree(),
                    "check-login" => CmdCheckLogin(),
                    "activate" => CmdActivate(),
                    "monitor" => CmdMonitor(),
                    "get-rect" => CmdGetRect(),
                    _ => 1
                };
            }
            catch (Exception ex)
            {
                var error = new { error = ex.Message };
                Console.WriteLine(JsonSerializer.Serialize(error, _jsonOpts));
                return 2;
            }
        }

        // ═══════════════════════════════════════════════════════
        // dump-tree: 原生 UIAutomation 遍历控件树
        // ═══════════════════════════════════════════════════════

        static int CmdDumpTree()
        {
            var hwnd = WeChatWindow.Find();
            if (hwnd == IntPtr.Zero)
            {
                Console.Error.WriteLine("未找到微信窗口");
                return 1;
            }

            // ★ 关键步骤: AutomationElement.FromHandle() 会触发微信的无障碍模式
            // 微信检测到 UIAutomationClient 附着后，会加载完整控件 Provider
            AutomationElement root;
            try
            {
                root = AutomationElement.FromHandle(hwnd);
            }
            catch (Exception ex)
            {
                Console.Error.WriteLine($"无法附着微信窗口: {ex.Message}");
                return 1;
            }

            // 等待微信加载完整控件树（无障碍模式需要时间激活）
            Thread.Sleep(500);

            // 强制刷新——遍历 ControlView 触发 Provider 加载
            var walker = TreeWalker.ControlViewWalker;
            var dummy = walker.GetFirstChild(root);
            Thread.Sleep(200);

            // 现在拿到完整的树
            var treeInfo = BuildTree(root, walker, 0, 30);

            var rect = WeChatWindow.GetRect(hwnd);
            var output = new TreeOutput
            {
                Window = new WindowInfo
                {
                    Left = rect.Left, Top = rect.Top,
                    Right = rect.Right, Bottom = rect.Bottom,
                    Title = "微信", ClassName = "Qt",
                    IsMinimized = Win32.IsIconic(hwnd),
                    Timestamp = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds(),
                },
                RootElement = treeInfo,
                TotalElements = CountElements(treeInfo),
                Timestamp = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds(),
            };

            Console.WriteLine(JsonSerializer.Serialize(output, _jsonOpts));
            return 0;
        }

        static UIElementInfo BuildTree(AutomationElement element, TreeWalker walker, int depth, int maxDepth)
        {
            var info = new UIElementInfo();

            try
            {
                info.ControlType = element.Current.ControlType.ProgrammaticName.Replace("ControlType.", "");
                info.Name = element.Current.Name ?? "";
                info.AutomationId = element.Current.AutomationId ?? "";
                info.ClassName = element.Current.ClassName ?? "";
                info.IsEnabled = element.Current.IsEnabled;
                info.IsOffscreen = element.Current.IsOffscreen;

                var rect = element.Current.BoundingRectangle;
                if (!rect.IsEmpty)
                {
                    info.X = (int)rect.X;
                    info.Y = (int)rect.Y;
                    info.Width = (int)rect.Width;
                    info.Height = (int)rect.Height;
                }
            }
            catch (ElementNotAvailableException) { return info; }
            catch { /* some props may not be available */ }

            if (depth < maxDepth)
            {
                try
                {
                    var child = walker.GetFirstChild(element);
                    int count = 0;

                    while (child != null && count < 500)
                    {
                        var childInfo = BuildTree(child, walker, depth + 1, maxDepth);
                        // 只保留有意义的元素
                        if (!string.IsNullOrEmpty(childInfo.ControlType))
                            info.Children.Add(childInfo);

                        child = walker.GetNextSibling(child);
                        count++;
                    }

                    // WeChat 4.x 元素可能在 RawView 中
                    if (count == 0 && depth < 2)
                    {
                        var rawWalker = TreeWalker.RawViewWalker;
                        child = rawWalker.GetFirstChild(element);
                        while (child != null && count < 500)
                        {
                            var childInfo = BuildTree(child, rawWalker, depth + 1, maxDepth);
                            if (!string.IsNullOrEmpty(childInfo.ControlType))
                                info.Children.Add(childInfo);
                            child = rawWalker.GetNextSibling(child);
                            count++;
                        }
                    }
                }
                catch { }
            }

            return info;
        }

        static int CountElements(UIElementInfo element)
        {
            int count = 1;
            foreach (var child in element.Children)
                count += CountElements(child);
            return count;
        }

        // ═══════════════════════════════════════════════════════
        // check-login: 检测是否已登录
        // ═══════════════════════════════════════════════════════

        static int CmdCheckLogin()
        {
            var hwnd = WeChatWindow.Find();
            if (hwnd == IntPtr.Zero)
            {
                var r = new LoginCheckResult { IsLoggedIn = false, DetectedPage = "微信未运行",
                    Timestamp = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() };
                Console.WriteLine(JsonSerializer.Serialize(r, _jsonOpts));
                return 0;
            }

            try
            {
                var root = AutomationElement.FromHandle(hwnd);
                Thread.Sleep(300); // 等微信响应

                var walker = TreeWalker.ControlViewWalker;
                var navLabels = new List<string>();
                var targets = new[] { "聊天", "通讯录", "朋友圈", "视频号" };

                CollectNavLabels(root, walker, navLabels, targets, 0);

                // ControlView 没找到的话，尝试 RawView
                if (navLabels.Count == 0)
                {
                    var rawWalker = TreeWalker.RawViewWalker;
                    CollectNavLabels(root, rawWalker, navLabels, targets, 0);
                }

                bool loggedIn = navLabels.Any();
                var result = new LoginCheckResult
                {
                    IsLoggedIn = loggedIn,
                    DetectedPage = loggedIn ? "微信主界面" : "登录页面或未知页面",
                    NavLabels = navLabels,
                    Timestamp = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds(),
                };

                Console.WriteLine(JsonSerializer.Serialize(result, _jsonOpts));
            }
            catch (Exception ex)
            {
                var r = new LoginCheckResult { IsLoggedIn = false, DetectedPage = $"异常: {ex.Message}",
                    Timestamp = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() };
                Console.WriteLine(JsonSerializer.Serialize(r, _jsonOpts));
            }

            return 0;
        }

        static void CollectNavLabels(AutomationElement element, TreeWalker walker,
            List<string> found, string[] targets, int depth)
        {
            if (depth > 15 || found.Count >= targets.Length) return;

            try
            {
                var name = element.Current.Name ?? "";
                if (!string.IsNullOrEmpty(name) && targets.Contains(name))
                    found.Add(name);

                var child = walker.GetFirstChild(element);
                int count = 0;
                while (child != null && count < 300)
                {
                    CollectNavLabels(child, walker, found, targets, depth + 1);
                    child = walker.GetNextSibling(child);
                    count++;
                }
            }
            catch { }
        }

        // ═══════════════════════════════════════════════════════
        // 其他命令
        // ═══════════════════════════════════════════════════════

        static int CmdActivate()
        {
            var hwnd = WeChatWindow.Find();
            if (hwnd == IntPtr.Zero) { Console.Error.WriteLine("未找到微信窗口"); return 1; }
            WeChatWindow.Activate(hwnd);
            Console.WriteLine("{\"success\":true}");
            return 0;
        }

        static int CmdMonitor()
        {
            var hwnd = WeChatWindow.Find();
            if (hwnd == IntPtr.Zero) { Console.Error.WriteLine("未找到微信窗口"); return 1; }

            Win32.GetWindowThreadProcessId(hwnd, out uint pid);
            uint tid = Win32.GetWindowThreadProcessId(hwnd, out _);

            Win32.SetWinEventHook(Win32.EVENT_OBJECT_LOCATIONCHANGE, Win32.EVENT_OBJECT_LOCATIONCHANGE,
                IntPtr.Zero, (h, evt, w, idObj, idChild, evtTid, time) =>
                {
                    if (w != hwnd) return;
                    var rect = WeChatWindow.GetRect(hwnd);
                    var info = new WindowInfo
                    {
                        Left = rect.Left, Top = rect.Top, Right = rect.Right, Bottom = rect.Bottom,
                        Title = "微信", IsMinimized = Win32.IsIconic(hwnd),
                        Timestamp = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds(),
                    };
                    Console.WriteLine(JsonSerializer.Serialize(info, _jsonOpts));
                }, pid, tid, Win32.WINEVENT_OUTOFCONTEXT);

            Console.Error.WriteLine("监控中...");
            while (true) { Thread.Sleep(100); }
        }

        static int CmdGetRect()
        {
            var hwnd = WeChatWindow.Find();
            if (hwnd == IntPtr.Zero) { Console.Error.WriteLine("未找到微信窗口"); return 1; }
            var rect = WeChatWindow.GetRect(hwnd);
            var info = new WindowInfo
            {
                Left = rect.Left, Top = rect.Top, Right = rect.Right, Bottom = rect.Bottom,
                Title = "微信", ClassName = "Qt", IsMinimized = Win32.IsIconic(hwnd),
                Timestamp = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds(),
            };
            Console.WriteLine(JsonSerializer.Serialize(info, _jsonOpts));
            return 0;
        }
    }
}
