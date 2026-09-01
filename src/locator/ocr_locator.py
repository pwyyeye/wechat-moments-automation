"""
OCR 文字定位器 —— 版本无关的界面元素定位核心。

通过文字语义定位 UI 元素，而非像素或控件属性。
微信怎么改 UI 框架、换字体、调颜色，"朋友圈"三个字永远叫"朋友圈"。

支持两种 OCR 引擎：
  1. PaddleOCR（默认，中文识别最优）
  2. EasyOCR（备选）

Author: 版本无关微信自动化系统
"""

import logging
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

import numpy as np
import pyautogui

logger = logging.getLogger(__name__)

OCR_MODEL_ENV = "WECHAT_PUBLISHER_OCR_MODEL_ROOT"
OCR_MODEL_NAMES = (
    "PP-OCRv6_medium_det",
    "PP-OCRv6_medium_rec",
)
OCR_MODEL_REQUIRED_FILES = (
    "inference.json",
    "inference.pdiparams",
    "inference.yml",
)


def _is_complete_ocr_model_root(root: Path) -> bool:
    return all(
        (root / model_name / filename).is_file()
        for model_name in OCR_MODEL_NAMES
        for filename in OCR_MODEL_REQUIRED_FILES
    )


def resolve_ocr_model_root(config: dict = None) -> Optional[Path]:
    """Resolve explicitly configured or PyInstaller-bundled OCR models."""
    config = config or {}
    configured = config.get("model_root") or os.environ.get(OCR_MODEL_ENV)
    if configured:
        root = Path(configured).expanduser().resolve()
        if not _is_complete_ocr_model_root(root):
            raise RuntimeError(f"OCR model directory is incomplete: {root}")
        return root

    if getattr(sys, "frozen", False):
        root = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)) / "models" / "paddleocr"
        if not _is_complete_ocr_model_root(root):
            raise RuntimeError(f"Bundled OCR models are missing or incomplete: {root}")
        return root

    source_root = Path(__file__).resolve().parents[2] / "models" / "paddleocr"
    return source_root if _is_complete_ocr_model_root(source_root) else None


# ═══════════════════════════════════════════════════════════════
# 数据结构
# ═══════════════════════════════════════════════════════════════

@dataclass
class TextBlock:
    """OCR 识别出的单个文本块"""
    text: str           # 文字内容
    x: int              # 中心点 X
    y: int              # 中心点 Y
    width: int          # 宽度
    height: int         # 高度
    confidence: float   # 识别置信度 0-1
    box: List           # 原始四点坐标 [[x1,y1],[x2,y2],[x3,y3],[x4,y4]]


@dataclass
class OCRResult:
    """OCR 扫描结果"""
    blocks: List[TextBlock]
    timestamp: float
    screen_size: Tuple[int, int]
    region: Optional[Tuple[int, int, int, int]] = None


# ═══════════════════════════════════════════════════════════════
# PaddleOCR 实现
# ═══════════════════════════════════════════════════════════════

class PaddleOCREngine:
    """PaddleOCR 引擎封装"""

    def __init__(self, config: dict = None):
        self._ocr = None
        self._config = config or {}
        self._api_version = 0
        self._initialization_error: Exception | None = None
        self._recognition_error_logged = False

    @property
    def ocr(self):
        """延迟加载 OCR 模型"""
        if self._initialization_error is not None:
            raise RuntimeError("PaddleOCR 本进程初始化失败，需重启 Agent 后重试") from (
                self._initialization_error
            )
        if self._ocr is None:
            try:
                from paddleocr import PaddleOCR
                try:
                    kwargs = {
                        'use_doc_orientation_classify': self._config.get(
                            'use_doc_orientation_classify', False
                        ),
                        'use_doc_unwarping': self._config.get(
                            'use_doc_unwarping', False
                        ),
                        'use_textline_orientation': self._config.get(
                            'use_angle_cls', False
                        ),
                        'enable_mkldnn': self._config.get('enable_mkldnn', False),
                    }
                    model_root = resolve_ocr_model_root(self._config)
                    if model_root is not None:
                        kwargs.update({
                            'text_detection_model_dir': str(
                                model_root / 'PP-OCRv6_medium_det'
                            ),
                            'text_recognition_model_dir': str(
                                model_root / 'PP-OCRv6_medium_rec'
                            ),
                        })
                    else:
                        kwargs['lang'] = self._config.get('lang', 'ch')
                    self._ocr = PaddleOCR(**kwargs)
                    self._api_version = 3
                except TypeError:
                    self._ocr = PaddleOCR(
                        lang=self._config.get('lang', 'ch'),
                        use_angle_cls=self._config.get('use_angle_cls', False),
                    )
                    self._api_version = 2
                logger.info("PaddleOCR 模型加载完成")
            except ImportError:
                raise ImportError(
                    "请安装 PaddleOCR: pip install paddleocr\n"
                    "如果安装失败，尝试: pip install paddlepaddle 后再 pip install paddleocr"
                )
            except Exception as e:
                self._initialization_error = e
                logger.exception(f"PaddleOCR 初始化失败: {e}")
                raise
        return self._ocr

    def recognize(self, image: np.ndarray) -> List[TextBlock]:
        """识别图像中的所有文字"""
        try:
            ocr = self.ocr
            if self._api_version >= 3:
                return self._parse_v3_results(ocr.predict(image))
            result = ocr.ocr(image, cls=False)
        except Exception as e:
            if not self._recognition_error_logged:
                logger.error(f"OCR 识别异常，后续相同错误不再重复记录: {e}")
                self._recognition_error_logged = True
            return []

        if not result or not result[0]:
            return []

        blocks = []
        for line in result[0]:
            box = line[0]      # [[x1,y1],[x2,y2],[x3,y3],[x4,y4]]
            text = line[1][0]   # 文字内容
            conf = line[1][1]   # 置信度

            x1, y1 = box[0]
            x2, y2 = box[2]
            center_x = int((x1 + x2) / 2)
            center_y = int((y1 + y2) / 2)
            width = int(x2 - x1)
            height = int(y2 - y1)

            blocks.append(TextBlock(
                text=text,
                x=center_x, y=center_y,
                width=width, height=height,
                confidence=conf,
                box=box,
            ))

        return blocks

    @staticmethod
    def _parse_v3_results(results) -> List[TextBlock]:
        """Convert PaddleOCR 3.x result objects to the project's stable model."""
        blocks = []
        for result in results or []:
            payload = getattr(result, 'json', result)
            if not isinstance(payload, dict):
                continue
            data = payload.get('res', payload)
            texts = data.get('rec_texts', [])
            scores = data.get('rec_scores', [])
            polygons = data.get('rec_polys', data.get('dt_polys', []))

            for text, score, polygon in zip(texts, scores, polygons):
                points = [[float(x), float(y)] for x, y in polygon]
                xs = [point[0] for point in points]
                ys = [point[1] for point in points]
                x1, x2 = min(xs), max(xs)
                y1, y2 = min(ys), max(ys)
                blocks.append(TextBlock(
                    text=str(text),
                    x=int((x1 + x2) / 2),
                    y=int((y1 + y2) / 2),
                    width=int(x2 - x1),
                    height=int(y2 - y1),
                    confidence=float(score),
                    box=points,
                ))

        return blocks


# ═══════════════════════════════════════════════════════════════
# 微信原生 OCR 引擎（独立调用 WeChatOCR.exe）
# ═══════════════════════════════════════════════════════════════

class WeChatNativeOCREngine:
    """
    微信自带 OCR 引擎的独立调用封装。

    微信安装目录下包含 WeChatOCR.exe + mmmojo.dll + 模型文件，
    可以通过 protobuf 协议独立调用，不需要启动微信 GUI。

    参考项目: github.com/swigger/wechat-ocr
    """

    def __init__(self, wechat_ocr_path: str):
        self.ocr_path = Path(wechat_ocr_path)
        if not self.ocr_path.exists():
            raise FileNotFoundError(f"未找到微信 OCR: {self.ocr_path}")
        # TODO: 实现 protobuf 通信协议
        # 当前为占位，完整实现需要逆向 WeChatOCR.exe 的 protobuf schema
        logger.warning("微信原生 OCR 引擎需要 protobuf 协议支持，暂未实现。使用 PaddleOCR 替代。")

    def recognize(self, image: np.ndarray) -> List[TextBlock]:
        raise NotImplementedError("微信原生 OCR 引擎待实现")


# ═══════════════════════════════════════════════════════════════
# OCR 定位器主类
# ═══════════════════════════════════════════════════════════════

class OCRLocator:
    """
    OCR 文字定位器。

    使用方式：
        locator = OCRLocator()
        locator.click_text("朋友圈")      # 找到并点击"朋友圈"
        candidates = locator.find_text("发表")  # 查找所有"发表"的位置
    """

    def __init__(self, engine: str = 'paddleocr', config: dict = None):
        """
        Args:
            engine: 'paddleocr' | 'wechat_native'
            config: 引擎配置字典
        """
        self._config = config or {}
        self._cache: Optional[OCRResult] = None
        self._cache_ttl = self._config.get('cache_ttl', 2.0)

        if engine == 'paddleocr':
            self._engine = PaddleOCREngine(self._config.get('paddleocr', {}))
        elif engine == 'wechat_native':
            self._engine = WeChatNativeOCREngine(self._config.get('wechat_ocr_exe', ''))
        else:
            raise ValueError(f"不支持的 OCR 引擎: {engine}")

    # ── 公共接口 ──

    def scan_screen(self, region: Tuple[int, int, int, int] = None) -> List[TextBlock]:
        """
        扫描当前屏幕，返回所有识别到的文字块。

        Args:
            region: 搜索区域 (left, top, width, height)，None 为全屏
        """
        # 检查缓存
        if self._is_cache_valid(region):
            return self._cache.blocks

        # 截屏
        screenshot = pyautogui.screenshot(region=region)
        img_array = np.array(screenshot)

        # OCR 识别
        blocks = self._engine.recognize(img_array)

        # OCR engines return coordinates relative to the cropped image. Expose
        # screen coordinates consistently so callers can click the result.
        if region:
            left, top, _, _ = region
            for block in blocks:
                block.x += left
                block.y += top
                block.box = [
                    [point[0] + left, point[1] + top]
                    for point in block.box
                ]

        # 更新缓存
        self._cache = OCRResult(
            blocks=blocks,
            timestamp=time.time(),
            screen_size=pyautogui.size(),
            region=region,
        )

        logger.debug(f"OCR 扫描完成: {len(blocks)} 个文本块")
        return blocks

    def find_text(self, target: str, exact: bool = False,
                  region: Tuple[int, int, int, int] = None) -> List[TextBlock]:
        """
        查找包含指定文字的所有文本块。

        Args:
            target: 目标文字
            exact: True=精确匹配, False=包含即可

        Returns:
            匹配的文本块列表，按置信度从高到低排序
        """
        blocks = self.scan_screen(region=region)
        matches = []

        for block in blocks:
            if exact:
                if block.text.strip() == target:
                    matches.append(block)
            else:
                if target in block.text:
                    matches.append(block)

        # 按置信度排序
        matches.sort(key=lambda b: b.confidence, reverse=True)
        return matches

    def find_best(self, target: str,
                  region: Tuple[int, int, int, int] = None) -> Optional[TextBlock]:
        """查找最佳匹配（置信度最高）"""
        matches = self.find_text(target, region=region)
        return matches[0] if matches else None

    def click_text(self, target: str) -> bool:
        """
        查找并点击指定文字。

        Returns:
            True 表示找到并点击成功
        """
        best = self.find_best(target)
        if best is None:
            logger.warning(f"未找到文字: '{target}'")
            return False

        logger.info(f"点击 '{target}' → ({best.x}, {best.y}) 置信度={best.confidence:.2f}")
        pyautogui.click(best.x, best.y)
        return True

    def wait_text(self, target: str, timeout: float = 10.0,
                  interval: float = 0.5) -> Optional[TextBlock]:
        """
        等待指定文字出现（用于等待页面加载完成）。

        Args:
            target: 目标文字
            timeout: 超时时间（秒）
            interval: 轮询间隔（秒）

        Returns:
            找到的文本块，超时返回 None
        """
        start = time.time()
        self._invalidate_cache()

        while time.time() - start < timeout:
            best = self.find_best(target)
            if best is not None:
                logger.info(f"等待到 '{target}' 出现 (耗时 {time.time() - start:.1f}s)")
                return best
            time.sleep(interval)

        logger.warning(f"等待 '{target}' 超时 ({timeout}s)")
        return None

    def get_all_text(self) -> List[str]:
        """获取屏幕上所有识别到的文字（用于调试）"""
        blocks = self.scan_screen()
        return [b.text for b in blocks]

    # ── 区域文字扫描（性能优化：只在指定区域搜索） ──

    def find_text_in_region(self, target: str,
                            region: Tuple[int, int, int, int]) -> List[TextBlock]:
        """
        在指定区域内搜索文字。适合已知目标大致位置时使用，比全屏扫描快很多。
        """
        blocks = self.scan_screen(region=region)
        return [b for b in blocks if target in b.text]

    # ── 调试工具 ──

    def dump_screen_text(self) -> str:
        """导出屏幕上所有文字的格式化摘要（调试用）"""
        blocks = self.scan_screen()
        lines = [f"{'─' * 40}"]
        lines.append(f"OCR 屏幕扫描结果 ({len(blocks)} 个文本块)")
        lines.append(f"{'─' * 40}")

        # 按 Y 坐标分组（模拟按行排列）
        sorted_blocks = sorted(blocks, key=lambda b: (b.y // 20, b.x))
        for block in sorted_blocks:
            lines.append(
                f"  [{block.confidence:.2f}] '{block.text}' "
                f"@ ({block.x}, {block.y})"
            )
        return '\n'.join(lines)

    # ── 内部方法 ──

    def _is_cache_valid(self,
                        region: Tuple[int, int, int, int] = None) -> bool:
        """检查缓存是否有效"""
        if self._cache is None:
            return False
        if time.time() - self._cache.timestamp > self._cache_ttl:
            return False
        # 屏幕分辨率变化了
        if self._cache.screen_size != pyautogui.size():
            return False
        if self._cache.region != region:
            return False
        return True

    def _invalidate_cache(self):
        """清除缓存，强制下次重新扫描"""
        self._cache = None
