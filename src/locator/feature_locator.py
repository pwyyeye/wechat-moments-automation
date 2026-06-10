"""
特征点匹配定位器 —— 通过图形语义定位纯图标元素。

SIFT/ORB 特征点匹配 vs 传统模板匹配：
  ┌──────────────┬─────────────────────┬──────────────────────┐
  │   特性       │  模板匹配            │  特征点匹配 (本模块)  │
  ├──────────────┼─────────────────────┼──────────────────────┤
  │ DPI 缩放     │  ❌ 失效             │  ✅ SIFT 尺度不变     │
  │ 轻微旋转     │  ❌ 失效             │  ✅ 旋转不变          │
  │ 光照/主题    │  ❌ 敏感             │  ✅ 对亮度和对比度不敏感│
  │ 部分遮挡     │  ❌ 失效             │  ✅ RANSAC 容错       │
  │ 速度(1920×1080)│ ⚡ 50-200ms        │ 🐢 200-500ms         │
  └──────────────┴─────────────────────┴──────────────────────┘

使用场景：
  - 朋友圈"相机"图标（纯图标，OCR 找不到）
  - 添加图片的"+"按钮
  - 用户头像等图形元素

Author: 版本无关微信自动化系统
"""

import logging
import time
from typing import Optional, List, Tuple
from pathlib import Path

import cv2
import numpy as np
import pyautogui

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# ORB 特征匹配器（推荐：开源、快速、免专利）
# ═══════════════════════════════════════════════════════════════

class ORBMatcher:
    """ORB (Oriented FAST and Rotated BRIEF) 特征匹配器"""

    def __init__(self, n_features: int = 2000):
        self.detector = cv2.ORB_create(nfeatures=n_features)
        # BFMatcher + Hamming 距离（ORB 输出二进制描述符）
        self.matcher = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=False)

    def detect_and_compute(self, image: np.ndarray):
        """提取关键点和描述符"""
        if len(image.shape) == 3:
            image = cv2.cvtColor(image, cv2.COLOR_RGB2GRAY)
        return self.detector.detectAndCompute(image, None)

    def match(self, des1, des2) -> list:
        """特征匹配 + Lowe's ratio test"""
        if des1 is None or des2 is None or len(des1) < 2 or len(des2) < 2:
            return []

        matches = self.matcher.knnMatch(des1, des2, k=2)

        # Lowe's ratio test
        good = []
        for pair in matches:
            if len(pair) == 2:
                m, n = pair
                if m.distance < 0.65 * n.distance:
                    good.append(m)
        return good


# ═══════════════════════════════════════════════════════════════
# SIFT 特征匹配器（备选：精度更高，但需编译 OpenCV contrib）
# ═══════════════════════════════════════════════════════════════

class SIFTMatcher:
    """SIFT (Scale-Invariant Feature Transform) 特征匹配器"""

    def __init__(self):
        try:
            self.detector = cv2.SIFT_create()
        except AttributeError:
            raise ImportError(
                "SIFT 需要 opencv-contrib-python。\n"
                "安装：pip uninstall opencv-python && pip install opencv-contrib-python\n"
                "或使用 ORB 替代：FeatureLocator(algorithm='orb')"
            )
        # FLANN 匹配器（对 SIFT float 描述符更快）
        self.matcher = cv2.FlannBasedMatcher(
            dict(algorithm=1, trees=5),   # KD-tree
            dict(checks=50),
        )

    def detect_and_compute(self, image: np.ndarray):
        """提取关键点和描述符"""
        if len(image.shape) == 3:
            image = cv2.cvtColor(image, cv2.COLOR_RGB2GRAY)
        return self.detector.detectAndCompute(image, None)

    def match(self, des1, des2) -> list:
        """特征匹配 + Lowe's ratio test"""
        if des1 is None or des2 is None or len(des1) < 2 or len(des2) < 2:
            return []

        matches = self.matcher.knnMatch(des1, des2, k=2)

        good = []
        for pair in matches:
            if len(pair) == 2:
                m, n = pair
                if m.distance < 0.65 * n.distance:
                    good.append(m)
        return good


# ═══════════════════════════════════════════════════════════════
# 特征点定位器主类
# ═══════════════════════════════════════════════════════════════

class FeatureLocator:
    """
    特征点匹配定位器。

    使用方式：
        locator = FeatureLocator(algorithm='orb')
        pos = locator.locate("templates/icons/camera_icon.png")
        if pos:
            pyautogui.click(pos)
    """

    def __init__(self, algorithm: str = 'orb', config: dict = None):
        """
        Args:
            algorithm: 'orb' | 'sift'
            config: 配置字典
        """
        self._config = config or {}

        if algorithm == 'orb':
            n_features = self._config.get('orb_features', 2000)
            self._matcher = ORBMatcher(n_features=n_features)
        elif algorithm == 'sift':
            self._matcher = SIFTMatcher()
        else:
            raise ValueError(f"不支持的特征匹配算法: {algorithm}")

        self._min_matches = self._config.get('min_good_matches', 10)
        self._ransac_threshold = self._config.get('ransac_threshold', 5.0)
        self._lowe_ratio = self._config.get('lowe_ratio', 0.65)

    # ── 公共接口 ──

    def locate(self, template_path: str,
               screenshot: np.ndarray = None,
               region: Tuple[int, int, int, int] = None
               ) -> Optional[Tuple[int, int]]:
        """
        在屏幕上定位模板图标。

        Args:
            template_path: 图标模板文件路径
            screenshot: 预截取的屏幕图像（None 则自动截图）
            region: 搜索区域 (left, top, width, height)

        Returns:
            (center_x, center_y) 或 None
        """
        template_path = str(Path(template_path).resolve())
        template = cv2.imread(template_path)
        if template is None:
            logger.error(f"无法加载模板: {template_path}")
            return None

        # 获取屏幕截图
        if screenshot is None:
            screen = pyautogui.screenshot(region=region)
            screen = cv2.cvtColor(np.array(screen), cv2.COLOR_RGB2BGR)
        else:
            if region:
                x, y, w, h = region
                screen = screenshot[y:y+h, x:x+w]
            else:
                screen = screenshot

        # 提取特征点
        kp1, des1 = self._matcher.detect_and_compute(template)
        kp2, des2 = self._matcher.detect_and_compute(screen)

        # 特征匹配
        good_matches = self._matcher.match(des1, des2)

        logger.debug(f"模板 {Path(template_path).name}: "
                     f"特征点={len(kp1) if kp1 else 0}/{len(kp2) if kp2 else 0}, "
                     f"优质匹配={len(good_matches)}")

        if len(good_matches) < self._min_matches:
            logger.debug(f"优质匹配不足: {len(good_matches)} < {self._min_matches}")
            return None

        # RANSAC 计算单应性矩阵
        src_pts = np.float32([kp1[m.queryIdx].pt for m in good_matches]).reshape(-1, 1, 2)
        dst_pts = np.float32([kp2[m.trainIdx].pt for m in good_matches]).reshape(-1, 1, 2)

        M, mask = cv2.findHomography(src_pts, dst_pts, cv2.RANSAC, self._ransac_threshold)

        if M is None:
            logger.debug("RANSAC 单应性矩阵计算失败")
            return None

        # 通过单应性矩阵计算模板在屏幕上的位置
        h, w = template.shape[:2]
        corners = np.float32([[0, 0], [0, h-1], [w-1, h-1], [w-1, 0]]).reshape(-1, 1, 2)
        dst = cv2.perspectiveTransform(corners, M)

        center_x = int(np.mean(dst[:, 0, 0]))
        center_y = int(np.mean(dst[:, 0, 1]))

        # 加上区域偏移
        if region:
            center_x += region[0]
            center_y += region[1]

        inlier_count = np.sum(mask) if mask is not None else 0
        logger.info(f"特征匹配成功: {Path(template_path).name} → "
                    f"({center_x}, {center_y}), inliers={inlier_count}")

        return (center_x, center_y)

    def locate_best(self, template_paths: List[str],
                    screenshot: np.ndarray = None
                    ) -> Optional[Tuple[str, int, int]]:
        """
        从多个候选模板中找最佳匹配。

        Args:
            template_paths: 候选模板路径列表（同元素的不同样式）

        Returns:
            (template_path, x, y) 或 None
        """
        best = None
        best_inliers = 0

        for path in template_paths:
            result = self.locate(path, screenshot=screenshot)
            if result:
                # 这里我们简单取第一个成功的（inlier 计数在 locate 内部）
                # 如需更精细的比较，可扩展 locate 返回 inlier 数量
                return (path, result[0], result[1])

        return None

    def is_visible(self, template_path: str, screenshot: np.ndarray = None) -> bool:
        """检查图标是否在屏幕上可见"""
        return self.locate(template_path, screenshot=screenshot) is not None
