"""
朋友圈发布主控模块 —— 整合所有子系统，实现完整的"进入→编辑→发布→验证"流程。

这是整个系统的指挥中心，协调定位、执行、监控、恢复四个子系统。

完整发布流程：
  1. 前置检查    → 窗口激活 + 弹窗清理 + 风控检查
  2. 自动校准    → 如果无缓存映射，执行 OCR 扫描建立坐标映射
  3. 进入朋友圈   → 点击导航栏"朋友圈"标签
  4. 输入文字     → 点击输入区 + 类人打字输入
  5. 添加图片     → （可选）粘贴图片
  6. 确认发布     → 点击"发表"按钮
  7. 验证成功     → 等待"已发送"提示出现
  8. 任务间休息   → 随机 30-120s 延迟

每步失败有独立的错误恢复和重试。

Author: 版本无关微信自动化系统
"""

import time
import logging
from typing import Optional, List, Dict, Any
from dataclasses import dataclass, field

from ..locator.ocr_locator import OCRLocator
from ..locator.feature_locator import FeatureLocator
from ..locator.anchor_locator import AnchorCalibrator
from ..locator.router import LocateRouter, ElementDescriptor, MOMENTS_ELEMENTS
from ..executor.human_sim import HumanSimulator, SimulationConfig
from ..executor.state_machine import (
    WorkflowStateMachine, WorkflowState, WorkflowContext, StateConfig
)
from ..executor.operator import Operator
from ..executor.uia_bridge import UIABridge
from ..executor.file_dialog import FileDialogHandler
from ..monitor.risk_detector import RiskDetector
from ..monitor.popup_handler import PopupHandler
from ..recovery.error_recovery import ErrorRecovery, ErrorType

logger = logging.getLogger(__name__)


@dataclass
class PublishTask:
    """单次发布任务"""
    text: str                        # 文字内容
    images: List[str] = field(default_factory=list)  # 图片路径列表
    location: str = ""               # 所在地（"所在位置"功能）
    privacy: str = "公开"            # 隐私设置
    remind_users: List[str] = field(default_factory=list)  # @提醒的用户


@dataclass
class PublishResult:
    """发布结果"""
    success: bool
    task: PublishTask
    attempts: int                    # 尝试次数
    elapsed_seconds: float           # 耗时
    error_message: str = ""          # 失败原因
    step_times: Dict[str, float] = field(default_factory=dict)


class MomentsPublisher:
    """
    朋友圈发布主控。

    使用方式：
        publisher = MomentsPublisher(config)
        publisher.initialize()   # 自动校准

        task = PublishTask(text="今天天气真好", images=["photo.jpg"])
        result = publisher.publish(task)

        publisher.shutdown()
    """

    def __init__(self, config_path: str = None):
        """
        Args:
            config_path: settings.yaml 配置文件路径
        """
        # 加载配置
        self._config = self._load_config(config_path)

        # 初始化日志
        self._setup_logging()

        # ── 初始化各子系统 ──
        ocr_config = self._config.get('ocr', {})
        feature_config = self._config.get('feature_matching', {})
        human_config_raw = self._config.get('human_simulation', {})
        safety_config = self._config.get('safety', {})
        moments_config = self._config.get('moments', {})

        # 定位系统
        self.ocr = OCRLocator(engine=ocr_config.get('engine', 'paddleocr'),
                              config=ocr_config)
        self.feature = FeatureLocator(
            algorithm=feature_config.get('algorithm', 'orb'),
            config=feature_config,
        )
        self.calibrator = AnchorCalibrator(self.ocr, self.feature)
        self.router = LocateRouter(self.ocr, self.feature, self.calibrator)

        # 执行系统
        self.uia = UIABridge()  # C# UIAutomation 桥接（微信 4.x 关键）
        self.sim = HumanSimulator(SimulationConfig(
            base_delay=human_config_raw.get('base_delay', 3.0),
            delay_shape=human_config_raw.get('delay_shape', 3.0),
        ))
        self.operator = Operator(self.router, self.sim, self._config, uia=self.uia)
        self.file_dialog = FileDialogHandler()  # 文件对话框处理器

        # 监控系统
        self.risk_detector = RiskDetector(self.ocr, safety_config)
        self.popup_handler = PopupHandler(self.ocr, self.router)

        # 恢复系统
        self.recovery = ErrorRecovery(
            self.operator, self.popup_handler, self.risk_detector
        )

        # ── 状态配置 ──
        self._state_configs = {
            WorkflowState.ENTERING_MOMENTS: StateConfig(
                max_retries=3, timeout=30.0, cooldown_on_fail=2.0
            ),
            WorkflowState.TYPING_CONTENT: StateConfig(
                max_retries=3, timeout=20.0, cooldown_on_fail=1.0
            ),
            WorkflowState.ADDING_IMAGES: StateConfig(
                max_retries=2, timeout=60.0, cooldown_on_fail=3.0
            ),
            WorkflowState.CONFIRMING_PUBLISH: StateConfig(
                max_retries=3, timeout=15.0, cooldown_on_fail=2.0
            ),
            WorkflowState.VERIFYING_SUCCESS: StateConfig(
                max_retries=5, timeout=moments_config.get('publish_verify_timeout', 10.0),
                cooldown_on_fail=1.0,
            ),
        }

        # ── 状态 ──
        self._initialized = False
        self._daily_post_count = 0
        self._stats: List[PublishResult] = []

        logger.info("朋友圈发布器初始化完成")

    # ══════════════════════════════════════════════════════════
    # 公共接口
    # ══════════════════════════════════════════════════════════

    def initialize(self) -> bool:
        """
        初始化：自动校准 + 窗口激活。

        Returns:
            True 表示初始化成功
        """
        logger.info("=" * 50)
        logger.info("开始初始化...")

        # 1. 查找微信窗口
        if not self.operator.find_wechat_window():
            logger.error("未找到微信窗口，请确认微信已启动")
            return False

        # 2. 激活窗口
        if not self.operator.ensure_window_active():
            logger.error("无法激活微信窗口")
            return False

        # 3. 检查登录状态
        login_state = self.operator.check_login_state()
        if not login_state['logged_in']:
            if login_state['page'] == 'not_running':
                logger.error("微信未运行")
            else:
                logger.error(
                    f"微信可能已掉线: {login_state['details']}\n"
                    "请确认微信已登录到主界面（能看到'聊天''通讯录'导航栏）"
                )
            return False
        logger.info(f"登录状态: ✅ 已登录 ({login_state['page']})")

        # 4. 自动校准
        mapping = self.calibrator.calibrate()
        if len(mapping.anchors) < 3:
            logger.warning(f"校准锚点不足 ({len(mapping.anchors)} 个)，但继续运行")

        # 5. 启动窗口位置监控（窗口移动时自动重新校准）
        self.operator.start_window_monitoring(
            on_recalibrate=lambda: self.calibrator.calibrate(force=True)
        )

        # 6. 风控检查
        risk_level = self.risk_detector.check(force=True)
        if risk_level.value >= 3:
            logger.critical(f"初始化时检测到高风险: {risk_level.name}")
            return False

        self._initialized = True
        logger.info("初始化完成")
        return True

    def publish(self, task: PublishTask) -> PublishResult:
        """
        执行一次朋友圈发布。

        Args:
            task: 发布任务

        Returns:
            PublishResult 发布结果
        """
        if not self._initialized:
            if not self.initialize():
                return PublishResult(
                    success=False, task=task, attempts=0,
                    elapsed_seconds=0, error_message="初始化失败"
                )

        start_time = time.time()
        attempts = 0
        MAX_ATTEMPTS = 3

        for attempt in range(1, MAX_ATTEMPTS + 1):
            attempts = attempt
            logger.info(f"\n{'─' * 40}")
            logger.info(f"📝 发布尝试 {attempt}/{MAX_ATTEMPTS}: {task.text[:30]}...")

            try:
                result = self._execute_publish(task, attempt)
                if result.success:
                    self._daily_post_count += 1
                    self._stats.append(result)
                    logger.info(f"✅ 发布成功 (耗时 {result.elapsed_seconds:.0f}s)")
                    return result

            except Exception as e:
                logger.error(f"发布异常: {e}")
                self.recovery._record_failure(
                    ErrorType.UNKNOWN, "publish", str(e), attempt
                )

            # 失败后等待再重试
            if attempt < MAX_ATTEMPTS:
                cooldown = 2.0 * attempt
                logger.info(f"等待 {cooldown}s 后重试...")
                time.sleep(cooldown)

        elapsed = time.time() - start_time
        logger.error(f"❌ 发布失败 ({attempts} 次尝试, {elapsed:.0f}s)")
        return PublishResult(
            success=False, task=task, attempts=attempts,
            elapsed_seconds=elapsed,
            error_message=self.recovery.get_failure_report(),
        )

    def publish_batch(self, tasks: List[PublishTask]) -> List[PublishResult]:
        """
        批量发布。

        Args:
            tasks: 发布任务列表

        Returns:
            发布结果列表
        """
        results = []
        for i, task in enumerate(tasks):
            # 前置检查
            if not self._pre_task_check():
                logger.warning("前置检查不通过，暂停批量发布")
                break

            logger.info(f"\n📋 任务 {i+1}/{len(tasks)}")
            result = self.publish(task)
            results.append(result)

            # 任务间休息
            if i < len(tasks) - 1:
                self.sim.task_interval()

        # 打印汇总
        success_count = sum(1 for r in results if r.success)
        logger.info(f"\n批量发布完成: {success_count}/{len(results)} 成功")
        return results

    def shutdown(self):
        """关闭发布器，输出统计"""
        logger.info("=" * 50)
        logger.info("运行统计:")
        total = len(self._stats)
        success = sum(1 for s in self._stats if s.success)
        logger.info(f"  总发布: {total}, 成功: {success}")
        logger.info(f"  风控状态: {self.risk_detector.get_status_report()}")
        logger.info(f"  失败记录: {self.recovery.get_failure_report()}")
        logger.info("再见 👋")

    # ══════════════════════════════════════════════════════════
    # 内部：单次发布执行
    # ══════════════════════════════════════════════════════════

    def _execute_publish(self, task: PublishTask, attempt: int) -> PublishResult:
        """执行单次发布（状态机驱动）"""
        start_time = time.time()
        context = WorkflowContext(
            text=task.text,
            images=task.images,
        )

        # 构建状态机
        sm = WorkflowStateMachine()

        # 配置各状态
        for state, config in self._state_configs.items():
            sm.configure_state(state, config)

        # 注册状态处理函数
        sm.register_handler(
            WorkflowState.ENTERING_MOMENTS,
            lambda ctx: self._step_enter_moments(),
        )
        sm.register_handler(
            WorkflowState.TYPING_CONTENT,
            lambda ctx: self._step_type_content(ctx),
        )
        sm.register_handler(
            WorkflowState.ADDING_IMAGES,
            lambda ctx: self._step_add_images(ctx),
        )
        sm.register_handler(
            WorkflowState.CONFIRMING_PUBLISH,
            lambda ctx: self._step_confirm_publish(),
        )
        sm.register_handler(
            WorkflowState.VERIFYING_SUCCESS,
            lambda ctx: self._step_verify_success(),
        )

        # 启动状态机
        sm.start(context)

        # 运行状态机
        while not sm.is_terminal():
            sm.tick()

            if sm.state == WorkflowState.WAITING:
                wait_time = sm.cooldown_remaining
                if wait_time > 0:
                    time.sleep(min(wait_time, 5.0))  # 最多等 5 秒一次

            # 检查风控
            if self.risk_detector.check().value >= 3:
                logger.critical("风控等级过高，中止当前发布")
                break

        elapsed = time.time() - start_time

        if sm.is_success():
            return PublishResult(
                success=True, task=task, attempts=attempt,
                elapsed_seconds=elapsed,
                step_times=context.step_times,
            )
        else:
            return PublishResult(
                success=False, task=task, attempts=attempt,
                elapsed_seconds=elapsed,
                error_message=sm.get_error(),
                step_times=context.step_times,
            )

    # ══════════════════════════════════════════════════════════
    # 各步骤实现
    # ══════════════════════════════════════════════════════════

    def _step_enter_moments(self) -> bool:
        """步骤 1：进入朋友圈"""
        logger.info("→ 进入朋友圈...")

        return self.recovery.with_recovery(
            lambda: self.operator.enter_moments(
                nav_element=MOMENTS_ELEMENTS['nav_moments'],
                verify_element=MOMENTS_ELEMENTS['input_hint'],
            ),
            step_name="进入朋友圈",
            error_type=ErrorType.LOCATE_FAILED,
            max_attempts=3,
        )

    def _step_type_content(self, ctx: WorkflowContext) -> bool:
        """步骤 2：输入文字内容"""
        if not ctx.text:
            return True  # 纯图片朋友圈

        logger.info(f"→ 输入文字: {ctx.text[:30]}...")

        return self.recovery.with_recovery(
            lambda: self.operator.type_content(
                text=ctx.text,
                input_element=MOMENTS_ELEMENTS['input_hint'],
            ),
            step_name="输入文字",
            error_type=ErrorType.CLICK_NO_RESPONSE,
            max_attempts=2,
        )

    def _step_add_images(self, ctx: WorkflowContext) -> bool:
        """步骤 3：添加图片"""
        if not ctx.images:
            return True  # 没有图片

        logger.info(f"→ 添加 {len(ctx.images)} 张图片...")

        return self.recovery.with_recovery(
            lambda: self.operator.add_images(
                image_paths=ctx.images,
                add_btn=MOMENTS_ELEMENTS['btn_add_photo'],
            ),
            step_name="添加图片",
            error_type=ErrorType.TIMEOUT,
            max_attempts=2,
        )

    def _step_confirm_publish(self) -> bool:
        """步骤 4：点击发表按钮"""
        logger.info("→ 确认发布...")

        # 操作前的小随机延迟（模拟"最后检查一遍"）
        self.sim.micro_pause(mean=1.0)

        return self.recovery.with_recovery(
            lambda: self.operator.click_publish(
                publish_element=MOMENTS_ELEMENTS['btn_publish'],
            ),
            step_name="点击发表",
            error_type=ErrorType.LOCATE_FAILED,
            max_attempts=3,
        )

    def _step_verify_success(self) -> bool:
        """步骤 5：验证发布成功"""
        logger.info("→ 验证发布成功...")

        return self.recovery.with_recovery(
            lambda: self.operator.verify_published(
                success_element=MOMENTS_ELEMENTS['msg_success'],
                timeout=self._config.get('moments', {}).get('publish_verify_timeout', 10.0),
            ),
            step_name="验证成功",
            error_type=ErrorType.TIMEOUT,
            max_attempts=5,
        )

    # ══════════════════════════════════════════════════════════
    # 前置检查
    # ══════════════════════════════════════════════════════════

    def _pre_task_check(self) -> bool:
        """每个任务前的前置检查"""
        # 1. 登录状态检查（最重要）
        login_state = self.operator.check_login_state()
        if not login_state['logged_in']:
            logger.critical(f"掉线检测: {login_state['details']}")
            return False

        # 2. 每日限制
        if not self.risk_detector.record_operation('posts'):
            logger.error("达到每日发布上限")
            return False

        # 3. 风控冷却
        if not self.risk_detector.wait_if_needed():
            return False

        # 4. 窗口激活
        if not self.operator.ensure_window_active():
            return False

        # 5. 弹窗清理
        self.popup_handler.clear_blocking_popups()

        return True

    # ══════════════════════════════════════════════════════════
    # 配置与日志
    # ══════════════════════════════════════════════════════════

    def _load_config(self, config_path: str = None) -> dict:
        """加载配置文件"""
        import yaml
        from pathlib import Path

        if config_path is None:
            # 默认路径：项目根目录/config/settings.yaml
            config_path = Path(__file__).parent.parent.parent / "config" / "settings.yaml"

        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                return yaml.safe_load(f) or {}
        except FileNotFoundError:
            logger.warning(f"配置文件未找到: {config_path}，使用默认配置")
            return {}
        except Exception as e:
            logger.error(f"配置文件加载失败: {e}")
            return {}

    def _setup_logging(self):
        """配置日志"""
        log_config = self._config.get('logging', {})
        log_level = log_config.get('level', 'INFO')
        log_path = log_config.get('path', 'logs/automation.log')

        from pathlib import Path
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)

        # 如果使用 loguru
        try:
            from loguru import logger as loguru_logger
            loguru_logger.add(
                log_path,
                level=log_level,
                rotation=log_config.get('rotation', '10 MB'),
                retention=log_config.get('retention', '7 days'),
            )
        except ImportError:
            # 回退到标准 logging
            logging.basicConfig(
                level=getattr(logging, log_level),
                format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
                handlers=[
                    logging.FileHandler(log_path, encoding='utf-8'),
                    logging.StreamHandler(),
                ],
            )
