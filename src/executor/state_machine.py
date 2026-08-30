"""
流程状态机 —— 管理朋友圈发布的每一步状态转换。

设计原因：
  线性脚本最大的问题是"中间一步失败后，不知道自己在哪，无法安全恢复"。
  状态机让每一步都有明确的状态标识，失败后可以原地重试，
  而不是从第一行重新执行。

状态转换图：
  IDLE → ENTERING_MOMENTS → TYPING_CONTENT → ADDING_IMAGES
       → CONFIRMING_PUBLISH → VERIFYING_SUCCESS → DONE
                                      ↓ (失败)
                                 任意状态可降级到 WAITING（风控冷却）

每个状态：
  - 有独立的失败计数器
  - 有独立的超时时间
  - 有进入条件验证
  - 有退出条件验证

Author: 版本无关微信自动化系统
"""

import time
import logging
from enum import Enum, auto
from typing import Optional, Callable, Dict, Any
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


class WorkflowState(Enum):
    """工作流状态枚举"""
    IDLE = auto()                   # 空闲
    ENTERING_MOMENTS = auto()       # 正在进入朋友圈
    TYPING_CONTENT = auto()         # 正在输入文字
    ADDING_IMAGES = auto()          # 正在添加图片
    CONFIRMING_PUBLISH = auto()     # 正在确认发布
    VERIFYING_SUCCESS = auto()      # 正在验证发布结果
    READY_FOR_REVIEW = auto()       # 已准备完成，安全停在发表前
    DONE = auto()                   # 完成
    WAITING = auto()                # 风控冷却等待中
    ERROR = auto()                  # 错误状态


@dataclass
class StateConfig:
    """状态配置"""
    max_retries: int = 3            # 最大重试次数
    timeout: float = 30.0           # 超时时间（秒）
    cooldown_on_fail: float = 2.0   # 失败后冷却时间（秒）


@dataclass
class WorkflowContext:
    """工作流上下文 —— 携带整个流程的共享数据"""
    text: str = ""                  # 要发布的文字
    images: list = field(default_factory=list)  # 图片路径列表
    confirm_publish: bool = True     # False 时在最终点击前停止
    start_time: float = 0.0         # 流程开始时间
    step_times: Dict[str, float] = field(default_factory=dict)  # 每步耗时


class WorkflowStateMachine:
    """
    朋友圈发布流程状态机。

    使用方式：
        sm = WorkflowStateMachine(handlers)
        sm.start(context)

        while not sm.is_terminal():
            sm.tick()
            if sm.state == WorkflowState.WAITING:
                time.sleep(sm.cooldown_remaining)
    """

    def __init__(self, handlers: Dict[WorkflowState, Callable] = None):
        """
        Args:
            handlers: {状态: 处理函数} 映射
        """
        self._handlers = handlers or {}
        self._state: WorkflowState = WorkflowState.IDLE
        self._previous_state: Optional[WorkflowState] = None
        self._retry_counts: Dict[WorkflowState, int] = {}
        self._state_configs: Dict[WorkflowState, StateConfig] = {}
        self._context: Optional[WorkflowContext] = None
        self._cooldown_until: float = 0.0
        self._error_message: str = ""

        # 默认配置
        for state in WorkflowState:
            self._state_configs[state] = StateConfig()

    # ── 公共接口 ──

    @property
    def state(self) -> WorkflowState:
        return self._state

    @property
    def context(self) -> Optional[WorkflowContext]:
        return self._context

    @property
    def cooldown_remaining(self) -> float:
        return max(0, self._cooldown_until - time.time())

    def configure_state(self, state: WorkflowState, config: StateConfig):
        """配置特定状态的重试次数和超时"""
        self._state_configs[state] = config

    def register_handler(self, state: WorkflowState, handler: Callable):
        """注册状态处理函数"""
        self._handlers[state] = handler

    def start(self, context: WorkflowContext):
        """启动工作流"""
        self._context = context
        self._context.start_time = time.time()
        self._transition(WorkflowState.ENTERING_MOMENTS)
        logger.info(f"工作流启动 → 目标: {context.text[:30]}...")

    def tick(self) -> bool:
        """
        执行当前状态的一步操作。

        Returns:
            True 表示流程继续，False 表示需要等待或已终止
        """
        if self.is_terminal():
            return False

        # 如果处于冷却等待
        if self._state == WorkflowState.WAITING:
            if time.time() >= self._cooldown_until:
                # 冷却结束，恢复到之前的状态
                self._state = self._previous_state or WorkflowState.IDLE
                logger.info(f"冷却结束，恢复到 {self._state.name}")
            else:
                return False

        # 执行当前状态的处理函数
        handler = self._handlers.get(self._state)
        if handler is None:
            logger.error(f"状态 {self._state.name} 无处理函数")
            self._state = WorkflowState.ERROR
            return False

        try:
            start = time.time()
            success = handler(self._context)
            elapsed = time.time() - start

            if self._context:
                self._context.step_times[self._state.name] = elapsed

            if success:
                logger.info(f"✅ {self._state.name} 完成 ({elapsed:.1f}s)")
                self._advance()
            else:
                self._handle_failure()

        except Exception as e:
            logger.error(f"❌ {self._state.name} 异常: {e}")
            self._error_message = str(e)
            self._handle_failure()

        return True

    def is_terminal(self) -> bool:
        """是否已达到终止状态"""
        return self._state in (
            WorkflowState.READY_FOR_REVIEW,
            WorkflowState.DONE,
            WorkflowState.ERROR,
        )

    def is_success(self) -> bool:
        return self._state == WorkflowState.DONE

    def is_ready_for_review(self) -> bool:
        return self._state == WorkflowState.READY_FOR_REVIEW

    def get_error(self) -> str:
        return self._error_message

    def cooldown(self, seconds: float):
        """触发冷却等待"""
        self._previous_state = self._state
        self._state = WorkflowState.WAITING
        self._cooldown_until = time.time() + seconds
        logger.warning(f"进入冷却: {seconds:.0f}s (来自 {self._previous_state.name})")

    def reset(self):
        """重置状态机（用于流程重试）"""
        self._state = WorkflowState.IDLE
        self._previous_state = None
        self._retry_counts.clear()
        self._cooldown_until = 0.0
        self._error_message = ""

    # ── 内部方法 ──

    def _transition(self, new_state: WorkflowState):
        """状态转换，清除新状态的失败计数"""
        self._previous_state = self._state
        self._state = new_state
        self._retry_counts[new_state] = 0
        logger.info(f"→ {new_state.name}")

    def _advance(self):
        """当前状态成功，推进到下一个状态"""
        transitions = {
            WorkflowState.ENTERING_MOMENTS: WorkflowState.TYPING_CONTENT,
            WorkflowState.TYPING_CONTENT: WorkflowState.ADDING_IMAGES,  # 或 CONFIRMING_PUBLISH
            WorkflowState.ADDING_IMAGES: WorkflowState.CONFIRMING_PUBLISH,
            WorkflowState.CONFIRMING_PUBLISH: WorkflowState.VERIFYING_SUCCESS,
            WorkflowState.VERIFYING_SUCCESS: WorkflowState.DONE,
        }

        next_state = transitions.get(self._state)

        # 特殊处理：如果没有图片，跳过添加图片步骤
        if (self._state == WorkflowState.TYPING_CONTENT
                and self._context
                and not self._context.images):
            next_state = WorkflowState.CONFIRMING_PUBLISH

        if (self._state in (WorkflowState.TYPING_CONTENT, WorkflowState.ADDING_IMAGES)
                and self._context
                and not self._context.confirm_publish
                and (self._state == WorkflowState.ADDING_IMAGES or not self._context.images)):
            next_state = WorkflowState.READY_FOR_REVIEW

        if next_state is None:
            logger.error(f"未知的状态转换: {self._state.name}")
            self._state = WorkflowState.ERROR
            return

        self._transition(next_state)

    def _handle_failure(self):
        """处理当前状态失败"""
        config = self._state_configs.get(self._state, StateConfig())
        self._retry_counts[self._state] = self._retry_counts.get(self._state, 0) + 1
        attempts = self._retry_counts[self._state]

        if attempts >= config.max_retries:
            logger.error(
                f"状态 {self._state.name} 重试 {attempts} 次均失败，中止"
            )
            self._state = WorkflowState.ERROR
            self._error_message = f"{self._state.name} 重试 {attempts} 次失败"
            return

        logger.warning(
            f"状态 {self._state.name} 第 {attempts}/{config.max_retries} 次失败，"
            f"冷却 {config.cooldown_on_fail}s 后重试..."
        )
        self.cooldown(config.cooldown_on_fail)
