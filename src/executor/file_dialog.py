"""
文件选择对话框自动化 —— 多种策略绕过/操控 Windows 文件对话框。

三种策略（按推荐度排序）：

  策略 A：剪贴板粘贴（最佳）
    - 将图片复制到剪贴板（CF_DIB / CF_HDROP 格式）
    - 直接在微信朋友圈页面 Ctrl+V 粘贴
    - 完全绕过文件选择对话框
    - 微信朋友圈原生支持图片粘贴

  策略 B：pywinauto 操控文件对话框
    - 用 pywinauto 定位"打开文件"对话框
    - 直接在文件名 Edit 控件中输入路径
    - 点击"打开"按钮
    - 适用于所有 Windows 标准文件对话框

  策略 C：SendKeys 模拟键盘输入
    - 用 pynput/keyboard 模拟键盘输入路径
    - 最后一个兜底方案
    - 需要对话框在前台、输入法为英文

使用方式：
    dialog = FileDialogHandler()
    dialog.paste_image_from_file("photo.jpg")       # 策略 A
    dialog.select_file_via_pywinauto("photo.jpg")   # 策略 B

Author: 版本无关微信自动化系统
"""

import logging
import time
import io
import random
from typing import Optional, List
from pathlib import Path

import pyautogui
import win32gui
from PIL import Image

logger = logging.getLogger(__name__)


class FileDialogHandler:
    """
    文件对话框处理器 —— 多策略自动化选择文件。

    策略选择逻辑：
      1. 如果微信朋友圈支持粘贴 → 策略 A（剪贴板）
      2. 如果弹出标准 Windows 文件对话框 → 策略 B（pywinauto）
      3. 如果上述都失败 → 策略 C（pynput SendKeys）
    """

    def __init__(self):
        self._pywinauto_available = self._check_pywinauto()
        self._pynput_available = self._check_pynput()

    def _check_pywinauto(self) -> bool:
        try:
            import pywinauto
            return True
        except ImportError:
            logger.debug("pywinauto 未安装，策略 B 不可用")
            return False

    def _check_pynput(self) -> bool:
        try:
            import pynput
            return True
        except ImportError:
            logger.debug("pynput 未安装，策略 C 不可用")
            return False

    # ══════════════════════════════════════════════════════════
    # 策略 A：剪贴板粘贴（最佳方案）
    # ══════════════════════════════════════════════════════════

    def paste_image_from_file(self, image_path: str) -> bool:
        """
        将图片文件通过剪贴板粘贴到微信朋友圈。

        微信朋友圈原生支持 Ctrl+V 粘贴图片，完全不需要打开文件对话框。
        这是最简洁、最稳定的方案。

        原理：
          1. 用 PIL 读取图片
          2. 转换为 DIB 格式写入剪贴板（CF_DIB）
          3. Ctrl+V 粘贴

        Args:
            image_path: 图片文件路径

        Returns:
            True 表示粘贴成功
        """
        image_path = Path(image_path)
        if not image_path.exists():
            logger.error(f"图片不存在: {image_path}")
            return False

        try:
            img = Image.open(image_path)
            self._set_clipboard_image(img)
            time.sleep(0.2)

            # Ctrl+V 粘贴
            pyautogui.hotkey('ctrl', 'v')
            time.sleep(0.3)

            logger.info(f"图片已粘贴: {image_path.name}")
            return True

        except Exception as e:
            logger.error(f"图片粘贴失败: {e}")
            return False

    def paste_images_from_files(self, image_paths: List[str],
                                delay_between: float = 2.0) -> int:
        """
        批量粘贴多张图片。

        微信朋友圈支持一次粘贴多张图片（连续 Ctrl+V），
        但需要注意微信对图片数量有上限（通常 9 张）。
        """
        success_count = 0
        for i, path in enumerate(image_paths[:9]):  # 最多 9 张
            if self.paste_image_from_file(path):
                success_count += 1
                if i < len(image_paths) - 1:
                    # 图片之间需要等待上传
                    time.sleep(delay_between)
            else:
                logger.warning(f"第 {i+1} 张图片粘贴失败")
        return success_count

    @staticmethod
    def _set_clipboard_image(img: Image.Image):
        """
        将 PIL Image 以 DIB（Device Independent Bitmap）格式写入剪贴板。

        Windows 剪贴板的 CF_DIB 格式要求：
          - BITMAPINFOHEADER (40 bytes) + 像素数据 (BGR, bottom-up)
          - 数据前 14 bytes 的 BITMAPFILEHEADER 需要去掉
        """
        import win32clipboard
        import struct

        img = img.convert('RGB')
        width, height = img.size
        pixels = img.tobytes()

        # BITMAPINFOHEADER: biSize(4) + biWidth(4) + biHeight(4) + ...
        bmi_header = struct.pack(
            '<IiiHHIIiiII',
            40,        # biSize
            width,     # biWidth
            height,    # biHeight (正值表示 bottom-up)
            1,         # biPlanes
            24,        # biBitCount (24位)
            0,         # biCompression (BI_RGB)
            len(pixels),  # biSizeImage
            0, 0, 0, 0  # 其余字段
        )

        win32clipboard.OpenClipboard()
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(
            win32clipboard.CF_DIB,
            bmi_header + pixels
        )
        win32clipboard.CloseClipboard()

    # ══════════════════════════════════════════════════════════
    # 策略 A-2：CF_HDROP 剪切板粘贴（直接粘贴文件路径）
    # ══════════════════════════════════════════════════════════

    def paste_file_paths(self, file_paths: List[str]) -> bool:
        """
        通过 CF_HDROP 格式将文件路径写入剪贴板，然后粘贴。

        这种方式的优势：微信朋友圈的图片对话框支持直接粘贴文件，
        粘贴后微信自动处理上传。不需要打开文件选择对话框。

        CF_HDROP 格式结构：
          DROPFILES struct + file_paths (null-separated) + null terminator

        Args:
            file_paths: 文件路径列表（绝对路径）

        Returns:
            True 表示粘贴成功
        """
        import win32clipboard
        import struct

        # DROPFILES 结构体
        dropfiles = struct.pack(
            '<IIiiI',
            20,             # pFiles: 结构体偏移
            0, 0,           # pt (x, y) — 0 为默认
            0,              # fNC: 非客户端区域
            1,              # fWide: Unicode 路径
        )

        # 文件路径（null-separated Unicode）
        paths_wide = '\x00'.join(str(Path(p).resolve()) for p in file_paths)
        paths_wide += '\x00\x00'  # 双 null 终止

        data = dropfiles + paths_wide.encode('utf-16-le')

        win32clipboard.OpenClipboard()
        win32clipboard.EmptyClipboard()
        # CF_HDROP = 15
        win32clipboard.SetClipboardData(15, data)
        win32clipboard.CloseClipboard()

        time.sleep(0.2)
        pyautogui.hotkey('ctrl', 'v')

        return True

    # ══════════════════════════════════════════════════════════
    # 策略 B：pywinauto 操控文件对话框
    # ══════════════════════════════════════════════════════════

    @staticmethod
    def _foreground_file_dialog() -> Optional[int]:
        """Return the foreground standard file dialog without relying on localized titles."""
        hwnd = win32gui.GetForegroundWindow()
        if hwnd and win32gui.GetClassName(hwnd) == '#32770':
            return hwnd
        return None

    def select_file_via_pywinauto(self, file_path: str,
                                  dialog_title: str = "打开",
                                  timeout: float = 10.0) -> bool:
        """
        用 pywinauto 操控 Windows 标准文件选择对话框。

        找到"打开文件"对话框 → 在文件名输入框填入路径 → 点击"打开"。

        这比 SendKeys 更可靠，因为 pywinauto 直接操作 Edit 控件，
        不依赖于当前输入法和键盘状态。

        Args:
            file_path: 要选择的文件完整路径
            dialog_title: 对话框标题（中文微信可能是"打开"或"选择文件"）
            timeout: 等待对话框出现的超时时间

        Returns:
            True 表示文件选择成功
        """
        if not self._pywinauto_available:
            logger.warning("pywinauto 不可用，回退到策略 C")
            return self.select_file_via_sendkeys(file_path)

        from pywinauto import Desktop

        file_path = str(Path(file_path).resolve())
        logger.info(f"pywinauto: 选择文件 {Path(file_path).name}")

        try:
            # Windows 4.x 微信可能返回乱码标题，使用标准对话框类和 UIA ID。
            start_time = time.time()
            dlg = None

            while time.time() - start_time < timeout:
                hwnd = self._foreground_file_dialog()
                if hwnd:
                    try:
                        dlg = Desktop(backend="uia").window(handle=hwnd)
                        if dlg.exists():
                            break
                    except Exception:
                        dlg = None
                time.sleep(0.3)

            if dlg is None:
                logger.error(
                    f"未找到前台标准文件对话框 (期望标题='{dialog_title}')"
                )
                return False

            # 标准文件对话框中，1148 是文件名输入框，1 是确认按钮。
            edit_control = dlg.child_window(auto_id="1148", control_type="Edit")
            open_button = dlg.child_window(auto_id="1", control_type="Button")
            if edit_control.exists() and open_button.exists():
                edit_control.set_edit_text(file_path)
                time.sleep(0.3)
                open_button.click()
                logger.info("文件对话框已确认")
                return True

            # 兼容旧版对话框：回退到名称和控件类型。
            edit_control = None
            for child in dlg.descendants(control_type="Edit"):
                if child.is_enabled() and child.element_info.automation_id != "SearchEditBox":
                    edit_control = child
                    if child.element_info.automation_id == "1148":
                        break

            if edit_control:
                edit_control.set_edit_text('')
                edit_control.type_keys(file_path, with_spaces=True)
                time.sleep(0.3)

                # 点击"打开"按钮
                for btn_name in ['打开(O)', '打开', 'Open', '确定']:
                    try:
                        dlg[btn_name].click()
                        logger.info("文件对话框已确认")
                        return True
                    except Exception:
                        continue

            logger.error("无法操作文件对话框控件")
            return False

        except Exception as e:
            logger.error(f"pywinauto 文件选择失败: {e}")
            return False

    # ══════════════════════════════════════════════════════════
    # 策略 C：pynput SendKeys 模拟键盘输入（兜底）
    # ══════════════════════════════════════════════════════════

    def select_file_via_sendkeys(self, file_path: str) -> bool:
        """
        用 pynput 模拟键盘输入文件路径（最后的兜底方案）。

        前提条件：
          - 文件对话框已弹出且在前台
          - 焦点在文件名输入框中
          - 输入法为英文状态（按 Shift 切换）
        """
        if not self._pynput_available:
            logger.error("pynput 不可用，无法使用策略 C")
            return False

        from pynput.keyboard import Key, Controller

        file_path = str(Path(file_path).resolve())
        keyboard = Controller()

        # 切换到英文输入法
        keyboard.press(Key.shift)
        time.sleep(0.05)
        keyboard.release(Key.shift)
        time.sleep(0.1)

        # 确保焦点在文件名输入框（Alt+N 通常可以）
        with keyboard.pressed(Key.alt):
            keyboard.press('n')
            keyboard.release('n')
        time.sleep(0.2)

        # 输入文件路径
        for char in file_path:
            keyboard.press(char)
            keyboard.release(char)
            time.sleep(random.uniform(0.01, 0.03))

        time.sleep(0.3)

        # 按回车确认
        keyboard.press(Key.enter)
        keyboard.release(Key.enter)

        logger.info(f"SendKeys 输入完成: {Path(file_path).name}")
        return True

    # ══════════════════════════════════════════════════════════
    # 统一接口：自动选择最佳策略
    # ══════════════════════════════════════════════════════════

    def add_images_to_moments(self, image_paths: List[str],
                              prefer_paste: bool = True) -> int:
        """
        将图片添加到朋友圈（自动选择最佳策略）。

        策略优先级：
          1. 剪贴板 DIB 粘贴（CTRL+V）—— 最快，绕过文件对话框
          2. 如果微信不响应粘贴，点击"相册"按钮 → 用 pywinauto 选文件
          3. 如果 pywinauto 不可用 → SendKeys 键盘模拟

        Args:
            image_paths: 图片路径列表
            prefer_paste: 优先使用粘贴策略

        Returns:
            成功添加的图片数量
        """
        if prefer_paste:
            # 直接粘贴，不需要打开文件对话框
            return self.paste_images_from_files(image_paths)
        else:
            # 需要点击"相册"按钮打开文件对话框，然后用 pywinauto 选文件
            success = 0
            for i, path in enumerate(image_paths):
                # 点击"相册"按钮（由调用方负责）
                time.sleep(0.5)  # 等对话框弹出

                if self._pywinauto_available:
                    ok = self.select_file_via_pywinauto(path)
                else:
                    ok = self.select_file_via_sendkeys(path)

                if ok:
                    success += 1
                time.sleep(1.0)  # 等文件加载
            return success


# ═══════════════════════════════════════════════════════════════
# 补充：CF_BITMAP 方式（通过 HBITMAP 粘贴）
# ═══════════════════════════════════════════════════════════════

def paste_image_via_cf_bitmap(image_path: str) -> bool:
    """
    通过 CF_BITMAP 格式粘贴图片（备选，格式兼容性最佳）。

    与 CF_DIB 不同，CF_BITMAP 是 GDI 位图句柄，
    某些老版本 Windows 应用只识别 CF_BITMAP。
    """
    import win32clipboard
    import win32ui
    import win32gui
    import win32con
    from PIL import Image

    img = Image.open(image_path)
    img = img.convert('RGB')
    width, height = img.size

    # 创建 GDI 位图
    hdc = win32gui.GetDC(0)
    mem_dc = win32ui.CreateDCFromHandle(win32gui.CreateCompatibleDC(hdc))
    bitmap = win32ui.CreateBitmap()
    bitmap.CreateCompatibleBitmap(win32ui.CreateDCFromHandle(hdc), width, height)

    mem_dc.SelectObject(bitmap)

    # PIL → GDI 像素数据
    pixels = img.tobytes('raw', 'BGRX')
    bitmap.SetBitmapBits(pixels)

    win32clipboard.OpenClipboard()
    win32clipboard.EmptyClipboard()
    win32clipboard.SetClipboardData(win32con.CF_BITMAP, bitmap.GetHandle())
    win32clipboard.CloseClipboard()

    time.sleep(0.2)
    pyautogui.hotkey('ctrl', 'v')

    return True
