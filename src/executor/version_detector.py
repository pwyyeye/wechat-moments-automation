"""
微信版本检测器 —— 自动检测版本变化并触发重建。

原理：
  1. 从 WeChat.exe 的 PE 文件头读取 VS_FIXEDFILEINFO 版本号
  2. 与上次校准时记录的版本对比
  3. 版本变化时自动触发：
     - 锚点重新校准
     - 图标模板重建
     - 通知用户

首次运行时，如果 templates/icons/ 为空，自动触发模板扫描。

Author: 版本无关微信自动化系统
"""

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

VERSION_CACHE_FILE = Path(__file__).parent.parent.parent / "state.json"


@dataclass
class WeChatVersion:
    """微信版本信息"""
    major: int
    minor: int
    build: int
    revision: int
    raw: str  # 如 "4.1.5.16"

    def __eq__(self, other):
        if not isinstance(other, WeChatVersion):
            return False
        return (self.major, self.minor, self.build, self.revision) == \
               (other.major, other.minor, other.build, other.revision)

    def __repr__(self):
        return f"WeChat {self.raw}"


class VersionDetector:
    """
    微信版本检测器。

    使用方式：
        detector = VersionDetector()
        version = detector.get_version()

        if detector.is_version_changed():
            print("微信已更新，触发重建...")
            detector.trigger_rebuild(calibrator, ocr_locator)
    """

    def __init__(self, wechat_dir: str = None):
        self._wechat_executable: Optional[Path] = None
        self._version_dir: Optional[Path] = None
        if wechat_dir:
            self._wechat_dir = wechat_dir
        else:
            from .wechat_discovery import discover_from_window, discover_wechat
            env = discover_from_window() or discover_wechat()
            self._wechat_dir = str(env.install_dir) if env else ""
            if env:
                self._wechat_executable = env.executable
                self._version_dir = env.version_dir
        self._last_known_version: Optional[WeChatVersion] = None
        self._load_last_version()

    # ── 公共接口 ──

    def get_version(self) -> Optional[WeChatVersion]:
        """
        从微信 PE 文件读取版本号。

        检测顺序：
          1. 当前运行微信进程的可执行文件
          2. 安装目录和版本目录中的 Weixin.exe / WeChat.exe
          3. WeChatWin.dll / Weixin.dll
        """
        wechat_path = Path(self._wechat_dir)

        search_roots = [wechat_path]
        if self._version_dir and self._version_dir != wechat_path:
            search_roots.append(self._version_dir)

        candidates = []
        if self._wechat_executable:
            candidates.append(self._wechat_executable)
        for root in search_roots:
            candidates.extend([
                root / "Weixin.exe",
                root / "WeChat.exe",
                root / "[WeChat]_x64" / "WeChat.exe",
                root / "[WeChat]" / "WeChat.exe",
            ])

        for exe_path in dict.fromkeys(candidates):
            if exe_path.exists():
                version = self._read_pe_version(str(exe_path))
                if version:
                    return version

        # 备选：尝试从 DLL 读取
        dll_candidates = []
        for root in search_roots:
            dll_candidates.extend([
                root / "Weixin.dll",
                root / "WeChatWin.dll",
                root / "[WeChat]_x64" / "WeChatWin.dll",
            ])
        for dll_path in dict.fromkeys(dll_candidates):
            if dll_path.exists():
                version = self._read_pe_version(str(dll_path))
                if version:
                    return version

        # 回退：扫描版本子目录名
        for root in search_roots:
            version = self._detect_from_dir_name(root)
            if version:
                return version
        return None

    def is_version_changed(self) -> bool:
        """检测微信版本是否与上次不同"""
        current = self.get_version()
        if current is None:
            return False
        if self._last_known_version is None:
            return True  # 首次运行
        return current != self._last_known_version

    def mark_current_version(self):
        """记录当前版本为已知版本"""
        version = self.get_version()
        if version:
            self._last_known_version = version
            self._save_version(version)

    def trigger_rebuild(self, calibrator=None, ocr_locator=None) -> dict:
        """
        版本变化时自动触发重建。

        Returns:
            {'recalibrated': bool, 'templates_scanned': int}
        """
        logger.info("检测到微信版本变化，触发自动重建...")
        result = {'recalibrated': False, 'templates_scanned': 0}

        # 1. 重新校准锚点
        if calibrator:
            try:
                calibrator.calibrate(force=True)
                result['recalibrated'] = True
                logger.info("  锚点重新校准完成")
            except Exception as e:
                logger.error(f"  校准失败: {e}")

        # 2. 重建图标模板
        if ocr_locator:
            try:
                from ..locator.template_extractor import update_all_templates
                count = update_all_templates(ocr_locator)
                result['templates_scanned'] = count
                logger.info(f"  模板重建完成: {count} 个")
            except Exception as e:
                logger.error(f"  模板重建失败: {e}")

        # 3. 记录新版本
        self.mark_current_version()
        return result

    def templates_exist(self) -> bool:
        """检查模板库是否为空"""
        templates_dir = Path(__file__).parent.parent.parent / "templates" / "icons"
        if not templates_dir.exists():
            return False

        png_files = list(templates_dir.glob("*.png"))
        return len(png_files) > 0

    def ensure_templates(self, ocr_locator=None) -> int:
        """
        确保模板库非空。首次运行时自动生成。

        Returns:
            模板数量
        """
        if self.templates_exist():
            templates_dir = Path(__file__).parent.parent.parent / "templates" / "icons"
            return len(list(templates_dir.glob("*.png")))

        logger.info("模板库为空，首次运行自动生成...")
        if ocr_locator:
            from ..locator.template_extractor import update_all_templates
            return update_all_templates(ocr_locator)

        logger.warning("无法自动生成模板（OCR 定位器未初始化）")
        return 0

    # ── PE 版本读取 ──

    def _read_pe_version(self, filepath: str) -> Optional[WeChatVersion]:
        """
        从 Windows PE 文件的 VS_FIXEDFILEINFO 读取版本号。

        方法 1：用 pefile 库解析（推荐）
        方法 2：用 PowerShell 命令获取（备选）
        """
        # 方法 1: pefile
        try:
            import pefile
            pe = pefile.PE(filepath)
            if hasattr(pe, 'VS_FIXEDFILEINFO'):
                ff = pe.VS_FIXEDFILEINFO[0] if isinstance(
                    pe.VS_FIXEDFILEINFO, list
                ) else pe.VS_FIXEDFILEINFO

                version = WeChatVersion(
                    major=ff.FileVersionMS >> 16,
                    minor=ff.FileVersionMS & 0xFFFF,
                    build=ff.FileVersionLS >> 16,
                    revision=ff.FileVersionLS & 0xFFFF,
                    raw=f"{ff.FileVersionMS >> 16}.{ff.FileVersionMS & 0xFFFF}."
                        f"{ff.FileVersionLS >> 16}.{ff.FileVersionLS & 0xFFFF}",
                )
                logger.debug(f"PE 版本: {version.raw} ({filepath})")
                return version
        except ImportError:
            pass
        except Exception as e:
            logger.debug(f"pefile 解析失败: {e}")

        # 方法 2: PowerShell
        return self._read_version_via_powershell(filepath)

    def _read_version_via_powershell(self, filepath: str) -> Optional[WeChatVersion]:
        """通过 PowerShell 获取文件版本"""
        import subprocess

        try:
            result = subprocess.run(
                [
                    'powershell', '-Command',
                    f'(Get-Item "{filepath}").VersionInfo.FileVersion'
                ],
                capture_output=True, text=True, timeout=10,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            version_str = result.stdout.strip()
            if version_str:
                parts = version_str.split('.')
                if len(parts) >= 4:
                    return WeChatVersion(
                        major=int(parts[0]),
                        minor=int(parts[1]),
                        build=int(parts[2]),
                        revision=int(parts[3]),
                        raw=version_str,
                    )
                elif len(parts) >= 3:
                    return WeChatVersion(
                        major=int(parts[0]),
                        minor=int(parts[1]),
                        build=int(parts[2]),
                        revision=0,
                        raw=version_str,
                    )
        except Exception as e:
            logger.debug(f"PowerShell 版本读取失败: {e}")

        return None

    def _detect_from_dir_name(self, wechat_path: Path) -> Optional[WeChatVersion]:
        """从版本子目录名检测版本"""
        import re

        if not wechat_path.exists() or not wechat_path.is_dir():
            return None
        for d in wechat_path.iterdir():
            if d.is_dir():
                match = re.search(r'(\d+)\.(\d+)\.(\d+)\.(\d+)', d.name)
                if match:
                    return WeChatVersion(
                        major=int(match.group(1)),
                        minor=int(match.group(2)),
                        build=int(match.group(3)),
                        revision=int(match.group(4)),
                        raw=f"{match.group(1)}.{match.group(2)}.{match.group(3)}.{match.group(4)}",
                    )
        return None

    # ── 持久化 ──

    def _save_version(self, version: WeChatVersion):
        """保存版本到缓存文件"""
        try:
            cache = {}
            if VERSION_CACHE_FILE.exists():
                cache = json.loads(VERSION_CACHE_FILE.read_text(encoding='utf-8'))

            cache['wechat_version'] = {
                'raw': version.raw,
                'detected_at': __import__('datetime').datetime.now().isoformat(),
            }

            VERSION_CACHE_FILE.write_text(
                json.dumps(cache, ensure_ascii=False, indent=2),
                encoding='utf-8',
            )
        except Exception as e:
            logger.debug(f"版本缓存写入失败: {e}")

    def _load_last_version(self):
        """从缓存文件加载上次版本"""
        try:
            if VERSION_CACHE_FILE.exists():
                cache = json.loads(VERSION_CACHE_FILE.read_text(encoding='utf-8'))
                v = cache.get('wechat_version', {})
                raw = v.get('raw', '')
                if raw:
                    parts = raw.split('.')
                    if len(parts) >= 4:
                        self._last_known_version = WeChatVersion(
                            major=int(parts[0]), minor=int(parts[1]),
                            build=int(parts[2]), revision=int(parts[3]),
                            raw=raw,
                        )
        except Exception:
            self._last_known_version = None
