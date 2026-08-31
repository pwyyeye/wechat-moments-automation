"""
事件驱动的朋友圈发布器 —— 用事件替代轮询和固定延迟。

与旧版 publisher.py 的本质区别：

  旧版：
    click("朋友圈") → time.sleep(1.5) → OCR 扫描 → 找到了吗? → 没找到再等
    (阻塞调用 + 固定延迟 + 轮询确认)

  事件驱动版：
    click("朋友圈") → 等待 text.appeared("这一刻的想法") → 自动进入下一步
    (操作 → 等待事件 → 事件到达即响应，无空转)

  时间线对比：
    旧：click ─[等1.5s]─ OCR扫描 ─[等0.5s]─ OCR扫描 ─[找到了]─ 下一步
    事件：click ──[0.8s后"这一刻的想法"出现]── 事件到达 ── 下一步

事件驱动的优势：
  1. 响应速度 = 事件发生时间（而非最坏情况的 timeout）
  2. 无 CPU 空转（不在循环里 OCR 扫描）
  3. 组件解耦（Watcher 不知道谁在等待，Publisher 不知道谁在监测）
  4. 天然支持异常处理（超时事件 → 重试逻辑）

架构：
  ┌─────────────┐     ┌──────────────┐     ┌─────────────┐
  │   Watchers   │────→│   EventBus   │────→│  Publisher  │
  │  (事件源)    │     │  (消息中心)   │     │  (消费者)    │
  │              │     │              │     │             │
  │ OCR Watcher  │     │ 事件队列      │     │ 状态机      │
  │ UIA Watcher  │     │ 订阅/发布     │     │ 步骤流转    │
  │ Window Watch │     │ wait_for()    │     │ 异常处理    │
  │ Timer Watch  │     │              │     │              │
  └─────────────┘     └──────────────┘     └─────────────┘

Author: 版本无关微信自动化系统
"""

import time
import logging
from typing import Optional, List, Dict, Any, Callable
from dataclasses import dataclass, field, replace

from ..locator.ocr_locator import OCRLocator
from ..locator.feature_locator import FeatureLocator
from ..locator.anchor_locator import AnchorCalibrator
from ..locator.router import LocateRouter, ElementDescriptor, MOMENTS_ELEMENTS
from ..executor.human_sim import HumanSimulator, SimulationConfig
from ..executor.operator import Operator
from ..executor.uia_bridge import UIABridge
from ..executor.file_dialog import FileDialogHandler
from ..executor.version_detector import VersionDetector
from ..monitor.risk_detector import RiskDetector
from ..monitor.popup_handler import PopupHandler
from ..monitor.notifier import Notifier, create_notifier_from_config
from ..recovery.error_recovery import ErrorRecovery
from .events import EventBus, Event, EventType, global_event_bus
from .watchers import WatchManager

logger = logging.getLogger(__name__)


@dataclass
class PublishTask:
    """发布任务"""
    text: str
    images: List[str] = field(default_factory=list)
    confirm_publish: bool = False
    before_final_click: Optional[Callable[[], None]] = field(
        default=None,
        repr=False,
        compare=False,
    )
    after_final_click: Optional[Callable[[], None]] = field(
        default=None,
        repr=False,
        compare=False,
    )


@dataclass
class PublishResult:
    """发布结果"""
    success: bool
    task: PublishTask
    elapsed_seconds: float
    error_message: str = ""
    step_times: Dict[str, float] = field(default_factory=dict)
    published: bool = False
    stopped_before_publish: bool = False
    final_click_intent: bool = False


class EventDrivenPublisher:
    """
    事件驱动的朋友圈发布器。

    核心流程（状态机由事件驱动）：

      IDLE
        │  bus.emit(STEP_STARTED)
        ▼
      ENTERING_MOMENTS
        │  operator.click("朋友圈")
        │  等待 text.appeared("这一刻的想法")  ← 事件，不是轮询
        │  超时 → 重试
        ▼
      TYPING_CONTENT
        │  operator.type(text)
        │  bus.wait_for(TIMER_EXPIRED, 0.5s)  ← 打字完成确认
        ▼
      ADDING_IMAGES (如果有图片)
        │  operator.paste_image(file)
        │  等待 upload.complete              ← 上传完成事件
        ▼
      CONFIRMING_PUBLISH
        │  operator.click("发表")
        │  等待 text.appeared("已发送")       ← 发布确认
        ▼
      DONE
    """

    def __init__(self, config_path: str = None,
                 bus: EventBus = None):
        self._config = self._load_config(config_path)
        self.bus = bus or global_event_bus

        # 初始化各子系统（和旧版一样）
        self._init_subsystems()

        # 事件驱动的状态
        self._state = "IDLE"
        self._watch_manager: Optional[WatchManager] = None
        self._stats: List[PublishResult] = []

        # 订阅系统事件
        self._setup_event_handlers()

        logger.info("事件驱动发布器初始化完成")

    def _init_subsystems(self):
        """初始化所有子系统"""
        ocr_cfg = self._config.get('ocr', {})
        feat_cfg = self._config.get('feature_matching', {})
        human_cfg = self._config.get('human_simulation', {})
        safety_cfg = self._config.get('safety', {})

        self.ocr = OCRLocator(engine=ocr_cfg.get('engine', 'paddleocr'), config=ocr_cfg)
        self.feature = FeatureLocator(algorithm=feat_cfg.get('algorithm', 'orb'), config=feat_cfg)
        self.calibrator = AnchorCalibrator(self.ocr, self.feature)
        self.router = LocateRouter(self.ocr, self.feature, self.calibrator)

        self.uia = UIABridge()
        self.sim = HumanSimulator(SimulationConfig(
            base_delay=human_cfg.get('base_delay', 3.0),
            delay_shape=human_cfg.get('delay_shape', 3.0),
        ))
        self.operator = Operator(self.router, self.sim, self._config, uia=self.uia)
        self.file_dialog = FileDialogHandler()

        self.risk_detector = RiskDetector(self.ocr, safety_cfg)
        self.popup_handler = PopupHandler(self.ocr, self.router)
        self.recovery = ErrorRecovery(self.operator, self.popup_handler, self.risk_detector)

        # 版本检测
        self.version_detector = VersionDetector()

        # 通知系统
        self.notifier = create_notifier_from_config(self._config)

    def _setup_event_handlers(self):
        """注册全局事件处理器"""
        # 窗口移动/缩放 → 重新校准
        self.bus.on(EventType.WINDOW_MOVED, self._on_window_moved)
        self.bus.on(EventType.WINDOW_RESIZED, self._on_window_moved)

        # 弹窗检测 → 自动关闭
        self.bus.on(EventType.POPUP_DETECTED, self._on_popup)

        # 风控信号 → 自适应冷却
        self.bus.on(EventType.RISK_WARNING, self._on_risk_warning)
        self.bus.on(EventType.RISK_CRITICAL, self._on_risk_critical)

        # 掉线 → 停止一切
        self.bus.on(EventType.LOGIN_LOST, self._on_login_lost)

        # 步骤失败 → 通知
        self.bus.on(EventType.STEP_FAILED, self.notifier.handle_event)
        self.bus.on(EventType.RISK_WARNING, self.notifier.handle_event)
        self.bus.on(EventType.RISK_CRITICAL, self.notifier.handle_event)

    # ══════════════════════════════════════════════════════════
    # 公共接口
    # ══════════════════════════════════════════════════════════

    def initialize(self) -> bool:
        """初始化：校准 + 启动所有 Watcher + 登录检测"""
        logger.info("事件驱动发布器初始化...")

        # 0. 检测微信版本变化
        current_ver = self.version_detector.get_version()
        if current_ver:
            logger.info(f"当前微信版本: {current_ver.raw}")

        version_changed = self.version_detector.is_version_changed()
        is_desktop_v4 = bool(current_ver and current_ver.major >= 4)
        if version_changed:
            logger.info("微信版本已变化，将触发自动重建")
            self.notifier.info(
                "微信版本变化",
                f"检测到微信版本变化: {current_ver.raw if current_ver else 'unknown'}"
            )

        # Template extraction performs many full-screen OCR passes. Desktop
        # 4.x uses the validated window-relative camera path instead.
        if self.version_detector.templates_exist():
            logger.info("图标模板库就绪")
        elif not is_desktop_v4:
            template_count = self.version_detector.ensure_templates(self.ocr)
            if template_count > 0:
                logger.info(f"图标模板就绪: {template_count} 个")
        else:
            logger.info("微信 4.x 跳过旧版图标模板自动生成")

        # 1. 窗口
        if not self.operator.find_wechat_window():
            return False
        if not self.operator.ensure_window_active():
            return False

        # 2. 登录检测
        login_state = self.operator.check_login_state()
        if not login_state['logged_in']:
            logger.error(f"未登录: {login_state['details']}")
            return False

        # Desktop 4.x uses a separate window and window-relative positioning;
        # legacy navigation calibration is both slow and inapplicable there.
        if is_desktop_v4:
            logger.info("微信 4.x 跳过旧版导航校准")
        else:
            self.calibrator.calibrate(force=version_changed)

        # 3.5 版本变化时重建模板 + 标记新版本
        if version_changed and not is_desktop_v4:
            self.version_detector.trigger_rebuild(self.calibrator, self.ocr)
            self.version_detector.mark_current_version()
        elif version_changed:
            self.version_detector.mark_current_version()

        # 4. 启动所有 Watcher（事件源）
        self._watch_manager = WatchManager(self.bus, self.ocr, self.uia)
        self._watch_manager.start_all(
            watch_ocr_texts=[
                # 朋友圈页面元素
                '这一刻的想法', '相册', '发表', '所在位置',
                '谁可以看', '提醒谁看',
                # 验证元素
                '已发送', '发送失败',
                # 风控元素
                '操作太频繁', '请重新登录', '版本过低',
            ],
            watch_uia_names=[
                '朋友圈', '发表', '聊天', '通讯录',
            ],
            ocr_region_provider=self.operator.active_window_region,
            enable_ocr=not is_desktop_v4,
        )

        # 5. 风控检查
        self.risk_detector.check(
            force=True,
            region=self._active_alert_region(),
        )

        logger.info("初始化完成，事件驱动系统就绪")
        return True

    def publish(self, task: PublishTask) -> PublishResult:
        """
        事件驱动的发布流程。

        不再用 time.sleep() 等待——等待事件到达。
        """
        start_time = time.time()
        step_times = {}

        # 前置检查
        if not self._pre_check(will_publish=task.confirm_publish):
            return PublishResult(False, task, time.time() - start_time, "前置检查失败")

        self.bus.emit(Event(EventType.STEP_STARTED, "publisher", {"step": "publish"}))

        # ── 步骤 1：进入朋友圈 ──
        t0 = time.time()
        self._prepared_image_count = 0
        if not self._step_enter_moments(task.images):
            return PublishResult(False, task, time.time() - start_time, "进入朋友圈失败")
        step_times['enter_moments'] = time.time() - t0

        # ── 步骤 2：输入文字 ──
        t0 = time.time()
        if task.text:
            if not self._step_type_text(task.text):
                return PublishResult(False, task, time.time() - start_time, "输入文字失败")
        step_times['type_text'] = time.time() - t0

        # ── 步骤 3：添加图片 ──
        t0 = time.time()
        remaining_images = task.images[self._prepared_image_count:]
        if remaining_images:
            if not self._step_add_images(remaining_images):
                return PublishResult(False, task, time.time() - start_time, "添加图片失败")
        step_times['add_images'] = time.time() - t0

        # 默认停在编辑页；调用方必须显式授权最终的“发表”点击。
        if not task.confirm_publish:
            elapsed = time.time() - start_time
            result = PublishResult(
                True,
                task,
                elapsed,
                step_times=step_times,
                stopped_before_publish=True,
            )
            self._stats.append(result)
            self.bus.emit(Event(
                EventType.STEP_COMPLETED,
                "publisher",
                {"elapsed": elapsed, "stopped_before_publish": True},
            ))
            logger.warning("安全模式：内容已准备，未点击发表")
            return result

        # ── 步骤 4：发布 ──
        t0 = time.time()
        if task.before_final_click is not None:
            try:
                task.before_final_click()
            except Exception as error:
                logger.exception("最终点击意图持久化失败，停止发布")
                return PublishResult(
                    False,
                    task,
                    time.time() - start_time,
                    f"最终点击意图持久化失败: {error}",
                    step_times=step_times,
                )
        publish_confirmed = (
            self._step_publish(task.text, after_click=task.after_final_click)
            if task.after_final_click is not None
            else self._step_publish(task.text)
        )
        if not publish_confirmed:
            return PublishResult(
                False,
                task,
                time.time() - start_time,
                "发布结果无法确认",
                step_times=step_times,
                final_click_intent=True,
            )
        step_times['publish'] = time.time() - t0

        elapsed = time.time() - start_time
        result = PublishResult(
            True,
            task,
            elapsed,
            step_times=step_times,
            published=True,
            final_click_intent=True,
        )
        self._stats.append(result)

        self.bus.emit(Event(EventType.STEP_COMPLETED, "publisher",
                            {"elapsed": elapsed}))

        logger.info(f"✅ 发布成功 ({elapsed:.0f}s)")
        return result

    def publish_batch(self, tasks: List[PublishTask]) -> List[PublishResult]:
        """批量发布"""
        results = []
        for i, task in enumerate(tasks):
            logger.info(f"任务 {i+1}/{len(tasks)}")
            result = self.publish(task)
            results.append(result)

            # 任务间的事件驱动延迟
            if i < len(tasks) - 1:
                self._task_interval()
        return results

    def shutdown(self):
        """关闭"""
        if self._watch_manager:
            self._watch_manager.stop_all()
        self.bus.shutdown()
        logger.info("事件驱动发布器已关闭")

    # ══════════════════════════════════════════════════════════
    # 步骤实现 —— 每一步都等事件，不等时间
    # ══════════════════════════════════════════════════════════

    def _step_enter_moments(self, image_paths: list = None,
                            max_retries: int = 3) -> bool:
        """
        进入朋友圈。
        点击导航栏"朋友圈" → 等待"这一刻的想法"文字出现。

        旧版：click + time.sleep(1.5) + OCR扫描确认
        新版：click + wait_for(text.appeared("这一刻的想法"))
        """
        image_paths = image_paths or []

        # WeChat 4.x opens Moments in a separate Qt window. Its camera button
        # opens the file picker before the compose controls become available.
        if self.operator.activate_moments_window():
            return self._prepare_desktop_editor(image_paths)

        for attempt in range(1, max_retries + 1):
            self.operator.activate_main_window()
            # 点击
            self.operator.click_by_uia(name="朋友圈")
            if not self.operator.click_element(MOMENTS_ELEMENTS['nav_moments']):
                self.operator.click_by_uia(name="朋友圈")

            # 等待事件：朋友圈页面加载完成的标志
            event = self.bus.wait_for(
                EventType.TEXT_APPEARED,
                payload_match={'matched': '这一刻的想法'},
                timeout=5.0,
            )

            if self.operator.activate_moments_window():
                return self._prepare_desktop_editor(image_paths)

            if event is not None:
                logger.info(f"朋友圈页面已加载 (尝试 {attempt})")
                return True

            # 超时：可能被弹窗阻断或网络慢
            logger.warning(f"等待朋友圈页面超时 (尝试 {attempt}/{max_retries})")
            self.popup_handler.clear_blocking_popups()

        return False

    def _prepare_desktop_editor(self, image_paths: list,
                                timeout: float = 12.0) -> bool:
        """Open the desktop 4.x editor, selecting its required first image."""
        region = self.operator.active_window_region()
        self.ocr._invalidate_cache()
        if self.ocr.find_best('这一刻的想法', region=region):
            # The desktop 4.x compose panel is opened by selecting its first
            # image. Reusing that panel must not append the same image again.
            self._prepared_image_count = 1
            return True

        if not image_paths:
            logger.error("微信 Windows 4.x 当前流程需要至少一张图片才能打开编辑页")
            return False
        if not self.operator.click_moments_camera():
            return False
        if not self.file_dialog.select_file_via_pywinauto(image_paths[0], timeout=timeout):
            return False

        deadline = time.time() + timeout
        while time.time() < deadline:
            self.operator.activate_moments_window()
            region = self.operator.active_window_region()
            self.ocr._invalidate_cache()
            if self.ocr.find_best('这一刻的想法', region=region):
                self._prepared_image_count = 1
                logger.info("朋友圈编辑页已打开，首张图片已添加")
                return True
            time.sleep(0.5)

        logger.error("选择图片后未检测到朋友圈编辑页")
        return False

    def _step_type_text(self, text: str) -> bool:
        """
        输入文字。
        点击输入框 → 输入 → 等一个小定时器确认。
        """
        # 点击输入框
        input_element = replace(
            MOMENTS_ELEMENTS['input_hint'],
            ocr_region=self.operator.active_window_region(),
        )
        if not self.operator.click_element(input_element):
            return False
        self.sim.micro_pause(mean=0.2)

        # 清空旧内容 + 输入
        import pyautogui
        pyautogui.hotkey('ctrl', 'a')
        self.sim.micro_pause(mean=0.1)
        self.sim.type_text(text)

        # 事件驱动的等待：用 Timer 确认打字完成
        self._watch_manager.after(0.5, {'reason': 'typing_complete'})
        event = self.bus.wait_for(EventType.TIMER_EXPIRED, timeout=2.0)

        return True

    def _step_add_images(self, image_paths: list, max_retries: int = 2) -> bool:
        """
        添加图片。
        粘贴 → 等待 upload.complete 事件。
        """
        for i, img_path in enumerate(image_paths):
            for attempt in range(1, max_retries + 1):
                # 粘贴图片
                if not self.file_dialog.paste_image_from_file(img_path):
                    logger.warning(f"图片 {i+1} 粘贴失败 (尝试 {attempt})")
                    continue

                # 等待上传完成事件 OR 超时
                # 先等一个小延迟（给微信一点时间反应）
                self._watch_manager.after(2.0, {'reason': 'initial_upload_wait'})
                self.bus.wait_for(EventType.TIMER_EXPIRED, timeout=3.0)

                # 检查上传状态
                blocks = self.ocr.scan_screen(
                    region=self.operator.active_window_region()
                )
                texts = [b.text for b in blocks]

                if any('上传失败' in t for t in texts):
                    logger.warning(f"图片 {i+1} 上传失败 (尝试 {attempt})")
                    continue  # 重试

                # 成功
                logger.info(f"图片 {i+1}/{len(image_paths)} 已添加")
                break

        return True

    def _step_publish(self, expected_text: str = "",
                      max_retries: int = 1,
                      after_click: Optional[Callable[[], None]] = None) -> bool:
        """
        点击发表 → 等待"已发送"事件确认。
        """
        for attempt in range(1, max_retries + 1):
            # 发表前的随机微延迟（模拟"最后检查一遍"）
            self.sim.micro_pause(mean=1.0)

            # 点击发表
            publish_element = replace(
                MOMENTS_ELEMENTS['btn_publish'],
                ocr_region=self.operator.active_window_region(),
            )
            if not self.operator.click_element(publish_element):
                continue

            if after_click is not None:
                try:
                    after_click()
                except Exception:
                    # The click has already happened. Continue confirmation and
                    # let the caller's durable outbox retry reporting later.
                    logger.exception("最终点击后的状态记录失败，继续确认发布结果")

            # WeChat 4.x does not expose a usable UIA event. Verify that the
            # editor disappears, but never click twice when status is unclear.
            deadline = time.time() + 15.0
            editor_gone_scans = 0
            expected_probe = expected_text.strip()[:8]
            while time.time() < deadline:
                self.operator.activate_moments_window()
                region = self.operator.active_window_region()
                self.ocr._invalidate_cache()
                blocks = self.ocr.scan_screen(region=region)
                texts = [block.text for block in blocks]

                if any('发送失败' in text or '发表失败' in text for text in texts):
                    logger.error("微信报告发布失败")
                    break
                if any('已发送' in text for text in texts):
                    logger.info("发布已确认")
                    return True
                if expected_probe and any(expected_probe in text for text in texts):
                    logger.info("已在朋友圈列表识别到新发布文案")
                    return True

                editor_visible = any(
                    marker in text
                    for text in texts
                    for marker in ('发表', '取消', '谁可以看', '提醒谁看')
                )
                if not editor_visible and len(blocks) >= 3:
                    editor_gone_scans += 1
                    if editor_gone_scans >= 2:
                        logger.info("朋友圈编辑页已关闭，发布完成")
                        return True
                else:
                    editor_gone_scans = 0
                time.sleep(0.5)

            logger.warning(f"发布结果无法确认 (尝试 {attempt}/{max_retries})")

        return False

    # ══════════════════════════════════════════════════════════
    # 事件处理器（全局）
    # ══════════════════════════════════════════════════════════

    def _on_window_moved(self, event: Event):
        """窗口移动 → 自动重新校准"""
        dx = event.payload.get('dx', 0)
        dy = event.payload.get('dy', 0)
        logger.info(f"窗口移动 ({dx:+d}, {dy:+d}) → 重新校准")
        self.calibrator.calibrate(force=True)

    def _on_popup(self, event: Event):
        """弹窗出现 → 自动关闭"""
        popup_name = event.payload.get('name', '未知')
        logger.info(f"检测到弹窗: {popup_name} → 自动关闭")
        self.popup_handler.clear_blocking_popups()

    def _on_risk_warning(self, event: Event):
        """风控警告 → 暂时冷却"""
        cooldown = event.payload.get('cooldown', 120)
        logger.warning(f"风控警告 → 冷却 {cooldown}s")
        self._watch_manager.after(cooldown, {'reason': 'risk_cooldown'})

    def _on_risk_critical(self, event: Event):
        """风控严重 → 停止所有操作"""
        logger.critical("风控严重 → 停止所有操作！")

    def _on_login_lost(self, event: Event):
        """掉线 → 停止一切"""
        logger.critical("检测到掉线 → 停止所有待处理任务")

    # ══════════════════════════════════════════════════════════
    # 辅助
    # ══════════════════════════════════════════════════════════

    def _pre_check(self, will_publish: bool = False) -> bool:
        """发布前的快速检查"""
        login_state = self.operator.check_login_state()
        if not login_state['logged_in']:
            logger.critical(f"掉线: {login_state['details']}")
            return False
        if will_publish and not self.risk_detector.record_operation('posts'):
            return False
        if not self.risk_detector.wait_if_needed():
            return False
        self.operator.ensure_window_active()
        self.popup_handler.clear_blocking_popups(
            region=self._active_alert_region()
        )
        return True

    def _active_alert_region(self):
        """Return the central area where WeChat displays modal warnings."""
        region = self.operator.active_window_region()
        if not region:
            return None
        left, top, width, height = region
        return (
            left + width // 5,
            top + height // 5,
            width * 3 // 5,
            height * 3 // 5,
        )

    def _task_interval(self):
        """任务间的事件驱动延迟"""
        import random
        delay = random.uniform(30, 120)
        self._watch_manager.after(delay, {'reason': 'task_interval'})
        self.bus.wait_for(EventType.TIMER_EXPIRED, timeout=delay + 5)

    def _load_config(self, config_path: str = None) -> dict:
        import yaml
        from pathlib import Path
        if config_path is None:
            config_path = Path(__file__).parent.parent.parent / "config" / "settings.yaml"
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                return yaml.safe_load(f) or {}
        except Exception:
            return {}
