"""
多微信账号管理器 — 管理多个微信实例的注册、切换、状态跟踪。

核心设计：
  每个微信窗口是一个独立的"账号"。
  账号通过窗口标题（微信昵称）区分。
  每个账号有独立的：
    - Publisher（发布器）
    - Calibrator（校准器）
    - Operator（操作器）
    - RiskDetector（风控）
    - 配置（每日限制、类人参数等可独立覆盖）

  共享资源：
    - OCR 引擎（多个账号共用一个 OCR 实例）
    - FeatureLocator（特征匹配器）
    - EventBus（事件总线，带账号前缀）

使用方式：
    manager = AccountManager(ocr_engine, feature_locator)
    accounts = manager.scan()           # 扫描所有运行的微信窗口
    manager.register("工作号", hwnd)     # 手动注册
    manager.set_active("工作号")         # 切换活跃账号
    result = manager.publish(task)       # 在活跃账号上发布

CLI 用法：
    python main.py --accounts            # 列出所有检测到的微信窗口
    python main.py --account 工作号 --text "工作内容"
    python main.py --account 生活号 --interactive

Author: 版本无关微信自动化系统
"""

import logging
import threading
from typing import Optional, Dict, List, Tuple
from dataclasses import dataclass, field
from pathlib import Path

import win32gui
import win32con

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# 数据结构
# ═══════════════════════════════════════════════════════════════

@dataclass
class AccountInfo:
    """单个微信账号的信息"""
    name: str                         # 账号昵称（从微信窗口标题获取）
    hwnd: int                         # 窗口句柄
    process_id: int                   # 进程 ID
    window_rect: Tuple[int, int, int, int] = (0, 0, 0, 0)
    is_minimized: bool = False
    is_visible: bool = True
    last_seen: float = 0.0

    def __hash__(self):
        return hash(self.hwnd)


@dataclass
class AccountState:
    """账号运行时状态"""
    info: AccountInfo
    publisher: any = None             # EventDrivenPublisher 实例
    is_active: bool = False
    daily_post_count: int = 0
    last_publish_time: float = 0.0
    config_overrides: dict = field(default_factory=dict)  # 账号级配置覆盖


# ═══════════════════════════════════════════════════════════════
# 窗口枚举器 — 查找和区分微信窗口
# ═══════════════════════════════════════════════════════════════

class WeChatWindowFinder:
    """微信窗口查找器 — 枚举所有微信窗口实例"""

    WECHAT_CLASS = "WeChatMainWndForPC"

    @staticmethod
    def enum_all() -> List[Tuple[int, str]]:
        """
        枚举所有微信窗口。

        Returns:
            [(hwnd, window_title), ...] 列表
        """
        windows = []

        def callback(hwnd, results):
            if win32gui.IsWindowVisible(hwnd):
                class_name = win32gui.GetClassName(hwnd)
                if class_name == WeChatWindowFinder.WECHAT_CLASS:
                    title = win32gui.GetWindowText(hwnd)
                    if title and title != '微信':  # "微信"是默认标题，说明未登录
                        results.append((hwnd, title or f"微信窗口_{hwnd}"))

        win32gui.EnumWindows(callback, windows)
        return windows

    @staticmethod
    def find_by_name(name: str) -> Optional[int]:
        """按窗口标题查找微信窗口"""
        for hwnd, title in WeChatWindowFinder.enum_all():
            if name in title or title in name:
                return hwnd
        return None

    @staticmethod
    def get_window_info(hwnd: int) -> Optional[AccountInfo]:
        """获取指定窗口的详细信息"""
        try:
            if not win32gui.IsWindow(hwnd):
                return None

            title = win32gui.GetWindowText(hwnd)
            rect = win32gui.GetWindowRect(hwnd)
            is_min = win32gui.IsIconic(hwnd)
            is_vis = win32gui.IsWindowVisible(hwnd)

            import win32process
            _, pid = win32process.GetWindowThreadProcessId(hwnd)

            return AccountInfo(
                name=title or f"微信_{hwnd}",
                hwnd=hwnd,
                process_id=pid,
                window_rect=rect,
                is_minimized=is_min,
                is_visible=is_vis,
            )
        except Exception:
            return None

    @staticmethod
    def activate_window(hwnd: int):
        """激活指定微信窗口"""
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        win32gui.SetForegroundWindow(hwnd)


# ═══════════════════════════════════════════════════════════════
# 账号管理器
# ═══════════════════════════════════════════════════════════════

class AccountManager:
    """
    多微信账号管理器。

    每个账号有独立的 Publisher/Calibrator/Operator/RiskDetector，
    共享 OCR 引擎和 FeatureLocator。

    使用方式：
        mgr = AccountManager(ocr_engine, feature_locator, config)
        mgr.scan_and_register()                # 自动扫描注册所有微信窗口
        mgr.set_active("工作号")               # 切换活跃账号
        mgr.publish(PublishTask("hello"))     # 在活跃账号上发布
    """

    def __init__(self, ocr_engine=None, feature_locator=None,
                 base_config: dict = None, bus=None):
        self._ocr = ocr_engine
        self._feature = feature_locator
        self._base_config = base_config or {}
        self._bus = bus
        self._accounts: Dict[str, AccountState] = {}
        self._active_name: Optional[str] = None
        self._lock = threading.Lock()

    # ── 账号发现与注册 ──

    def scan(self) -> List[AccountInfo]:
        """扫描当前运行的所有微信窗口"""
        windows = WeChatWindowFinder.enum_all()
        results = []

        for hwnd, title in windows:
            info = WeChatWindowFinder.get_window_info(hwnd)
            if info:
                results.append(info)
                logger.info(f"发现微信窗口: '{info.name}' (hwnd={hwnd})")

        return results

    def scan_and_register(self) -> int:
        """扫描并自动注册所有微信窗口"""
        accounts = self.scan()
        count = 0

        for info in accounts:
            self.register(info)
            count += 1

        return count

    def register(self, info: AccountInfo, config_overrides: dict = None) -> bool:
        """
        注册一个微信账号。

        Args:
            info: 账号信息（窗口句柄等）
            config_overrides: 账号级配置覆盖（每日限制等）

        Returns:
            True 表示注册成功
        """
        with self._lock:
            if info.name in self._accounts:
                # 更新已有账号的信息
                self._accounts[info.name].info = info
                logger.debug(f"更新账号: {info.name}")
                return True

            # 创建新账号
            state = AccountState(
                info=info,
                config_overrides=config_overrides or {},
            )

            # 延迟创建 publisher（在首次 activate 时）
            self._accounts[info.name] = state
            logger.info(f"注册账号: {info.name}")

            # 如果是第一个账号，自动设为活跃
            if self._active_name is None:
                self._active_name = info.name

            return True

    def unregister(self, name: str) -> bool:
        """注销账号"""
        with self._lock:
            if name not in self._accounts:
                return False

            state = self._accounts[name]
            if state.publisher:
                try:
                    state.publisher.shutdown()
                except Exception:
                    pass

            del self._accounts[name]

            if self._active_name == name:
                self._active_name = next(iter(self._accounts), None)

            logger.info(f"注销账号: {name}")
            return True

    # ── 账号切换 ──

    def set_active(self, name: str) -> bool:
        """切换活跃账号"""
        with self._lock:
            if name not in self._accounts:
                logger.error(f"账号不存在: {name}")
                return False

            # 停用旧账号
            if self._active_name and self._active_name in self._accounts:
                self._accounts[self._active_name].is_active = False

            # 激活新账号
            self._active_name = name
            self._accounts[name].is_active = True

            # 激活窗口
            WeChatWindowFinder.activate_window(
                self._accounts[name].info.hwnd
            )

            # 确保 publisher 已初始化
            if self._accounts[name].publisher is None:
                self._init_publisher(name)

            logger.info(f"切换活跃账号: {name}")
            return True

    @property
    def active_name(self) -> Optional[str]:
        return self._active_name

    @property
    def active(self) -> Optional[AccountState]:
        if self._active_name:
            return self._accounts.get(self._active_name)
        return None

    # ── 发布操作 ──

    def publish(self, task, account_name: str = None) -> any:
        """在指定账号上发布朋友圈"""
        name = account_name or self._active_name
        if name is None:
            raise RuntimeError("未指定账号")

        if name not in self._accounts:
            raise RuntimeError(f"账号不存在: {name}")

        # 如果不是活跃账号，先切换
        if name != self._active_name:
            self.set_active(name)

        state = self._accounts[name]

        if state.publisher is None:
            self._init_publisher(name)

        # 发布
        result = state.publisher.publish(task)

        if result.success:
            state.daily_post_count += 1

        return result

    def publish_batch(self, tasks: list, account_name: str = None) -> list:
        """在指定账号上批量发布"""
        name = account_name or self._active_name
        results = []

        for task in tasks:
            result = self.publish(task, account_name=name)
            results.append(result)

        return results

    # ── 状态查询 ──

    def list_all(self) -> List[dict]:
        """列出所有账号及其状态"""
        result = []
        for name, state in self._accounts.items():
            result.append({
                'name': name,
                'is_active': state.is_active,
                'is_minimized': state.info.is_minimized,
                'process_id': state.info.process_id,
                'daily_posts': state.daily_post_count,
                'publisher_ready': state.publisher is not None,
            })
        return result

    def get_status(self, name: str = None) -> dict:
        """获取指定账号的详细状态"""
        name = name or self._active_name
        if name is None or name not in self._accounts:
            return {'error': '账号不存在'}

        state = self._accounts[name]
        status = {
            'name': name,
            'is_active': state.is_active,
            'daily_posts': state.daily_post_count,
            'window_visible': state.info.is_visible,
            'window_minimized': state.info.is_minimized,
        }

        if state.publisher:
            try:
                login = state.publisher.operator.check_login_state()
                status['logged_in'] = login.get('logged_in', False)
            except Exception:
                status['logged_in'] = False

        return status

    def refresh_windows(self):
        """刷新所有账号的窗口状态（窗口可能移动/最小化等）"""
        for name, state in self._accounts.items():
            info = WeChatWindowFinder.get_window_info(state.info.hwnd)
            if info:
                state.info = info
            else:
                logger.warning(f"账号 {name} 的窗口已不存在")

    def shutdown(self):
        """关闭所有账号的 publisher"""
        for name, state in self._accounts.items():
            if state.publisher:
                try:
                    state.publisher.shutdown()
                except Exception:
                    pass
        self._accounts.clear()
        logger.info("AccountManager 已关闭")

    # ── 内部 ──

    def _init_publisher(self, name: str):
        """为指定账号初始化 publisher"""
        from .publisher import EventDrivenPublisher

        # 合并基础配置和账号覆盖配置
        config = dict(self._base_config)
        overrides = self._accounts[name].config_overrides

        if 'daily_limits' in overrides:
            if 'safety' not in config:
                config['safety'] = {}
            config['safety']['daily_limits'] = overrides['daily_limits']

        publisher = EventDrivenPublisher(config_path=None, bus=self._bus)
        publisher._config = config  # 注入合并后的配置

        # 注入共享的 OCR 和 Feature
        if self._ocr:
            publisher.ocr = self._ocr
        if self._feature:
            publisher.feature = self._feature

        # 设置正确的微信窗口
        hwnd = self._accounts[name].info.hwnd
        publisher.operator._wechat_hwnd = hwnd

        self._accounts[name].publisher = publisher
        logger.info(f"账号 {name} 的 publisher 已初始化")
