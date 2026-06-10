"""
运行时图标模板自动提取器 —— 替代手工截图，从微信界面自动捕获 UI 元素。

核心思路：
  微信 4.x 使用自绘 UI（Skia/DirectUI），DLL 资源里没有现成的按钮位图。
  因此不走"从程序文件提取资源"的路线，而是在运行时从屏幕直接截取。

三步自动提取流程：
  1. OCR 扫描界面 → 找到所有带文字标签的 UI 元素
  2. 边缘检测/轮廓提取 → 精确裁剪到图标/按钮的边界
  3. 保存为模板 PNG → 供 SIFT/ORB 特征匹配使用

每次微信版本更新后运行一次，即可自动更新模板库。

Author: 版本无关微信自动化系统
"""

import logging
import time
from typing import Optional, List, Tuple, Dict
from dataclasses import dataclass, field
from pathlib import Path
from datetime import datetime

import cv2
import numpy as np
import pyautogui
from PIL import Image

logger = logging.getLogger(__name__)


@dataclass
class ExtractedTemplate:
    """提取出的模板"""
    name: str                              # 模板名称
    filepath: str                          # 保存路径
    x: int                                 # 屏幕坐标 X
    y: int                                 # 屏幕坐标 Y
    width: int                             # 宽度
    height: int                            # 高度
    source: str                            # 提取来源: 'ocr_boundary' | 'contour' | 'manual_region'
    confidence: float                      # 提取置信度
    preview_path: str = ""                 # 预览图路径


class TemplateExtractor:
    """
    运行时图标模板自动提取器。

    使用方式：
        extractor = TemplateExtractor(ocr_locator)
        templates = extractor.extract_all(region="moments_page")
        # templates/ 目录下自动生成所有 UI 元素模板
    """

    def __init__(self, ocr_locator, output_dir: str = "templates/icons"):
        self._ocr = ocr_locator
        self._output_dir = Path(output_dir)
        self._output_dir.mkdir(parents=True, exist_ok=True)

    # ══════════════════════════════════════════════════════════
    # 公共接口
    # ══════════════════════════════════════════════════════════

    def extract_by_text(self, text_labels: List[str],
                        padding: int = 8,
                        method: str = 'contour'
                        ) -> List[ExtractedTemplate]:
        """
        根据文字标签提取 UI 元素模板。

        这是最常用的方法：告诉它按钮上写的什么字，它自动找到按钮并截图。

        Args:
            text_labels: 文字标签列表，如 ['朋友圈', '发表', '相册']
            padding: 模板四周留白 (px)
            method: 裁剪方法 'contour'=边缘检测精确裁剪 | 'fixed'=固定范围

        Returns:
            提取到的模板列表
        """
        results = []

        for label in text_labels:
            logger.info(f"🔍 提取模板: '{label}'")

            # 第一步：OCR 定位文字
            block = self._ocr.find_best(label)
            if block is None:
                logger.warning(f"  未找到 '{label}'，跳过")
                continue

            # 第二步：截图文字所在区域
            if method == 'contour':
                template = self._extract_by_contour(block, padding)
            else:
                template = self._extract_by_fixed_size(block, padding)

            if template:
                results.append(template)
                logger.info(
                    f"  ✅ {template.name} → {template.filepath} "
                    f"({template.width}×{template.height})"
                )

        return results

    def extract_all_moments_ui(self) -> Dict[str, ExtractedTemplate]:
        """
        一次性提取朋友圈页面所有 UI 元素模板。

        自动进入朋友圈页面，扫描所有按钮/图标/输入框，
        然后返回导航栏重新扫描。
        """
        all_templates = {}

        # ── 朋友圈页面元素 ──
        moments_labels = [
            '这一刻的想法',   # 文字输入框
            '相册',           # 添加图片按钮
            '所在位置',       # 位置按钮
            '谁可以看',       # 权限按钮
            '提醒谁看',       # @提醒按钮
            '发表',           # 发布按钮
            '已发送',         # 成功提示（验证用）
        ]

        logger.info("提取朋友圈页面元素...")
        for label in moments_labels:
            templates = self.extract_by_text([label])
            for t in templates:
                all_templates[t.name] = t

        return all_templates

    def extract_navigation_icons(self) -> Dict[str, ExtractedTemplate]:
        """
        提取导航栏图标模板。

        微信顶部/侧边导航栏的图标（聊天、通讯录、朋友圈等）
        通常带有文字标签，直接用 OCR 定位 + 轮廓裁剪。
        """
        nav_labels = ['聊天', '通讯录', '朋友圈', '视频号', '小程序', '我']

        logger.info("提取导航栏元素...")
        results = {}
        for label in nav_labels:
            templates = self.extract_by_text([label], padding=4)
            for t in templates:
                # 保存时使用统一命名规则
                name = f"nav_{label}"
                t.name = name
                results[name] = t

        return results

    def extract_icon_only(self, region: Tuple[int, int, int, int],
                          name: str) -> Optional[ExtractedTemplate]:
        """
        提取纯图标元素（没有文字标签的）。

        对于 OCR 无法定位的纯图标（如朋友圈相机图标），
        需要手动指定大致区域，然后用边缘检测精确裁剪。

        Args:
            region: (x, y, w, h) 大致区域
            name: 模板名称
        """
        x, y, w, h = region
        screenshot = pyautogui.screenshot(region=(x-20, y-20, w+40, h+40))
        img = cv2.cvtColor(np.array(screenshot), cv2.COLOR_RGB2BGR)

        # 边缘检测找图标边界
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        blurred = cv2.GaussianBlur(gray, (5, 5), 0)
        edged = cv2.Canny(blurred, 30, 100)

        # 找轮廓
        contours, _ = cv2.findContours(edged, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if not contours:
            logger.warning(f"未找到图标轮廓: {name}")
            return None

        # 取最大轮廓
        largest = max(contours, key=cv2.contourArea)
        rx, ry, rw, rh = cv2.boundingRect(largest)

        # 裁剪
        icon = img[ry:ry+rh, rx:rx+rw]
        filepath = self._output_dir / f"{name}.png"
        cv2.imwrite(str(filepath), icon)

        return ExtractedTemplate(
            name=name,
            filepath=str(filepath),
            x=x + rx, y=y + ry,
            width=rw, height=rh,
            source='contour',
            confidence=0.8,
        )

    # ══════════════════════════════════════════════════════════
    # 核心算法
    # ══════════════════════════════════════════════════════════

    def _extract_by_contour(self, block, padding: int) -> Optional[ExtractedTemplate]:
        """
        通过边缘检测精确裁剪按钮/图标边界。

        算法：
          1. 在 OCR 定位的文字周围取一块较大的区域
          2. 对区域做 Canny 边缘检测
          3. 找最大轮廓 → 确定按钮边界
          4. 裁剪保存
        """
        # 扩大搜索区域（按钮通常比文字大）
        search_margin = 30
        x1 = max(0, block.x - block.width // 2 - search_margin)
        y1 = max(0, block.y - block.height // 2 - search_margin)
        x2 = block.x + block.width // 2 + search_margin
        y2 = block.y + block.height // 2 + search_margin

        # 截屏
        screenshot = pyautogui.screenshot(region=(x1, y1, x2 - x1, y2 - y1))
        img_bgr = cv2.cvtColor(np.array(screenshot), cv2.COLOR_RGB2BGR)
        img_gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)

        # 自适应阈值二值化（处理不同主题）
        binary = cv2.adaptiveThreshold(
            img_gray, 255,
            cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
            cv2.THRESH_BINARY_INV,  # 反转：按钮通常是亮的，背景暗
            15,  # 块大小
            5,   # 常数减量
        )

        # 形态学闭运算（连接断裂的边框）
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5))
        closed = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, kernel)

        # 找轮廓
        contours, _ = cv2.findContours(closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        if not contours:
            # 回退：用固定尺寸（按钮通常 60×30 到 120×40）
            logger.debug(f"  '{block.text}' 边缘检测无轮廓，使用固定尺寸")
            return self._extract_by_fixed_size(block, padding)

        # 筛选：取面积最大、且大致在文字附近的轮廓
        valid_contours = []
        for cnt in contours:
            rx, ry, rw, rh = cv2.boundingRect(cnt)
            area = rw * rh
            # 过滤太小或太大的
            if area < 100 or area > 50000:
                continue
            # 过滤太扁的（可能是横线）
            if rh < 8:
                continue
            valid_contours.append((area, rx, ry, rw, rh))

        if not valid_contours:
            return self._extract_by_fixed_size(block, padding)

        # 取最接近文字中心的轮廓
        text_local_x = block.x - x1
        text_local_y = block.y - y1
        best = min(valid_contours, key=lambda c:
                   abs((c[1] + c[3] // 2) - text_local_x) +
                   abs((c[2] + c[4] // 2) - text_local_y)
                   )
        area, rx, ry, rw, rh = best

        # 加 padding
        rx = max(0, rx - padding)
        ry = max(0, ry - padding)
        rw = min(img_bgr.shape[1] - rx, rw + 2 * padding)
        rh = min(img_bgr.shape[0] - ry, rh + 2 * padding)

        # 裁剪保存
        icon = img_bgr[ry:ry+rh, rx:rx+rw]
        safe_name = "".join(c for c in block.text if c.isalnum() or c in '._-')
        filepath = self._output_dir / f"{safe_name}.png"
        cv2.imwrite(str(filepath), icon)

        return ExtractedTemplate(
            name=safe_name,
            filepath=str(filepath),
            x=x1 + rx, y=y1 + ry,
            width=rw, height=rh,
            source='contour',
            confidence=block.confidence,
        )

    def _extract_by_fixed_size(self, block, padding: int) -> Optional[ExtractedTemplate]:
        """
        用固定尺寸截取（当边缘检测找不到轮廓时的回退方案）。

        假设按钮大小与文字大小成正比：
          button_width ≈ text_width * 1.8 + 20
          button_height ≈ text_height * 1.5 + 12
        """
        bw = int(block.width * 1.8 + 20)
        bh = int(block.height * 1.5 + 12)

        x1 = max(0, block.x - bw // 2 - padding)
        y1 = max(0, block.y - bh // 2 - padding)
        x2 = block.x + bw // 2 + padding
        y2 = block.y + bh // 2 + padding

        screenshot = pyautogui.screenshot(region=(x1, y1, x2 - x1, y2 - y1))
        img_bgr = cv2.cvtColor(np.array(screenshot), cv2.COLOR_RGB2BGR)

        safe_name = "".join(c for c in block.text if c.isalnum() or c in '._-')
        filepath = self._output_dir / f"{safe_name}.png"
        cv2.imwrite(str(filepath), img_bgr)

        return ExtractedTemplate(
            name=safe_name,
            filepath=str(filepath),
            x=x1, y=y1,
            width=x2 - x1, height=y2 - y1,
            source='manual_region',
            confidence=block.confidence * 0.8,  # 降级置信度
        )


# ═══════════════════════════════════════════════════════════════
# 便捷函数：一键更新模板库
# ═══════════════════════════════════════════════════════════════

def update_all_templates(ocr_locator,
                         output_dir: str = "templates/icons",
                         capture_navigation: bool = True,
                         capture_moments: bool = True) -> int:
    """
    一键更新所有模板库。

    微信版本更新后运行此函数，自动重新截取所有 UI 元素。

    Returns:
        成功提取的模板数量
    """
    extractor = TemplateExtractor(ocr_locator, output_dir)
    total = 0

    if capture_navigation:
        logger.info("=" * 50)
        logger.info("提取导航栏模板...")
        nav_templates = extractor.extract_navigation_icons()
        total += len(nav_templates)

    if capture_moments:
        logger.info("=" * 50)
        logger.info("提取朋友圈页面模板...")
        moments_templates = extractor.extract_all_moments_ui()
        total += len(moments_templates)

    logger.info(f"模板更新完成：共 {total} 个")
    return total
