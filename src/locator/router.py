"""
定位策略路由器 —— 协调多种定位策略，按优先级降级。

定位优先级：
  1. OCR 文字定位     → 最快最准确，适用所有带文字的元素
  2. 特征点匹配       → 适用纯图标元素
  3. 锚点相对定位     → 适用以上两种都失败时的推断定位
  4. 全屏模板匹配     → 最终兜底（传统方案，版本敏感）

每个策略失败时自动降级到下一个，所有策略失败则记录截图并报错。

Author: 版本无关微信自动化系统
"""

import logging
import time
from enum import Enum
from typing import Optional, Tuple, List, Dict, Any
from dataclasses import dataclass
from pathlib import Path
from datetime import datetime

import pyautogui

from .ocr_locator import OCRLocator
from .feature_locator import FeatureLocator
from .anchor_locator import AnchorCalibrator

logger = logging.getLogger(__name__)


class LocateStrategy(Enum):
    OCR = "ocr"
    FEATURE = "feature"
    ANCHOR = "anchor"
    TEMPLATE = "template"


@dataclass
class LocateResult:
    """定位结果"""
    x: int
    y: int
    strategy: LocateStrategy
    confidence: float
    attempts: int
    elapsed_ms: float


@dataclass
class ElementDescriptor:
    """
    界面元素描述符 —— 描述一个界面元素的多种定位方式。

    示例:
        ElementDescriptor(
            name="朋友圈导航按钮",
            ocr_text="朋友圈",
            anchor_ref=('nav_聊天', 128, 0),  # 从"聊天"标签右移128px
            feature_template="templates/icons/moments_tab.png",
        )
    """
    name: str                                # 元素名称（用于日志）
    ocr_text: Optional[str] = None           # OCR 查找文字
    ocr_region: Optional[Tuple] = None       # OCR 搜索区域
    feature_template: Optional[str] = None   # 特征匹配模板路径
    feature_templates: Optional[List[str]] = None  # 多模板候选
    anchor_ref: Optional[Tuple[str, int, int]] = None  # (锚点名, dx, dy)
    fallback_region: Optional[Tuple] = None  # 兜底搜索区域


class LocateRouter:
    """
    定位策略路由器。

    使用方式：
        router = LocateRouter(ocr, feature, calibrator)

        desc = ElementDescriptor(
            name="朋友圈Tab",
            ocr_text="朋友圈",
            feature_template="templates/icons/moments_tab.png",
        )
        result = router.locate(desc)
        if result:
            pyautogui.click(result.x, result.y)
    """

    def __init__(self, ocr: OCRLocator, feature: FeatureLocator,
                 calibrator: AnchorCalibrator, config: dict = None):
        self.ocr = ocr
        self.feature = feature
        self.calibrator = calibrator
        self._config = config or {}
        self._failure_screenshots_dir = Path(
            self._config.get('failure_screenshots_dir', 'logs/failures')
        )
        self._failure_screenshots_dir.mkdir(parents=True, exist_ok=True)

    # ── 公共接口 ──

    def locate(self, element: ElementDescriptor,
               max_attempts: int = 3) -> Optional[LocateResult]:
        """
        定位界面元素。按策略优先级依次尝试。

        每个策略内部也可重试。
        """
        attempts = 0
        start_time = time.time()

        # ── 策略 1：OCR 文字定位 ──
        if element.ocr_text:
            logger.debug(f"策略1 (OCR): 查找 '{element.ocr_text}'")
            for retry in range(max_attempts):
                attempts += 1
                if retry > 0:
                    time.sleep(0.5)
                    self.ocr._invalidate_cache()  # 强制刷新

                best = self.ocr.find_best(
                    element.ocr_text,
                    region=element.ocr_region,
                )
                if best:
                    elapsed = (time.time() - start_time) * 1000
                    logger.info(
                        f"✅ [{element.name}] OCR定位成功 "
                        f"({best.x}, {best.y}) 置信度={best.confidence:.2f}"
                    )
                    return LocateResult(
                        x=best.x, y=best.y,
                        strategy=LocateStrategy.OCR,
                        confidence=best.confidence,
                        attempts=attempts,
                        elapsed_ms=elapsed,
                    )

        # ── 策略 2：特征点匹配 ──
        if element.feature_template or element.feature_templates:
            logger.debug(f"策略2 (FEATURE): 特征匹配")
            templates = element.feature_templates or [element.feature_template]
            for retry in range(max_attempts):
                attempts += 1
                if retry > 0:
                    time.sleep(0.3)

                result = self.feature.locate_best(templates)
                if result:
                    path, x, y = result
                    elapsed = (time.time() - start_time) * 1000
                    logger.info(
                        f"✅ [{element.name}] 特征匹配成功 "
                        f"({x}, {y}) 模板={Path(path).name}"
                    )
                    return LocateResult(
                        x=x, y=y,
                        strategy=LocateStrategy.FEATURE,
                        confidence=0.9,
                        attempts=attempts,
                        elapsed_ms=elapsed,
                    )

        # ── 策略 3：锚点相对定位 ──
        if element.anchor_ref:
            logger.debug(f"策略3 (ANCHOR): 锚点推算")
            anchor_name, dx, dy = element.anchor_ref
            attempts += 1
            pos = self.calibrator.locate_relative(anchor_name, dx, dy)
            if pos:
                elapsed = (time.time() - start_time) * 1000
                logger.info(
                    f"✅ [{element.name}] 锚点定位成功 "
                    f"({pos[0]}, {pos[1]}) 参考={anchor_name}"
                )
                return LocateResult(
                    x=pos[0], y=pos[1],
                    strategy=LocateStrategy.ANCHOR,
                    confidence=0.7,  # 锚点定位置信度较低
                    attempts=attempts,
                    elapsed_ms=elapsed,
                )

        # ── 策略 4：全屏模板匹配（兜底） ──
        if element.feature_template and element.fallback_region:
            logger.debug(f"策略4 (TEMPLATE): 区域模板匹配")
            attempts += 1
            # 使用 PyAutoGUI 内置的像素级模板匹配
            # 这是版本敏感的兜底方案
            try:
                pos = pyautogui.locateCenterOnScreen(
                    element.feature_template,
                    confidence=0.7,
                    region=element.fallback_region,
                    grayscale=True,
                )
                if pos:
                    elapsed = (time.time() - start_time) * 1000
                    logger.info(
                        f"✅ [{element.name}] 模板匹配成功 "
                        f"(兜底方案) ({pos.x}, {pos.y})"
                    )
                    return LocateResult(
                        x=pos.x, y=pos.y,
                        strategy=LocateStrategy.TEMPLATE,
                        confidence=0.7,
                        attempts=attempts,
                        elapsed_ms=elapsed,
                    )
            except pyautogui.ImageNotFoundException:
                pass

        # ── 全部失败 ──
        elapsed = (time.time() - start_time) * 1000
        logger.error(
            f"❌ [{element.name}] 所有策略均失败 "
            f"(尝试{attempts}次, 耗时{elapsed:.0f}ms)"
        )
        self._save_failure_screenshot(element)
        return None

    def click_element(self, element: ElementDescriptor) -> bool:
        """定位并点击元素"""
        result = self.locate(element)
        if result is None:
            return False
        pyautogui.click(result.x, result.y)
        return True

    def wait_element(self, element: ElementDescriptor,
                     timeout: float = 15.0,
                     interval: float = 0.5) -> Optional[LocateResult]:
        """轮询等待元素出现"""
        start = time.time()
        while time.time() - start < timeout:
            result = self.locate(element, max_attempts=1)
            if result is not None:
                return result
            time.sleep(interval)
        logger.warning(f"[{element.name}] 等待超时 ({timeout}s)")
        return None

    def verify_element(self, element: ElementDescriptor) -> bool:
        """
        验证元素是否在屏幕上可见。
        只做一次快速查找，不重试。
        """
        result = self.locate(element, max_attempts=1)
        return result is not None

    # ── 批量操作 ──

    def locate_sequence(self, elements: List[ElementDescriptor]
                        ) -> List[Optional[LocateResult]]:
        """
        批量定位多个元素。用于校准场景。
        一旦某个元素定位失败，后续仍然继续（不提前终止）。
        """
        results = []
        for elem in elements:
            result = self.locate(elem, max_attempts=2)
            results.append(result)
        return results

    # ── 内部方法 ──

    def _save_failure_screenshot(self, element: ElementDescriptor):
        """定位失败时保存屏幕截图供调试"""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_name = "".join(
            c for c in element.name if c.isalnum() or c in '._-'
        )
        filename = f"{timestamp}_{safe_name}_failure.png"
        filepath = self._failure_screenshots_dir / filename
        try:
            pyautogui.screenshot(str(filepath))
            logger.info(f"📸 失败截图已保存: {filepath}")
        except Exception as e:
            logger.error(f"保存失败截图异常: {e}")


# ═══════════════════════════════════════════════════════════════
# 微信朋友圈界面元素描述库
# ═══════════════════════════════════════════════════════════════

# 预定义的界面元素描述符，直接可用
# 所有文字标签是版本无关的（只要微信还用中文）

MOMENTS_ELEMENTS = {
    # ── 导航栏 ──
    'nav_moments': ElementDescriptor(
        name="朋友圈导航",
        ocr_text="朋友圈",
        anchor_ref=('nav_聊天', 128, 0),
        feature_template="templates/icons/moments_tab.png",
    ),

    # ── 朋友圈页面 ──
    'input_hint': ElementDescriptor(
        name="文字输入框",
        ocr_text="这一刻的想法",
        # 这个元素是灰色的提示文字，OCR 可能识别率较低
    ),
    'btn_add_photo': ElementDescriptor(
        name="添加图片按钮",
        ocr_text="相册",
        # 备选：相机图标用特征匹配
        feature_template="templates/icons/camera_icon.png",
    ),
    'btn_location': ElementDescriptor(
        name="所在位置",
        ocr_text="所在位置",
    ),
    'btn_privacy': ElementDescriptor(
        name="谁可以看",
        ocr_text="谁可以看",
    ),
    'btn_remind': ElementDescriptor(
        name="提醒谁看",
        ocr_text="提醒谁看",
    ),
    'btn_publish': ElementDescriptor(
        name="发表按钮",
        ocr_text="发表",
    ),

    # ── 发布确认 ──
    'msg_success': ElementDescriptor(
        name="发布成功提示",
        ocr_text="已发送",
    ),
}
