"""
多锚点相对定位系统 —— 通过已知位置推断未知元素位置。

核心思想：
  有些 UI 元素难以直接定位（如"空的输入框"），但可以通过它们
  与已知锚点的空间关系来推断位置。

  锚点优先级：
    1. OCR 找到的导航栏标签（"聊天""通讯录""朋友圈"）→ 版本完全无关
    2. 特征匹配找到的图标（头像、相机图标）→ 缩放无关
    3. 窗口边框和尺寸 → 用于推算百分比位置

工作流程：
  启动时自动校准 → 扫描界面建立锚点映射 → 后续操作通过锚点推算

Author: 版本无关微信自动化系统
"""

import logging
import time
from typing import Optional, Dict, Tuple, List
from dataclasses import dataclass, field

import pyautogui

logger = logging.getLogger(__name__)


@dataclass
class Anchor:
    """单个锚点"""
    name: str           # 锚点名称
    x: int              # X 坐标
    y: int              # Y 坐标
    source: str         # 定位来源: 'ocr' | 'feature' | 'window' | 'manual'
    confidence: float   # 定位置信度 0-1
    timestamp: float    # 创建时间


@dataclass
class CoordinateMapping:
    """当前版本的完整坐标映射"""
    version_id: str           # 版本标识（微信版本号或界面指纹）
    anchors: Dict[str, Anchor] = field(default_factory=dict)
    window_rect: Tuple[int, int, int, int] = (0, 0, 1920, 1080)
    calibrated_at: float = 0.0

    def get(self, name: str) -> Optional[Anchor]:
        return self.anchors.get(name)

    def relative(self, anchor_name: str, dx: int, dy: int) -> Optional[Tuple[int, int]]:
        """从锚点推算相对位置"""
        anchor = self.anchors.get(anchor_name)
        if anchor is None:
            return None
        return (anchor.x + dx, anchor.y + dy)

    def percentile(self, x_pct: float, y_pct: float) -> Tuple[int, int]:
        """从窗口尺寸推算百分比位置"""
        left, top, right, bottom = self.window_rect
        w = right - left
        h = bottom - top
        return (left + int(w * x_pct), top + int(h * y_pct))


class AnchorCalibrator:
    """
    运行时自动校准器。

    每次启动时扫描微信界面，自动建立当前版本的坐标映射。
    不依赖预设的版本信息，完全自适应。

    使用方式：
        calibrator = AnchorCalibrator(ocr_locator, feature_locator)
        mapping = calibrator.calibrate()
        pos = mapping.relative('nav_moments', dx=30, dy=180)
    """

    def __init__(self, ocr_locator, feature_locator):
        """
        Args:
            ocr_locator: OCRLocator 实例
            feature_locator: FeatureLocator 实例
        """
        self.ocr = ocr_locator
        self.feature = feature_locator
        self._mapping: Optional[CoordinateMapping] = None

    # ── 公共接口 ──

    def calibrate(self, force: bool = False) -> CoordinateMapping:
        """
        执行自动校准。

        扫描微信界面，建立当前版本的坐标映射。
        如果已有有效映射且 force=False，直接返回缓存。

        Returns:
            CoordinateMapping 对象
        """
        if self._mapping is not None and not force:
            # 检查映射是否还新鲜（5 分钟内）
            if time.time() - self._mapping.calibrated_at < 300:
                return self._mapping

        logger.info("开始自动校准...")

        mapping = CoordinateMapping(
            version_id=self._generate_version_id(),
            calibrated_at=time.time(),
        )

        # 第 1 步：获取窗口信息
        self._calibrate_window(mapping)

        # 第 2 步：OCR 扫描导航栏
        self._calibrate_navigation(mapping)

        # 第 3 步：特征匹配定位图标
        self._calibrate_icons(mapping)

        # 第 4 步：进入朋友圈页面，扫描子元素
        self._calibrate_moments_page(mapping)

        # 第 5 步：验证校准质量
        if len(mapping.anchors) < 3:
            logger.warning(f"校准质量低：仅找到 {len(mapping.anchors)} 个锚点")

        self._mapping = mapping
        logger.info(
            f"校准完成：{len(mapping.anchors)} 个锚点, "
            f"窗口={mapping.window_rect}"
        )
        logger.debug(f"锚点详情:\n{self._format_anchors(mapping)}")

        return mapping

    def get_mapping(self) -> Optional[CoordinateMapping]:
        """获取当前坐标映射（可能为 None 表示未校准）"""
        return self._mapping

    def locate_relative(self, anchor_name: str, dx: int, dy: int) -> Optional[Tuple[int, int]]:
        """
        通过锚点推算目标位置。

        Args:
            anchor_name: 锚点名称（如 'nav_moments'）
            dx, dy: 相对偏移（像素）

        Returns:
            (x, y) 或 None（如果锚点不存在）
        """
        mapping = self.get_mapping()
        if mapping is None:
            logger.warning("无坐标映射，无法执行相对定位")
            return None
        return mapping.relative(anchor_name, dx, dy)

    # ── 分步校准 ──

    def _calibrate_window(self, mapping: CoordinateMapping):
        """获取微信窗口尺寸"""
        import win32gui
        hwnd = win32gui.FindWindow("WeChatMainWndForPC", None)
        if hwnd:
            rect = win32gui.GetWindowRect(hwnd)
            mapping.window_rect = rect
            # 添加窗口锚点
            mapping.anchors['window_top_left'] = Anchor(
                name='window_top_left',
                x=rect[0], y=rect[1],
                source='window',
                confidence=1.0,
                timestamp=time.time(),
            )
            mapping.anchors['window_center'] = Anchor(
                name='window_center',
                x=(rect[0] + rect[2]) // 2,
                y=(rect[1] + rect[3]) // 2,
                source='window',
                confidence=1.0,
                timestamp=time.time(),
            )
            logger.debug(f"微信窗口: {rect}")
        else:
            logger.warning("未找到微信窗口，使用全屏作为窗口区域")
            screen_w, screen_h = pyautogui.size()
            mapping.window_rect = (0, 0, screen_w, screen_h)

    def _calibrate_navigation(self, mapping: CoordinateMapping):
        """OCR 扫描微信导航栏，找到所有标签按钮"""
        nav_labels = ['聊天', '通讯录', '朋友圈', '视频号', '小程序', '我']

        for label in nav_labels:
            best = self.ocr.find_best(label)
            if best:
                anchor_name = f'nav_{label}'
                mapping.anchors[anchor_name] = Anchor(
                    name=anchor_name,
                    x=best.x, y=best.y,
                    source='ocr',
                    confidence=best.confidence,
                    timestamp=time.time(),
                )
                logger.debug(f"  导航锚点: {anchor_name} → ({best.x}, {best.y})")

    def _calibrate_icons(self, mapping: CoordinateMapping):
        """特征匹配定位图标锚点"""
        # 这些图标模板需要预先准备（从任意微信版本截取均可）
        icon_targets = {
            'icon_search': 'search_icon.png',
            'icon_add': 'add_icon.png',
        }

        for name, template in icon_targets.items():
            result = self.feature.locate(f"templates/icons/{template}")
            if result:
                mapping.anchors[name] = Anchor(
                    name=name,
                    x=result[0], y=result[1],
                    source='feature',
                    confidence=0.9,
                    timestamp=time.time(),
                )

    def _calibrate_moments_page(self, mapping: CoordinateMapping):
        """
        进入朋友圈页面，扫描子元素锚点。

        注意：这会实际点击"朋友圈"按钮进入。
        如果你不希望校准时改变微信状态，可以将此步骤设为手动触发。
        """
        # 只在有导航锚点时才进入
        if 'nav_朋友圈' not in mapping.anchors:
            logger.debug("无朋友圈导航锚点，跳过页面内锚点扫描")
            return

        # 点击朋友圈，等待页面加载
        nav = mapping.anchors['nav_朋友圈']
        pyautogui.click(nav.x, nav.y)
        time.sleep(1.5)

        # 强制刷新 OCR 缓存
        self.ocr._invalidate_cache()

        # 扫描朋友圈页面内的元素
        page_labels = ['这一刻的想法', '相册', '发表', '所在位置', '谁可以看', '提醒谁看']
        for label in page_labels:
            best = self.ocr.find_best(label)
            if best:
                anchor_name = f'moments_{label}'
                mapping.anchors[anchor_name] = Anchor(
                    name=anchor_name,
                    x=best.x, y=best.y,
                    source='ocr',
                    confidence=best.confidence,
                    timestamp=time.time(),
                )
                logger.debug(f"  朋友圈锚点: {anchor_name} → ({best.x}, {best.y})")

    def _generate_version_id(self) -> str:
        """生成当前版本的唯一标识"""
        try:
            import hashlib
            # 用 OCR 扫描结果的部分文字做指纹
            blocks = self.ocr.scan_screen()
            texts = sorted([b.text for b in blocks[:10]])
            fingerprint = '|'.join(texts)
            hash_id = hashlib.md5(fingerprint.encode()).hexdigest()[:8]
            return f"wechat_{hash_id}"
        except Exception:
            return f"wechat_{int(time.time())}"

    def _format_anchors(self, mapping: CoordinateMapping) -> str:
        """格式化输出所有锚点"""
        lines = []
        for name, anchor in sorted(mapping.anchors.items()):
            lines.append(
                f"    {name:25s} ({anchor.x:4d}, {anchor.y:4d}) "
                f"来源={anchor.source} 置信度={anchor.confidence:.2f}"
            )
        return '\n'.join(lines)
