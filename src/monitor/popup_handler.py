"""
弹窗处理器 —— 操作前/操作中自动检测并清理阻断弹窗。

弹窗分类：
  阻断性弹窗（必须在任何操作前处理）：
    - 版本更新提示
    - 强制登出提示
    - 安全验证弹窗

  非阻断性弹窗（可在操作间隙处理）：
    - 新消息通知
    - 文件传输弹窗
    - 系统托盘通知

处理策略：
  1. 维护弹窗模板库（文字版，OCR 检测）
  2. 每步操作前快速巡检
  3. 阻断性弹窗立即处理，非阻断弹窗入队
  4. 弹窗处理记录日志供分析

Author: 版本无关微信自动化系统
"""

import time
import logging
from enum import Enum
from typing import Optional, List, Dict, Callable
from dataclasses import dataclass, field

import pyautogui

logger = logging.getLogger(__name__)


class PopupPriority(Enum):
    """弹窗优先级"""
    BLOCKING = 1     # 阻断性：必须立即处理
    HIGH = 2         # 高优先：可能影响操作
    NORMAL = 3       # 普通：可延后处理
    LOW = 4          # 低优先：忽略也可


@dataclass
class PopupDescriptor:
    """弹窗描述符"""
    name: str                             # 弹窗名称
    priority: PopupPriority               # 优先级
    ocr_signatures: List[str]             # OCR 文字特征（版本无关）
    dismiss_action: str = "escape"        # 关闭方式: 'escape' | 'click_text' | 'click_template'
    dismiss_target: str = ""              # 关闭目标（如 "稍后再说" / "确定" 的文字）
    timeout: float = 3.0                  # 处理超时


# ═══════════════════════════════════════════════════════════════
# 预定义弹窗（文字标签是版本无关的）
# ═══════════════════════════════════════════════════════════════

KNOWN_POPUPS = [
    PopupDescriptor(
        name="version_update",
        priority=PopupPriority.BLOCKING,
        ocr_signatures=["版本过低", "升级到最新版本"],
        dismiss_action="click_text",
        dismiss_target="稍后再说",
    ),
    PopupDescriptor(
        name="auto_update_ready",
        priority=PopupPriority.BLOCKING,
        ocr_signatures=["新版本已下载", "是否立即更新"],
        dismiss_action="click_text",
        dismiss_target="稍后提醒",
    ),
    PopupDescriptor(
        name="force_relogin",
        priority=PopupPriority.BLOCKING,
        ocr_signatures=["重新登录", "为了你的账号安全"],
        dismiss_action="stop",   # 无法自动关闭，需要通知用户
        dismiss_target="",
    ),
    PopupDescriptor(
        name="operation_too_frequent",
        priority=PopupPriority.BLOCKING,
        ocr_signatures=["操作太频繁", "请稍后再试"],
        dismiss_action="click_text",
        dismiss_target="确定",
    ),
    PopupDescriptor(
        name="new_message_notification",
        priority=PopupPriority.LOW,
        ocr_signatures=["发来一条消息"],
        dismiss_action="escape",
    ),
    PopupDescriptor(
        name="wechat_restart_needed",
        priority=PopupPriority.HIGH,
        ocr_signatures=["需要重新启动微信"],
        dismiss_action="click_text",
        dismiss_target="确定",
    ),
]


class PopupHandler:
    """
    弹窗处理器。

    使用方式：
        handler = PopupHandler(ocr_locator, router)
        handler.clear_blocking_popups()  # 操作前清理阻断弹窗
    """

    def __init__(self, ocr_locator, router):
        self._ocr = ocr_locator
        self._router = router
        self._popups = KNOWN_POPUPS.copy()
        self._history: List[Dict] = []

    # ── 公共接口 ──

    def clear_blocking_popups(self, region=None) -> List[str]:
        """
        清理所有阻断性弹窗。
        应该在每步操作前调用。

        Returns:
            已处理的弹窗名称列表
        """
        handled = []

        for popup in self._popups:
            if popup.priority != PopupPriority.BLOCKING:
                continue

            if self._detect_popup(popup, region=region):
                logger.warning(f"🚫 检测到阻断弹窗: {popup.name}")
                if self._dismiss_popup(popup, region=region):
                    handled.append(popup.name)
                    self._history.append({
                        'time': time.time(),
                        'popup': popup.name,
                        'action': 'dismissed',
                    })

        if handled:
            logger.info(f"已处理弹窗: {handled}")
        return handled

    def clear_all_popups(self) -> List[str]:
        """清理所有弹窗（阻断和非阻断）"""
        handled = self.clear_blocking_popups()

        for popup in self._popups:
            if popup.priority == PopupPriority.BLOCKING:
                continue
            if self._detect_popup(popup):
                if self._dismiss_popup(popup):
                    handled.append(popup.name)

        return handled

    def has_blocking_popup(self) -> bool:
        """检查是否有阻断性弹窗"""
        self._ocr._invalidate_cache()
        for popup in self._popups:
            if popup.priority == PopupPriority.BLOCKING:
                if self._detect_popup(popup):
                    return True
        return False

    def get_history(self) -> List[Dict]:
        """获取弹窗处理历史"""
        return self._history

    # ── 内部方法 ──

    def _detect_popup(self, popup: PopupDescriptor, region=None) -> bool:
        """检测弹窗是否存在"""
        for signature in popup.ocr_signatures:
            best = self._ocr.find_best(signature, region=region)
            if best:
                logger.debug(f"检测到弹窗特征 '{signature}' → {popup.name}")
                return True
        return False

    def _dismiss_popup(self, popup: PopupDescriptor, region=None) -> bool:
        """关闭弹窗"""
        if popup.dismiss_action == "stop":
            # 无法自动关闭，需要人工介入
            logger.critical(f"弹窗 {popup.name} 无法自动关闭，需要人工介入")
            return False

        if popup.dismiss_action == "escape":
            pyautogui.press('esc')
            time.sleep(0.3)
            return True

        if popup.dismiss_action == "click_text":
            if popup.dismiss_target:
                # 用 OCR 找到关闭按钮文字并点击
                best = self._ocr.find_best(popup.dismiss_target, region=region)
                if best:
                    pyautogui.click(best.x, best.y)
                    time.sleep(0.5)
                    logger.info(f"已关闭弹窗 {popup.name} (点击 '{popup.dismiss_target}')")
                    return True
                else:
                    # 找到弹窗但找不到关闭按钮
                    logger.warning(f"无法找到弹窗 {popup.name} 的关闭按钮 '{popup.dismiss_target}'")
                    # 尝试 ESC
                    pyautogui.press('esc')
                    time.sleep(0.3)
                    return False
            else:
                pyautogui.press('esc')
                return True

        return False
