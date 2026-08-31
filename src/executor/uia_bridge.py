"""
UIA 桥接层 —— Python 与 C# WeChatUIA.exe 之间的通信。

C# 微服务负责：
  1. 附着微信窗口 + 触发无障碍模式（WeChat 4.x 关键能力）
  2. dump 完整控件树为 JSON
  3. 监控窗口位置变化
  4. 检测登录状态
  5. 窗口激活/置顶

Python 端通过 subprocess 调用 C# exe，解析 JSON 输出。

为什么需要这个桥接层：
  - WeChat 4.x 默认隐藏控件树，只有 C# 原生 UIAutomation 能触发暴露
  - Python 的 pywin32/pywinauto 在微信 4.x 下无法获取完整控件信息
  - C# 的 FlaUI 库是 Windows UIAutomation 的最佳封装

使用方式：
    bridge = UIABridge()
    tree = bridge.dump_tree()        # 获取完整控件树
    bridge.activate_window()         # 激活微信窗口
    login_ok = bridge.check_login()  # 检测是否已登录
    rect = bridge.get_window_rect()  # 获取窗口位置

编译 C# 服务：
    cd src/cs_uia_service
    dotnet publish -c Release -o publish

Author: 版本无关微信自动化系统
"""

import logging
import subprocess
import json
import time
import threading
from typing import Optional, List, Dict, Any, Tuple
from pathlib import Path

logger = logging.getLogger(__name__)


class UIABridge:
    """
    Python ↔ C# UIAutomation 桥接层。
    """

    def __init__(self, exe_path: str = None):
        """
        Args:
            exe_path: WeChatUIA.exe 的路径。
                     默认在 src/cs_uia_service/publish/WeChatUIA.exe
        """
        if exe_path is None:
            exe_path = (
                Path(__file__).parent.parent / "cs_uia_service" /
                "publish" / "WeChatUIA.exe"
            )

        self._exe_path = Path(exe_path)
        self._monitor_process: Optional[subprocess.Popen] = None
        self._monitor_thread: Optional[threading.Thread] = None
        self._window_callbacks: list = []  # 窗口变化回调

        if not self._exe_path.exists():
            logger.warning(
                f"WeChatUIA.exe 未找到: {self._exe_path}\n"
                f"请先编译 C# 服务:\n"
                f"  cd src/cs_uia_service\n"
                f"  dotnet publish -c Release -o publish\n"
                f"  需要安装 .NET 8.0 SDK: https://dotnet.microsoft.com/download"
            )

    @property
    def available(self) -> bool:
        return self._exe_path.exists()

    # ═══════════════════════════════════════════════════════
    # 控件树操作
    # ═══════════════════════════════════════════════════════

    def dump_tree(self) -> Optional[Dict[str, Any]]:
        """
        获取微信完整控件树。

        Returns:
            {
                "window": { "left": 0, "top": 0, ... },
                "rootElement": {
                    "controlType": "Window",
                    "name": "微信",
                    "children": [ ... ]
                },
                "totalElements": 156,
                "timestamp": 1234567890
            }
        """
        result = self._run("dump-tree")
        if result is None:
            return None

        data = json.loads(result)
        logger.info(
            f"控件树: {data.get('totalElements', 0)} 个元素, "
            f"窗口=({data.get('window', {}).get('left', 0)}, "
            f"{data.get('window', {}).get('top', 0)})"
        )
        return data

    def find_elements_by_name(self, name: str) -> List[Dict[str, Any]]:
        """在控件树中按名称查找元素"""
        tree = self.dump_tree()
        if tree is None:
            return []

        results = []

        def search(node, target):
            if node.get('name') == target:
                results.append(node)
            for child in node.get('children', []):
                search(child, target)

        search(tree.get('rootElement', {}), name)
        return results

    def find_elements_by_type(self, control_type: str) -> List[Dict[str, Any]]:
        """在控件树中按控件类型查找"""
        tree = self.dump_tree()
        if tree is None:
            return []

        results = []

        def search(node, target):
            if node.get('controlType') == target:
                results.append(node)
            for child in node.get('children', []):
                search(child, target)

        search(tree.get('rootElement', {}), control_type)
        return results

    def get_all_clickable(self) -> List[Dict[str, Any]]:
        """获取所有可点击的按钮"""
        return self.find_elements_by_type("Button")

    # ═══════════════════════════════════════════════════════
    # 窗口操作
    # ═══════════════════════════════════════════════════════

    def activate_window(self) -> bool:
        """激活微信窗口到前台"""
        result = self._run("activate")
        if result is None:
            return False
        data = json.loads(result)
        return data.get('success', False)

    def open_moments(self, timeout: float = 8.0) -> bool:
        """Open only the Moments list through the dedicated safe command."""
        result = self._run("open-moments", timeout=timeout)
        if result is None:
            return False
        data = json.loads(result)
        if not data.get('success', False):
            logger.error("朋友圈预检失败: %s", data.get('reason', 'unknown'))
            return False
        logger.info("朋友圈列表已打开: %s", data.get('method', 'unknown'))
        return True

    def get_window_rect(self) -> Optional[Tuple[int, int, int, int]]:
        """
        获取微信窗口位置。

        Returns:
            (left, top, right, bottom) 或 None
        """
        result = self._run("get-window-rect")
        if result is None:
            return None

        data = json.loads(result)
        return (
            data.get('left', 0),
            data.get('top', 0),
            data.get('right', 0),
            data.get('bottom', 0),
        )

    # ═══════════════════════════════════════════════════════
    # 登录检测
    # ═══════════════════════════════════════════════════════

    def check_login(self) -> Dict[str, Any]:
        """
        检测微信是否已登录。

        Returns:
            {
                "isLoggedIn": true/false,
                "detectedPage": "微信主界面" | "登录页面" | "微信未运行",
                "navLabels": ["聊天", "通讯录", ...],
                "timestamp": 1234567890
            }
        """
        result = self._run("check-login")
        if result is None:
            return {
                'isLoggedIn': False,
                'detectedPage': '检测失败',
                'navLabels': [],
                'timestamp': int(time.time() * 1000),
            }

        return json.loads(result)

    # ═══════════════════════════════════════════════════════
    # 窗口位置监控
    # ═══════════════════════════════════════════════════════

    def start_window_monitor(self, on_change: callable = None,
                             run_in_background: bool = True):
        """
        启动窗口位置监控。

        当微信窗口移动/缩放时，自动触发回调。

        Args:
            on_change: 回调函数 callback(window_info: dict)
            run_in_background: 是否在后台线程运行
        """
        if on_change:
            self._window_callbacks.append(on_change)

        if not self.available:
            logger.warning("WeChatUIA.exe 不可用，窗口监控无法启动")
            return

        logger.info("启动窗口位置监控...")

        self._monitor_process = subprocess.Popen(
            [str(self._exe_path), "monitor"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )

        def _read_output():
            if self._monitor_process is None:
                return
            for line in self._monitor_process.stdout:
                try:
                    data = json.loads(line.strip())
                    for cb in self._window_callbacks:
                        try:
                            cb(data)
                        except Exception as e:
                            logger.debug(f"窗口回调异常: {e}")
                except json.JSONDecodeError:
                    pass  # 非 JSON 行，忽略

        if run_in_background:
            self._monitor_thread = threading.Thread(
                target=_read_output, daemon=True
            )
            self._monitor_thread.start()
        else:
            _read_output()

    def stop_window_monitor(self):
        """停止窗口位置监控"""
        if self._monitor_process:
            self._monitor_process.terminate()
            self._monitor_process = None
        logger.info("窗口监控已停止")

    def on_window_moved(self, callback: callable):
        """注册窗口移动回调（装饰器风格）"""
        self._window_callbacks.append(callback)
        return callback

    # ═══════════════════════════════════════════════════════
    # 内部方法
    # ═══════════════════════════════════════════════════════

    def _run(self, command: str, timeout: float = 15.0) -> Optional[str]:
        """
        调用 WeChatUIA.exe 并返回 stdout。

        Returns:
            stdout 字符串，失败返回 None
        """
        if not self.available:
            logger.error(f"WeChatUIA.exe 未找到: {self._exe_path}")
            return None

        try:
            result = subprocess.run(
                [str(self._exe_path), command],
                capture_output=True,
                text=True,
                timeout=timeout,
                creationflags=subprocess.CREATE_NO_WINDOW,  # 不弹控制台窗口
            )

            if result.returncode != 0:
                logger.error(
                    f"WeChatUIA {command} 失败 (exit={result.returncode}): "
                    f"{result.stderr.strip()}"
                )
                return None

            return result.stdout.strip()

        except subprocess.TimeoutExpired:
            logger.error(f"WeChatUIA {command} 超时 ({timeout}s)")
            return None
        except FileNotFoundError:
            logger.error(f".NET Runtime 未安装或 WeChatUIA.exe 缺失")
            return None
        except Exception as e:
            logger.error(f"WeChatUIA {command} 异常: {e}")
            return None


# ═══════════════════════════════════════════════════════════════
# 便捷函数：一行代码检测登录状态
# ═══════════════════════════════════════════════════════════════

def quick_login_check(uia: UIABridge = None) -> bool:
    """
    快速检查微信是否已登录。

    如果 C# 服务不可用，回退到 OCR 方式检测。
    """
    if uia and uia.available:
        result = uia.check_login()
        return result.get('isLoggedIn', False)

    # 回退：OCR 扫微信窗口，找"聊天"或"通讯录"标签
    try:
        from ..locator.ocr_locator import OCRLocator
        ocr = OCRLocator()
        nav_texts = ocr.get_all_text()
        has_nav = any(t in nav_texts for t in ['聊天', '通讯录', '朋友圈'])
        return has_nav
    except Exception:
        return True  # 无法检测时，假定已登录
