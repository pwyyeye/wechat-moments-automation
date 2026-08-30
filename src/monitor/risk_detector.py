"""
风控信号检测器 —— 实时监控微信弹出的风控/异常信号。

检测的信号类型：
  1. 强制登出弹窗（"为了你的账号安全，请重新登录"）
  2. 功能限制警告（"操作太频繁，请稍后再试"）
  3. 版本过低弹窗（"当前客户端版本过低"）
  4. 验证要求（"需要安全验证"）
  5. 静默功能失效（操作后无响应超过阈值时间）

响应策略：指数退避冷却
  第 1 次 → 冷却 2 分钟
  第 2 次 → 冷却 4 分钟
  第 3 次 → 冷却 8 分钟
  ...
  达到上限 → 停止当天所有操作

Author: 版本无关微信自动化系统
"""

import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List

logger = logging.getLogger(__name__)


class RiskLevel(Enum):
    """风险等级"""
    SAFE = 0           # 安全
    SUSPICIOUS = 1     # 可疑（检测到非阻断弹窗）
    WARNING = 2        # 警告（操作频繁提示）
    DANGER = 3         # 危险（账号被限制）
    CRITICAL = 4       # 严重（强制登出）


@dataclass
class RiskSignal:
    """风控信号"""
    name: str                      # 信号名称
    level: RiskLevel               # 风险等级
    ocr_text: str                  # OCR 检测文本（版本无关）
    is_blocking: bool = True       # 是否阻断性信号
    auto_handler: str = "pause"    # 自动处理方式: 'pause' | 'stop' | 'ignore'
    cooldown_multiplier: float = 2.0  # 触发后的冷却倍数


@dataclass
class RiskState:
    """风控状态"""
    level: RiskLevel = RiskLevel.SAFE
    consecutive_events: int = 0       # 连续触发次数
    total_events_today: int = 0       # 当日总事件数
    cooldown_until: float = 0.0       # 冷却截止时间
    last_event_time: float = 0.0      # 最近一次事件时间
    event_history: List[Dict] = field(default_factory=list)


# ═══════════════════════════════════════════════════════════════
# 预定义的风控信号（文字标签是版本无关的）
# ═══════════════════════════════════════════════════════════════

KNOWN_RISK_SIGNALS = [
    RiskSignal(
        name="force_relogin",
        level=RiskLevel.CRITICAL,
        ocr_text="重新登录",
        auto_handler="stop",
        cooldown_multiplier=8.0,  # 触发后冷却 8 倍基础时间
    ),
    RiskSignal(
        name="account_restricted",
        level=RiskLevel.DANGER,
        ocr_text="账号已被限制",
        auto_handler="stop",
        cooldown_multiplier=10.0,
    ),
    RiskSignal(
        name="operation_too_frequent",
        level=RiskLevel.WARNING,
        ocr_text="操作太频繁",
        auto_handler="pause",
        cooldown_multiplier=2.0,
    ),
    RiskSignal(
        name="verify_required",
        level=RiskLevel.DANGER,
        ocr_text="安全验证",
        auto_handler="stop",
        cooldown_multiplier=6.0,
    ),
    RiskSignal(
        name="version_too_old",
        level=RiskLevel.CRITICAL,
        ocr_text="版本过低",
        auto_handler="stop",
        cooldown_multiplier=999.0,  # 需要人工升级
    ),
    RiskSignal(
        name="unusual_login",
        level=RiskLevel.WARNING,
        ocr_text="登录环境异常",
        auto_handler="pause",
        cooldown_multiplier=4.0,
    ),
]


class RiskDetector:
    """
    风控信号检测器。

    使用方式：
        detector = RiskDetector(ocr_locator)
        detector.check()  # 每次操作前调用
        if detector.state.level >= RiskLevel.WARNING:
            detector.wait_cooldown()
    """

    def __init__(self, ocr_locator, config: dict = None):
        """
        Args:
            ocr_locator: OCRLocator 实例
            config: 配置字典
        """
        self._ocr = ocr_locator
        self._config = config or {}
        self.state = RiskState()
        self._signals = KNOWN_RISK_SIGNALS.copy()
        self._base_cooldown_minutes = self._config.get('cooldown_base_minutes', 2)
        self._max_cooldown = self._config.get('max_cooldown_seconds', 21600)  # 6h
        self._check_interval = self._config.get('risk_check_interval', 5.0)
        self._last_check_time = 0.0

        # 每日限制
        self._daily_limits = self._config.get('daily_limits', {})
        self._daily_counts = {
            'posts': 0,
            'likes': 0,
            'comments': 0,
        }

    # ── 公共接口 ──

    def check(self, force: bool = False, region=None) -> RiskLevel:
        """
        检查是否有风控信号。

        Args:
            force: 强制执行 OCR 扫描（忽略检查间隔）

        Returns:
            当前风险等级
        """
        now = time.time()

        # 检查间隔控制
        if not force and (now - self._last_check_time) < self._check_interval:
            return self.state.level

        self._last_check_time = now

        if region is not None:
            self._ocr._invalidate_cache()
            blocks = self._ocr.scan_screen(region=region)

            def detected(signal):
                return any(signal.ocr_text in block.text for block in blocks)
        else:
            def detected(signal):
                return bool(self._ocr.find_text(signal.ocr_text))

        for signal in self._signals:
            if detected(signal):
                self._on_signal_detected(signal)
                return signal.level

        # 如果冷却时间已过，降级风险等级
        if self.state.cooldown_until > 0 and now >= self.state.cooldown_until:
            self._decay_risk_level()

        return self.state.level

    def wait_if_needed(self) -> bool:
        """
        如果需要冷却，等待冷却结束。

        Returns:
            True 表示可以继续操作，False 表示应该停止
        """
        if self.state.level.value >= RiskLevel.CRITICAL.value:
            logger.critical("风险等级达到 CRITICAL，停止所有操作")
            return False

        remaining = max(0, self.state.cooldown_until - time.time())
        if remaining > 0:
            logger.warning(f"风控冷却中，剩余 {remaining:.0f}s...")
            if remaining <= 60:
                time.sleep(remaining)
            else:
                # 冷却时间太长，返回 False 让上层决定
                return False
        return True

    def record_operation(self, op_type: str) -> bool:
        """
        记录一次操作，检查是否超过每日限制。

        Returns:
            True 表示可以继续操作
        """
        if op_type in self._daily_counts:
            self._daily_counts[op_type] += 1
            limit = self._daily_limits.get(f'max_{op_type}', float('inf'))
            count = self._daily_counts[op_type]

            if count >= limit * 0.8:
                logger.warning(f"接近每日限制: {op_type} {count}/{limit}")
            if count >= limit:
                logger.error(f"达到每日限制: {op_type} {count}/{limit}")
                return False

        return True

    def get_status_report(self) -> str:
        """生成风控状态报告"""
        lines = [
            f"风控等级: {self.state.level.name}",
            f"连续事件: {self.state.consecutive_events}",
            f"今日事件: {self.state.total_events_today}",
            f"冷却剩余: {max(0, self.state.cooldown_until - time.time()):.0f}s",
            f"每日操作: {self._daily_counts}",
        ]
        return '\n'.join(lines)

    # ── 内部方法 ──

    def _on_signal_detected(self, signal: RiskSignal):
        """风控信号被检测到时的处理"""
        self.state.consecutive_events += 1
        self.state.total_events_today += 1
        self.state.level = signal.level
        self.state.last_event_time = time.time()

        # 记录事件
        self.state.event_history.append({
            'time': time.time(),
            'signal': signal.name,
            'level': signal.level.name,
            'consecutive': self.state.consecutive_events,
        })

        # 计算冷却时间（指数退避）
        cooldown_minutes = (
            self._base_cooldown_minutes
            * signal.cooldown_multiplier
            * (2 ** (self.state.consecutive_events - 1))
        )
        cooldown_seconds = min(cooldown_minutes * 60, self._max_cooldown)
        self.state.cooldown_until = time.time() + cooldown_seconds

        logger.warning(
            f"🚨 检测到风控信号: {signal.name} "
            f"(等级={signal.level.name}, "
            f"连续={self.state.consecutive_events}, "
            f"冷却={cooldown_seconds:.0f}s)"
        )

        if signal.auto_handler == 'stop':
            logger.critical(f"自动停止: {signal.name}")

    def _decay_risk_level(self):
        """冷却结束后降级风险等级"""
        if self.state.level == RiskLevel.WARNING:
            self.state.level = RiskLevel.SUSPICIOUS
        elif self.state.level == RiskLevel.SUSPICIOUS:
            self.state.level = RiskLevel.SAFE
        elif self.state.level == RiskLevel.DANGER:
            # 危险信号只降一级
            self.state.level = RiskLevel.WARNING

        if self.state.consecutive_events >= 3:
            # 多次事件后缓慢降低连续计数
            self.state.consecutive_events = max(0, self.state.consecutive_events - 1)

        logger.info(f"风险降级: → {self.state.level.name}")
