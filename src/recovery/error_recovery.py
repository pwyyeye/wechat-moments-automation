"""
异常恢复系统 —— 多层次错误检测与自动恢复。

恢复层级：
  第 1 层：操作重试（同一步骤内重试 1-3 次）
  第 2 层：策略降级（OCR 失败 → 特征匹配 → 锚点推算）
  第 3 层：窗口恢复（激活窗口 → 取消最小化 → 杀掉遮挡弹窗）
  第 4 层：进程恢复（杀死微信 → 重新启动 → 恢复窗口位置）
  第 5 层：放弃告警（记录详细日志 + 截图 + 通知用户）

恢复策略选择：
  ┌──────────────┬────────────────────────────────┐
  │ 异常类型     │ 恢复策略                        │
  ├──────────────┼────────────────────────────────┤
  │ 定位失败     │ 降级定位策略 + 刷新 OCR 缓存    │
  │ 点击无响应   │ 激活窗口 + 扩大搜索区域         │
  │ 弹窗阻断     │ 弹窗处理 + 标记 + 重试          │
  │ 窗口被遮挡   │ 置顶窗口 + 重试                 │
  │ 微信无响应   │ 重启微信进程                    │
  │ 连续 3+ 失败 │ 指数退避冷却 + 通知             │
  └──────────────┴────────────────────────────────┘

Author: 版本无关微信自动化系统
"""

import time
import logging
import traceback
from enum import Enum
from typing import Optional, Callable, Dict, Any
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


class ErrorType(Enum):
    """异常类型"""
    LOCATE_FAILED = "locate_failed"           # 定位失败
    CLICK_NO_RESPONSE = "click_no_response"   # 点击后无响应
    POPUP_BLOCKED = "popup_blocked"           # 弹窗阻断
    WINDOW_OCCLUDED = "window_occluded"       # 窗口被遮挡
    WECHAT_CRASHED = "wechat_crashed"         # 微信崩溃
    TIMEOUT = "timeout"                       # 操作超时
    UNKNOWN = "unknown"                       # 未知异常


@dataclass
class RecoveryAction:
    """恢复动作"""
    name: str
    handler: Callable[[], bool]
    cooldown_after: float = 0.0
    max_attempts: int = 3


@dataclass
class FailureRecord:
    """失败记录"""
    error_type: ErrorType
    step: str
    message: str
    timestamp: float = field(default_factory=time.time)
    attempt: int = 1
    recovered: bool = False


class ErrorRecovery:
    """
    异常恢复管理器。

    使用方式：
        recovery = ErrorRecovery(operator)

        def risky_operation():
            # 可能失败的操作
            return operator.click_element(some_button)

        success = recovery.with_recovery(
            risky_operation,
            step_name="点击朋友圈按钮",
            error_type=ErrorType.LOCATE_FAILED,
        )
    """

    def __init__(self, operator, popup_handler=None, risk_detector=None):
        self._operator = operator
        self._popup_handler = popup_handler
        self._risk_detector = risk_detector
        self._failure_history: list = []
        self._consecutive_failures = 0

    # ── 公共接口 ──

    def with_recovery(self, operation: Callable[[], bool],
                      step_name: str = "",
                      error_type: ErrorType = ErrorType.UNKNOWN,
                      max_attempts: int = 3) -> bool:
        """
        带自动恢复的操作执行。

        Args:
            operation: 要执行的操作（返回 True/False）
            step_name: 步骤名称（日志用）
            error_type: 异常类型（决定恢复策略）
            max_attempts: 最大尝试次数

        Returns:
            True 表示操作成功
        """
        for attempt in range(1, max_attempts + 1):
            try:
                # ── 执行前检查 ──
                if self._popup_handler:
                    self._popup_handler.clear_blocking_popups()

                if self._risk_detector:
                    if self._risk_detector.check().value >= 3:  # DANGER+
                        logger.critical("风控等级过高，中止操作")
                        return False

                # ── 执行操作 ──
                success = operation()

                if success:
                    self._consecutive_failures = 0
                    return True

                # ── 操作返回 False，进入恢复 ──
                logger.warning(f"[{step_name}] 第 {attempt}/{max_attempts} 次失败")

                self._record_failure(error_type, step_name,
                                     "操作返回 False", attempt)

                if attempt < max_attempts:
                    self._recover(error_type, attempt)

            except Exception as e:
                logger.error(f"[{step_name}] 异常: {e}")
                logger.debug(traceback.format_exc())

                self._record_failure(error_type, step_name,
                                     str(e), attempt)

                if attempt < max_attempts:
                    self._recover(error_type, attempt)

        # 全部重试失败
        self._consecutive_failures += 1
        logger.error(f"[{step_name}] 重试 {max_attempts} 次全部失败")
        return False

    # ── 恢复策略 ──

    def _recover(self, error_type: ErrorType, attempt: int):
        """根据错误类型执行恢复动作"""
        delay = 0.5 * attempt

        if error_type == ErrorType.LOCATE_FAILED:
            # 策略：刷新 OCR 缓存 + 等待界面变化
            if hasattr(self._operator, 'router'):
                self._operator.router.ocr._invalidate_cache()
            time.sleep(delay)

        elif error_type == ErrorType.CLICK_NO_RESPONSE:
            # 策略：激活窗口 + 等待动画
            self._operator.ensure_window_active()
            time.sleep(1.0 + delay)

        elif error_type == ErrorType.POPUP_BLOCKED:
            # 策略：弹窗清理 + 重试
            if self._popup_handler:
                self._popup_handler.clear_all_popups()
            time.sleep(delay)

        elif error_type == ErrorType.WINDOW_OCCLUDED:
            # 策略：强制置顶
            self._operator.ensure_window_active()
            time.sleep(0.5)

        elif error_type == ErrorType.WECHAT_CRASHED:
            # 策略：重启微信
            logger.warning("尝试重启微信...")
            self._operator.restart_wechat()
            time.sleep(5.0)

        elif error_type == ErrorType.TIMEOUT:
            # 策略：等待 + 重试
            time.sleep(2.0 + delay)

        else:
            # 未知错误：等待后重试
            time.sleep(delay)

    def _record_failure(self, error_type: ErrorType, step: str,
                        message: str, attempt: int):
        """记录失败"""
        record = FailureRecord(
            error_type=error_type,
            step=step,
            message=message,
            attempt=attempt,
        )
        self._failure_history.append(record)

    def get_failure_report(self) -> str:
        """生成失败报告"""
        if not self._failure_history:
            return "无失败记录"

        lines = ["失败报告:"]
        by_type: Dict[ErrorType, int] = {}
        for record in self._failure_history:
            by_type[record.error_type] = by_type.get(record.error_type, 0) + 1

        for err_type, count in by_type.items():
            lines.append(f"  {err_type.value}: {count} 次")

        lines.append(f"  连续失败: {self._consecutive_failures}")
        return '\n'.join(lines)
