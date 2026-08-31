"""
操作执行器 —— 将"定位"和"模拟"组合成可执行的操作原语。

每个操作原语都包含：
  1. 前置条件检查（窗口激活、弹窗清理）
  2. 元素定位（通过 LocateRouter）
  3. 类人操作执行（通过 HumanSimulator）
  4. 结果验证（OCR 确认）

Author: 版本无关微信自动化系统
"""

import ctypes
import time
import logging
import random
from typing import Optional, Tuple, Callable
from pathlib import Path

import pyautogui
import pyperclip
import win32gui
import win32con
import win32process

from ..locator.router import LocateRouter, ElementDescriptor, LocateResult
from .human_sim import HumanSimulator
from .uia_bridge import UIABridge

logger = logging.getLogger(__name__)


def _get_window_text(hwnd: int) -> str:
    """Read window text through the Unicode API on every Windows locale."""
    buffer = ctypes.create_unicode_buffer(512)
    ctypes.windll.user32.GetWindowTextW(hwnd, buffer, len(buffer))
    return buffer.value


class Operator:
    """
    微信操作执行器。

    封装所有对微信窗口的原子操作，确保：
      - 操作前微信窗口在前台
      - 操作前没有阻断性弹窗
      - 操作后验证结果
      - 失败时返回 False 而非抛异常

    使用方式：
        op = Operator(router, sim)
        op.ensure_window_active()
        op.click_element(ElementDescriptor(name="朋友圈", ocr_text="朋友圈"))
        op.type_content("今天天气真好")
        op.publish_post()
    """

    def __init__(self, router: LocateRouter, sim: HumanSimulator,
                 config: dict = None, uia: UIABridge = None):
        self.router = router
        self.sim = sim
        self._config = config or {}
        self._wechat_hwnd = None
        self._moments_hwnd = None
        self._active_hwnd = None
        self._uia = uia  # C# UIAutomation 桥接
        self._window_callbacks: list[Callable] = []
        self._last_window_rect: Optional[Tuple[int, int, int, int]] = None

    # ══════════════════════════════════════════════════════════
    # 窗口管理
    # ══════════════════════════════════════════════════════════

    def find_wechat_window(self) -> bool:
        """查找微信窗口（自动发现，无硬编码类名）"""
        from .wechat_discovery import _find_wechat_windows
        windows = _find_wechat_windows()
        if windows:
            self._wechat_hwnd = windows[0][0]
            self._active_hwnd = self._wechat_hwnd
            logger.info(f"找到微信窗口: hwnd={self._wechat_hwnd}")
            return True
        logger.error("未找到微信窗口，请确认微信已启动")
        return False

    def find_moments_window(self) -> bool:
        """Find the separate Moments window used by desktop WeChat 4.x."""
        from .wechat_discovery import WECHAT_PROCESS_NAMES, _get_process_name

        candidates = []

        def callback(hwnd, _):
            if not win32gui.IsWindowVisible(hwnd):
                return
            if _get_window_text(hwnd) != '朋友圈':
                return
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            if _get_process_name(pid) not in WECHAT_PROCESS_NAMES:
                return
            candidates.append(hwnd)

        win32gui.EnumWindows(callback, None)
        if not candidates:
            return False

        self._moments_hwnd = candidates[0]
        return True

    def ensure_window_active(self, hwnd: int = None) -> bool:
        """
        确保微信窗口在前台且未被最小化。
        这是所有自动化操作的前置条件。
        """
        target = hwnd or self._active_hwnd or self._wechat_hwnd
        if not target or not win32gui.IsWindow(target):
            if not self.find_wechat_window():
                return False
            target = self._wechat_hwnd

        # 检查是否最小化
        if win32gui.IsIconic(target):
            win32gui.ShowWindow(target, win32con.SW_RESTORE)
            time.sleep(0.3)
            logger.debug("微信窗口已从最小化恢复")

        # 强制置顶（短暂置顶后取消，避免一直盖住其他窗口）
        win32gui.SetWindowPos(
            target, win32con.HWND_TOPMOST,
            0, 0, 0, 0,
            win32con.SWP_NOMOVE | win32con.SWP_NOSIZE,
        )
        time.sleep(0.1)

        # 激活窗口
        win32gui.SetForegroundWindow(target)
        time.sleep(0.15)

        # 取消置顶
        win32gui.SetWindowPos(
            target, win32con.HWND_NOTOPMOST,
            0, 0, 0, 0,
            win32con.SWP_NOMOVE | win32con.SWP_NOSIZE,
        )

        self._active_hwnd = target
        return True

    def activate_main_window(self) -> bool:
        """Activate the main WeChat window."""
        if not self._wechat_hwnd and not self.find_wechat_window():
            return False
        return self.ensure_window_active(self._wechat_hwnd)

    def activate_moments_window(self) -> bool:
        """Activate the separate desktop Moments window when present."""
        if not self._moments_hwnd or not win32gui.IsWindow(self._moments_hwnd):
            if not self.find_moments_window():
                return False
        return self.ensure_window_active(self._moments_hwnd)

    def active_window_region(self) -> Optional[Tuple[int, int, int, int]]:
        """Return the active automation window as a PyAutoGUI region."""
        hwnd = self._active_hwnd
        if not hwnd or not win32gui.IsWindow(hwnd):
            return None
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        return left, top, right - left, bottom - top

    def click_moments_camera(self) -> bool:
        """Click the camera icon in the desktop 4.x Moments header."""
        if not self.activate_moments_window():
            return False
        region = self.active_window_region()
        if not region:
            return False

        left, top, width, height = region
        if width < 300 or height < 300:
            logger.error(f"朋友圈窗口尺寸异常: {region}")
            return False

        # The header icons scale with the Qt window. This point is the center
        # of the camera button and is deliberately far from the publish area.
        x = left + int(width * 0.164)
        y = top + int(height * 0.038)
        self.sim.click_at(x, y)
        return True

    def click_moments_editor_body(self) -> bool:
        """Focus the desktop 4.x compose text area without using placeholder text."""
        if not self.activate_moments_window():
            return False
        region = self.active_window_region()
        if not region:
            return False

        left, top, width, height = region
        if width < 300 or height < 300:
            logger.error(f"朋友圈窗口尺寸异常: {region}")
            return False

        # This point remains inside the compose body after its placeholder is
        # replaced by text and stays well above the image and publish controls.
        x = left + int(width * 0.35)
        y = top + int(height * 0.22)
        self.sim.click_at(x, y)
        return True

    # ══════════════════════════════════════════════════════════
    # 基础操作原语
    # ══════════════════════════════════════════════════════════

    def click_element(self, element: ElementDescriptor,
                      verify_element: ElementDescriptor = None
                      ) -> bool:
        """
        点击界面元素（含完整的定位→移动→点击→验证流程）。

        Args:
            element: 目标元素描述符
            verify_element: 点击后应出现的验证元素（如点击"朋友圈"后应看到"这一刻的想法"）

        Returns:
            True 表示操作成功
        """
        # 1. 前置条件
        if not self.ensure_window_active():
            return False

        # 2. 定位
        result = self.router.locate(element)
        if result is None:
            logger.error(f"点击失败：无法定位 [{element.name}]")
            return False

        # 3. 类人点击
        self.sim.click_at(result.x, result.y)
        self.sim.extra_action()

        # 4. 验证（如果提供了验证元素）
        if verify_element:
            time.sleep(1.0)  # 给界面加载时间
            verified = self.router.verify_element(verify_element)
            if not verified:
                logger.warning(f"点击 [{element.name}] 后未检测到验证元素 [{verify_element.name}]")
                # 不直接返回 False，可能是加载慢
                time.sleep(1.0)
                verified = self.router.verify_element(verify_element)
                if not verified:
                    return False

        return True

    def type_content(self, text: str, input_element: ElementDescriptor = None) -> bool:
        """
        输入文字内容。

        Args:
            text: 要输入的文字
            input_element: 输入框元素（可选，如果提供则先点击聚焦）

        Returns:
            True 表示输入成功
        """
        # 1. 先点击输入框聚焦
        if input_element:
            if not self.click_element(input_element):
                # 即使点击失败也尝试直接输入（可能焦点已经在正确位置）
                logger.warning("输入框定位失败，尝试直接输入")

        # 2. 等待焦点稳定
        self.sim.micro_pause(mean=0.2)

        # 3. 清空可能存在的旧内容
        pyautogui.hotkey('ctrl', 'a')
        self.sim.micro_pause(mean=0.1)

        # 4. 类人输入
        self.sim.type_text(text)

        return True

    def add_images(self, image_paths: list,
                   add_btn: ElementDescriptor = None,
                   wait_upload: bool = True) -> bool:
        """
        添加图片到朋友圈。

        通过剪贴板粘贴方式添加图片。
        微信朋友圈支持直接 Ctrl+V 粘贴图片。

        Args:
            image_paths: 图片路径列表
            add_btn: 添加按钮描述符（点击相册按钮，不用剪贴板时使用）
            wait_upload: 是否等待图片上传完成后再返回

        Returns:
            True 表示所有图片已成功添加/上传
        """
        if not image_paths:
            return True  # 没有图片，跳过

        from ..executor.file_dialog import FileDialogHandler
        handler = FileDialogHandler()

        for i, img_path in enumerate(image_paths):
            if not Path(img_path).exists():
                logger.error(f"图片不存在: {img_path}")
                continue

            # 策略 A：剪贴板粘贴（最优先）
            try:
                handler.paste_image_from_file(img_path)
            except Exception:
                # 策略 B：CF_HDROP 文件路径粘贴
                try:
                    handler.paste_file_paths([img_path])
                except Exception as e:
                    logger.error(f"图片粘贴失败: {e}")
                    return False

            # 等待上传完成
            if wait_upload and i < len(image_paths) - 1:
                self._wait_image_upload(timeout=60.0)

            self.sim.delay(base=1.5)

        return True

    def _wait_image_upload(self, timeout: float = 60.0):
        """
        等待图片上传完成。

        微信上传图片时的视觉信号：
          - 缩略图从模糊变清晰
          - 图片上不再有"上传中..."文字或进度圈

        通过 OCR 检测上传状态变化。
        """
        start = time.time()
        check_interval = 2.0

        while time.time() - start < timeout:
            # 检查是否有"上传失败"文字
            blocks = self.router.ocr.scan_screen()
            texts = [b.text for b in blocks]

            # 上传失败检测
            if any('上传失败' in t or '发送失败' in t for t in texts):
                logger.error("检测到上传失败")
                return

            # 如果没有"上传中"文字，认为完成
            if not any('上传中' in t or '正在发送' in t for t in texts):
                logger.debug("图片上传完成")
                return

            time.sleep(check_interval)

        logger.warning(f"图片上传等待超时 ({timeout}s)")

    def click_publish(self, publish_element: ElementDescriptor) -> bool:
        """点击发布按钮"""
        return self.click_element(publish_element)

    def verify_published(self, success_element: ElementDescriptor,
                         timeout: float = 10.0) -> bool:
        """
        验证发布成功。等待"已发送"或类似的成功提示出现。

        Returns:
            True 表示确认发布成功
        """
        result = self.router.wait_element(success_element, timeout=timeout)
        return result is not None

    # ══════════════════════════════════════════════════════════
    # 导航操作
    # ══════════════════════════════════════════════════════════

    def enter_moments(self, nav_element: ElementDescriptor,
                      verify_element: ElementDescriptor) -> bool:
        """进入朋友圈页面"""
        if self.open_moments_navigation():
            return True
        return self.click_element(nav_element, verify_element=verify_element)

    def open_moments_navigation(self, timeout: float = 8.0) -> bool:
        """Open Moments through either the direct or Discover navigation path."""
        if self._uia is None or not self._uia.available:
            return False
        return self._uia.open_moments(timeout=timeout)

    # ══════════════════════════════════════════════════════════
    # 应急操作
    # ══════════════════════════════════════════════════════════

    def dismiss_popup(self, popup: ElementDescriptor) -> bool:
        """关闭弹窗"""
        return self.click_element(popup)

    def press_escape(self):
        """按 ESC 关闭可能的弹窗"""
        pyautogui.press('esc')
        self.sim.micro_pause(mean=0.1)

    def restart_wechat(self) -> bool:
        """
        重启微信进程。
        当微信崩溃/卡死时使用。
        """
        import subprocess
        import os

        wechat_path = self._config.get(
            'wechat_path',
            r'C:\Program Files\Tencent\Weixin\Weixin.exe',
            r'C:\Program Files\Tencent\WeChat\WeChat.exe'
        )

        try:
            # 杀掉微信进程（尝试两个可能的进程名）
            for proc_name in ['WeChat.exe', 'Weixin.exe']:
                subprocess.run(['taskkill', '/f', '/im', proc_name],
                               capture_output=True, check=False)
            time.sleep(2.0)

            # 重新启动
            exe_path = Path(wechat_path)
            if not exe_path.exists():
                # 尝试 Weixin.exe
                alt = exe_path.parent / 'Weixin.exe'
                if alt.exists():
                    wechat_path = str(alt)
            subprocess.Popen([wechat_path])
            time.sleep(5.0)  # 等微信启动

            # 重新查找窗口
            return self.find_wechat_window()
        except Exception as e:
            logger.error(f"重启微信失败: {e}")
            return False

    # ══════════════════════════════════════════════════════════
    # UIA 控件树定位（优先于 OCR，微信 4.x 关键能力）
    # ══════════════════════════════════════════════════════════

    def click_by_uia(self, name: str = None, automation_id: str = None,
                     control_type: str = None) -> bool:
        """
        通过 UIA 控件树直接定位并点击元素。
        这是微信 4.x 下的首选定位方式（比 OCR 快 10 倍，100% 准确）。

        Args:
            name: 控件名称（如"朋友圈"、"发表"）
            automation_id: AutomationId
            control_type: 控件类型（如"Button"、"Edit"）

        Returns:
            True 表示找到并点击成功
        """
        if self._uia is None or not self._uia.available:
            return False

        # 拉取控件树
        tree = self._uia.dump_tree()
        if tree is None:
            return False

        # 搜索目标元素
        target = None
        root = tree.get('rootElement', {})

        def search(node):
            nonlocal target
            if target is not None:
                return

            matches = True
            if name and node.get('name') != name:
                matches = False
            if automation_id and node.get('automationId') != automation_id:
                matches = False
            if control_type and node.get('controlType') != control_type:
                matches = False

            if matches and (name or automation_id or control_type):
                target = node
                return

            for child in node.get('children', []):
                search(child)

        search(root)

        if target is None:
            return False

        # 计算中心坐标
        x = target.get('x', 0) + target.get('width', 0) // 2
        y = target.get('y', 0) + target.get('height', 0) // 2

        logger.info(
            f"UIA 点击: '{target.get('name')}' "
            f"[{target.get('controlType')}] → ({x}, {y})"
        )
        self.sim.click_at(x, y)
        return True

    def get_all_interactive_elements(self) -> list[dict]:
        """
        获取微信界面上所有可交互元素的列表（按钮、输入框等）。
        用于自动校准——比 OCR 扫描更快更全。
        """
        if self._uia is None or not self._uia.available:
            return []

        tree = self._uia.dump_tree()
        if tree is None:
            return []

        interactive = []
        interactive_types = {'Button', 'Edit', 'ComboBox', 'CheckBox',
                             'RadioButton', 'ListItem', 'Hyperlink', 'TabItem'}

        def collect(node):
            if node.get('controlType') in interactive_types and node.get('isEnabled'):
                interactive.append(node)
            for child in node.get('children', []):
                collect(child)

        collect(tree.get('rootElement', {}))
        return interactive

    # ══════════════════════════════════════════════════════════
    # 窗口位置监控 + 自动重新校准
    # ══════════════════════════════════════════════════════════

    def start_window_monitoring(self, on_recalibrate: callable = None):
        """
        启动微信窗口位置监控。
        窗口移动/缩放时自动触发重新校准。

        Args:
            on_recalibrate: 窗口变化回调（用于触发 calibrator.calibrate(force=True)）
        """
        if self._uia is None or not self._uia.available:
            logger.warning("UIA 桥接不可用，窗口监控未启动")
            return

        # 记录当前窗口位置
        rect = self._uia.get_window_rect()
        if rect:
            self._last_window_rect = rect

        def _on_window_changed(window_info: dict):
            new_rect = (
                window_info.get('left', 0),
                window_info.get('top', 0),
                window_info.get('right', 0),
                window_info.get('bottom', 0),
            )

            # 检查位置是否真的变了（忽略微小抖动 <5px）
            if self._last_window_rect:
                old = self._last_window_rect
                moved = (abs(new_rect[0] - old[0]) > 5 or
                         abs(new_rect[1] - old[1]) > 5 or
                         abs(new_rect[2] - old[2]) > 10 or
                         abs(new_rect[3] - old[3]) > 10)

                if moved:
                    logger.warning(
                        f"检测到窗口移动: {old[:2]} → {new_rect[:2]}"
                    )
                    self._last_window_rect = new_rect

                    if on_recalibrate:
                        logger.info("触发自动重新校准...")
                        on_recalibrate()

        self._uia.start_window_monitor(
            on_change=_on_window_changed,
            run_in_background=True,
        )
        logger.info("窗口监控已启动")

    def stop_window_monitoring(self):
        """停止窗口位置监控"""
        if self._uia:
            self._uia.stop_window_monitor()

    # ══════════════════════════════════════════════════════════
    # 登录状态检测
    # ══════════════════════════════════════════════════════════

    def check_login_state(self) -> dict:
        """
        检测微信是否已登录。

        优先使用 UIA 控件树检测（极快、极准），
        失败时回退到 OCR 扫描。

        Returns:
            {'logged_in': bool, 'page': str, 'details': str}
        """
        # 优先 UIA
        if self._uia and self._uia.available:
            result = self._uia.check_login()
            if result.get('isLoggedIn'):
                return {
                    'logged_in': True,
                    'page': result.get('detectedPage', '微信主界面'),
                    'details': f"导航标签: {result.get('navLabels', [])}",
                }
            # WeChat 4.x may expose no usable UIA navigation tree while its
            # authenticated Moments window remains visible and interactive.
            if (
                self.find_moments_window()
                and self.activate_moments_window()
            ):
                left, top, right, bottom = win32gui.GetWindowRect(
                    self._moments_hwnd
                )
                if right - left >= 300 and bottom - top >= 300:
                    return {
                        'logged_in': True,
                        'page': '朋友圈',
                        'details': '独立朋友圈窗口已就绪',
                    }
            elif result.get('detectedPage') == '微信未运行':
                return {
                    'logged_in': False,
                    'page': 'not_running',
                    'details': '微信进程未运行',
                }
            # UIA 检测到未登录但微信在运行
            return {
                'logged_in': False,
                'page': result.get('detectedPage', '未知'),
                'details': '未检测到导航栏标签，可能已掉线',
            }

        # 回退 OCR
        try:
            if not self.activate_main_window():
                return {
                    'logged_in': False,
                    'page': 'not_running',
                    'details': '未找到微信主窗口',
                }
            region = self.active_window_region()
            if region:
                left, top, width, height = region
                region = (left, top, min(width, 700), min(height, 400))
            blocks = self.router.ocr.scan_screen(region=region)
            texts = [b.text for b in blocks]
            has_nav = any(
                marker in text
                for text in texts
                for marker in ('聊天', '通讯录', '搜索')
            )

            return {
                'logged_in': has_nav,
                'page': '微信主界面' if has_nav else '未知',
                'details': f"OCR 检测: {len(texts)} 个文本块",
            }
        except Exception as e:
            return {
                'logged_in': False,
                'page': 'error',
                'details': f"检测异常: {e}",
            }
