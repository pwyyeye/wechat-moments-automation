"""
类人行为模拟器 —— 对抗微信服务端行为模式检测。

核心设计原则：
  1. 真实人类不会精确等间隔操作     → Gamma 分布替代固定 sleep
  2. 真实人类不会走直线移动鼠标     → 贝塞尔曲线 + 微抖动
  3. 真实人类不会瞬间粘贴文本       → 逐字符输入 + 自然波动
  4. 真实人类不会每次操作完全一样   → 随机插入多余动作
  5. 真实人类会有"犹豫"和"走神"    → 长尾延迟分布

所有算法基于对抗"服务端异步行为分析"的目标设计。

Author: 版本无关微信自动化系统
"""

import random
import time
import math
import logging
from typing import Tuple, Optional
from dataclasses import dataclass

import pyautogui

logger = logging.getLogger(__name__)

# 全局故障保护：鼠标移动到屏幕左上角 (0,0) 立即终止
pyautogui.FAILSAFE = True
pyautogui.PAUSE = 0.05  # 每个 pyautogui 操作后暂停 50ms（最低限度）


@dataclass
class SimulationConfig:
    """类人行为配置"""
    base_delay: float = 3.0               # 基础操作延迟（秒）
    delay_shape: float = 3.0              # Gamma 分布 shape（越大越集中）
    mouse_speed: int = 800                # 鼠标速度参考值 (px/s)
    bezier_offset_range: Tuple[int, int] = (-80, 80)  # 贝塞尔控制点随机偏移
    click_jitter: int = 3                 # 点击位置随机抖动 (px)
    extra_action_probability: float = 0.3 # 多余操作概率
    typing_wpm_range: Tuple[int, int] = (60, 100)  # 打字速度范围


class HumanSimulator:
    """
    类人行为模拟器。

    使用方式：
        sim = HumanSimulator(SimulationConfig(base_delay=3.0))
        sim.delay()                        # 类人等待
        sim.move_mouse_to(500, 300)        # 类人鼠标移动
        sim.click_at(500, 300)             # 类人点击
        sim.type_text("今天天气真好")       # 类人打字
    """

    def __init__(self, config: SimulationConfig = None):
        self._config = config or SimulationConfig()

    # ══════════════════════════════════════════════════════════
    # 延迟模拟
    # ══════════════════════════════════════════════════════════

    def delay(self, base: float = None):
        """
        类人延迟。使用 Gamma 分布模拟真实操作间隔。

        Gamma 分布特性：
          - 大部分值集中在 base 附近
          - 但存在长尾（模拟走神、犹豫）
          - 叠加均匀噪声增加不可预测性
        """
        base = base or self._config.base_delay
        shape = self._config.delay_shape
        scale = base / shape

        # Gamma 分布主延迟
        delay = random.gammavariate(shape, scale)

        # 叠加 ±30% 均匀噪声
        delay *= random.uniform(0.7, 1.3)

        # 偶尔的"走神"延迟（5% 概率，1.5-3 倍）
        if random.random() < 0.05:
            delay *= random.uniform(1.5, 3.0)
            logger.debug(f"模拟'走神'延迟: {delay:.1f}s")

        delay = max(0.1, delay)  # 不低于 100ms
        time.sleep(delay)

    def micro_pause(self, mean: float = 0.15):
        """微停顿（100-300ms），模拟人在不同步骤间的自然停顿"""
        pause = random.gauss(mean, mean * 0.3)
        time.sleep(max(0.05, pause))

    # ══════════════════════════════════════════════════════════
    # 鼠标模拟
    # ══════════════════════════════════════════════════════════

    def move_mouse_to(self, target_x: int, target_y: int,
                      add_jitter: bool = True):
        """
        贝塞尔曲线鼠标移动。模拟真人手部运动轨迹。

        真人移动鼠标的特征：
          - 轨迹是弧线而非直线（手腕转动产生）
          - 开始慢→加速→快→减速→微调（速度曲线是不对称的）
          - 有微小抖动
        """
        from_x, from_y = pyautogui.position()

        # 距离太短直接移动
        distance = math.hypot(target_x - from_x, target_y - from_y)
        if distance < 20:
            pyautogui.moveTo(target_x, target_y, duration=0.05)
            return

        # 计算步数（距离越远步数越多，但有上下限）
        steps = max(10, min(40, int(distance / 12)))

        # 贝塞尔控制点（制造弧线轨迹）
        cp_range = self._config.bezier_offset_range
        cp_x = (from_x + target_x) / 2 + random.randint(*cp_range)
        cp_y = (from_y + target_y) / 2 + random.randint(
            cp_range[0] // 2, cp_range[1] // 2
        )

        for i in range(steps):
            t = (i + 1) / steps
            # 二次贝塞尔曲线
            x = (1 - t) ** 2 * from_x + 2 * (1 - t) * t * cp_x + t ** 2 * target_x
            y = (1 - t) ** 2 * from_y + 2 * (1 - t) * t * cp_y + t ** 2 * target_y

            # 微抖动（±2px，末端更明显）
            if add_jitter:
                jitter_scale = 0.5 + t * 0.5  # 越到末尾抖动越大
                x += random.randint(-2, 2) * jitter_scale
                y += random.randint(-1, 1) * jitter_scale

            # 速度曲线：开始加速，中段匀速，末尾减速
            if t < 0.2:
                # 加速阶段（从静止到运动，稍慢但逐渐加快）
                step_delay = 0.008 + random.random() * 0.004 * (1 - t / 0.2)
            elif t > 0.8:
                # 减速阶段（逼近目标，精准调整）
                progress_in_decel = (t - 0.8) / 0.2
                step_delay = 0.01 + progress_in_decel * 0.015 + random.random() * 0.005
            else:
                # 匀速阶段（最快）
                step_delay = 0.005 + random.random() * 0.003

            pyautogui.moveTo(x, y, duration=step_delay)

        # 最终精确到达目标
        pyautogui.moveTo(target_x, target_y, duration=0.03)

    def click_at(self, x: int, y: int, add_jitter: bool = True,
                 click_type: str = 'left'):
        """
        类人点击。

        真人点击的特征：
          - 点击位置有微小偏移（不会每次都点在像素正中心）
          - 点击前有微停顿（手在到达后短暂稳定）
          - 点击速度和力度不均
        """
        if add_jitter:
            jitter = self._config.click_jitter
            x += random.randint(-jitter, jitter)
            y += random.randint(-jitter, jitter)

        self.move_mouse_to(x, y)

        # 到达后的微停顿（手稳定下来）
        time.sleep(random.uniform(0.05, 0.15))

        # 执行点击
        if click_type == 'left':
            pyautogui.click()
        elif click_type == 'right':
            pyautogui.rightClick()
        elif click_type == 'double':
            pyautogui.doubleClick()

    # ══════════════════════════════════════════════════════════
    # 键盘模拟
    # ══════════════════════════════════════════════════════════

    def type_text(self, text: str, use_clipboard: bool = True):
        """
        类人打字输入。

        当文本较长时（>20 字），先粘贴再模拟"检查"时间，
        因为真人面对长文本几乎不会逐字输入。

        短文本逐字输入，模拟真实打字节奏。
        """
        if not text:
            return

        requires_unicode_paste = any(ord(char) > 127 for char in text)
        if use_clipboard and (len(text) > 20 or requires_unicode_paste):
            self._paste_with_delay(text)
        else:
            self._type_character_by_character(text)

    def _type_character_by_character(self, text: str):
        """逐字输入，模拟真实中文打字节奏"""
        wpm = random.randint(*self._config.typing_wpm_range)

        # 高频字词库（中文输入法中更容易打出来的字）
        high_freq_chars = set('的一是了不在有人我他这为之来以时就要们说和')

        for i, char in enumerate(text):
            pyautogui.write(char)

            # 字符间延迟：受多种因素影响
            base_delay = 60.0 / wpm  # 每个字符的基础延迟

            if char in '，。！？；：、':
                # 标点符号后停顿更长（人在思考下一句）
                delay = base_delay * random.uniform(2.5, 4.5)
            elif char in high_freq_chars:
                # 高频字快速打出
                delay = base_delay * random.uniform(0.3, 0.6)
            elif i == 0:
                # 句首首个字略慢（开始打字的启动成本）
                delay = base_delay * random.uniform(1.5, 2.5)
            else:
                # 正常波动
                delay = base_delay * random.uniform(0.7, 1.8)

            # 加入微小的随机波动
            delay *= random.uniform(0.8, 1.2)

            time.sleep(max(0.02, delay))

    def _paste_with_delay(self, text: str):
        """粘贴长文本，但模拟'粘贴前复制→切换→粘贴'的时间差"""
        import pyperclip

        # 粘贴前的"准备时间"
        self.micro_pause(mean=0.3)

        # 设置剪贴板
        pyperclip.copy(text)
        self.micro_pause(mean=0.1)

        # Ctrl+V 粘贴
        pyautogui.hotkey('ctrl', 'v')

        # 粘贴后的"检查时间"——真人会扫一眼确认
        time.sleep(random.uniform(0.3, 0.8))

    # ══════════════════════════════════════════════════════════
    # 高级组合动作
    # ══════════════════════════════════════════════════════════

    def extra_action(self):
        """
        随机插入多余动作。
        真人不会每一步都最优——会无意义地晃一下鼠标、
        扫一眼别的地方、犹豫一下等。
        """
        if random.random() > self._config.extra_action_probability:
            return

        action = random.choice([
            'wiggle',        # 轻微晃动鼠标
            'glance',        # 鼠标移到别处再回来
            'hesitate',      # 短暂停顿
            'scroll',        # 轻微滚轮
        ])

        logger.debug(f"插入多余动作: {action}")

        if action == 'wiggle':
            x, y = pyautogui.position()
            pyautogui.moveRel(
                random.randint(-15, 15),
                random.randint(-10, 10),
                duration=0.1
            )
            pyautogui.moveTo(x, y, duration=0.15)

        elif action == 'glance':
            x, y = pyautogui.position()
            pyautogui.moveTo(
                x + random.randint(-100, 100),
                y + random.randint(-80, 80),
                duration=0.3
            )
            time.sleep(random.uniform(0.3, 0.8))
            pyautogui.moveTo(x, y, duration=0.25)

        elif action == 'hesitate':
            time.sleep(random.uniform(0.5, 1.5))

        elif action == 'scroll':
            pyautogui.scroll(random.randint(-3, 3))
            time.sleep(0.2)

    def task_interval(self):
        """
        任务间休息间隔。模拟真实工作节奏。

        人会累、会走神、会喝水。不会是持续 24 小时的均匀操作。
        """
        min_sec, max_sec = 30, 120
        interval = random.uniform(min_sec, max_sec)

        # 10% 概率来一个"长时间休息"（5-15 分钟）
        if random.random() < 0.1:
            interval = random.uniform(300, 900)
            logger.info(f"模拟长时间休息: {interval/60:.0f} 分钟")

        logger.info(f"任务间休息: {interval:.0f}s")
        time.sleep(interval)
