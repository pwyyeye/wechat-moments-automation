"""
微信环境自动发现 — 从运行进程检测，零硬编码路径。

完全替代所有硬编码的 C:\\Program Files\\... 路径。
所有路径都从正在运行的微信进程中动态获取。

Author: 版本无关微信自动化系统
"""

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

import win32api
import win32con
import win32gui
import win32process

logger = logging.getLogger(__name__)

# 可能的微信进程名（按版本）
WECHAT_PROCESS_NAMES = ["Weixin.exe", "WeChat.exe"]


@dataclass
class WeChatEnvironment:
    """自动发现的微信运行环境"""
    process_name: str           # Weixin.exe / WeChat.exe
    process_id: int             # 进程 PID
    install_dir: Path           # 安装目录 (如 C:\Program Files\Tencent\Weixin)
    version_dir: Path           # 版本目录 (如 .../4.1.10.31)
    executable: Path            # 主程序路径
    ocr_binary: Optional[Path]  # OCR 程序 (WeChatOcr.bin / WeChatOCR.bin)
    ocr_dll: Optional[Path]     # Mojo DLL (mmmojo_64.dll / mmmojo.dll)
    is_64bit: bool              # 是否 64 位

    @property
    def ocr_available(self) -> bool:
        return self.ocr_binary is not None and self.ocr_dll is not None


def discover_wechat(hwnd: int = None, pid: int = None) -> Optional[WeChatEnvironment]:
    """
    自动发现微信运行环境。

    优先级:
      1. 指定 hwnd → 从窗口获取进程
      2. 指定 pid → 从 PID 获取
      3. 自动扫描运行中的微信进程

    返回 None 表示未找到微信。
    """
    process = None

    # 从窗口句柄获取进程
    if hwnd and win32gui.IsWindow(hwnd):
        _, pid_from_hwnd = win32process.GetWindowThreadProcessId(hwnd)
        if pid_from_hwnd:
            pid = pid_from_hwnd

    # 从 PID 获取进程
    if pid:
        try:
            handle = win32api.OpenProcess(
                win32con.PROCESS_QUERY_INFORMATION | win32con.PROCESS_VM_READ,
                False, pid
            )
            process = {
                'pid': pid,
                'handle': handle,
                'name': _get_process_name(pid),
            }
        except Exception:
            pass

    # 自动扫描
    if process is None:
        processes = _find_wechat_processes()
        if not processes:
            logger.warning("未找到运行中的微信进程")
            return None
        process = processes[0]

    # 获取可执行文件路径
    exe_path = _get_process_exe_path(process['pid'])
    if not exe_path or not Path(exe_path).exists():
        logger.warning(f"无法获取进程 {process['pid']} 的可执行文件路径")
        return None

    exe = Path(exe_path)
    install_dir = exe.parent          # Weixin.exe 所在目录

    # 在安装目录下找版本子目录（4.1.10.31 或 [3.9.5.81] 格式）
    version_dir = install_dir  # 默认 = 安装目录
    for d in sorted(install_dir.iterdir(), reverse=True):
        if d.is_dir() and d.name != '..':
            # 版本目录特征：包含 Weixin.dll 或大量 DLL/OCR 文件
            has_features = any(
                (d / f).exists() for f in
                ['Weixin.dll', 'WeChatWin.dll', 'WeChatOcr.bin', 'WeChatOCR.bin', 'mmmojo_64.dll']
            )
            if has_features:
                version_dir = d
                break

    # 在版本目录中找 OCR
    ocr_bin = _find_in_dir(version_dir,
                          ['WeChatOcr.bin', 'WeChatOCR.bin', 'WeChatOCR.exe', 'wxocr.dll'])
    ocr_dll = _find_in_dir(version_dir,
                          ['mmmojo_64.dll', 'mmmojo.dll'])

    # 如果版本目录没找到，回退到安装目录搜索
    if not ocr_bin:
        for d in install_dir.iterdir():
            if d.is_dir():
                ocr_bin = _find_in_dir(d, ['WeChatOcr.bin', 'WeChatOCR.bin', 'WeChatOCR.exe'])
                if ocr_bin:
                    version_dir = d
                    break

    env = WeChatEnvironment(
        process_name=process['name'],
        process_id=process['pid'],
        install_dir=install_dir,
        version_dir=version_dir,
        executable=exe,
        ocr_binary=ocr_bin,
        ocr_dll=ocr_dll,
        is_64bit='64' in str(ocr_dll or '') or exe_path.lower().endswith('weixin.exe'),
    )

    logger.info(
        f"微信环境已发现: {env.process_name} (PID={env.process_id})\n"
        f"  安装: {env.install_dir}\n"
        f"  版本: {env.version_dir.name}\n"
        f"  OCR: {'✅' if env.ocr_available else '❌'} "
        f"({env.ocr_binary.name if env.ocr_binary else '无'})"
    )

    return env


def discover_from_window() -> Optional[WeChatEnvironment]:
    """从当前活跃的微信窗口自动发现"""
    windows = _find_wechat_windows()
    if not windows:
        return None
    hwnd, _ = windows[0]
    return discover_wechat(hwnd=hwnd)


# ═══════════════════════════════════════════════════════════════
# 内部查找函数
# ═══════════════════════════════════════════════════════════════

def _find_wechat_processes() -> List[dict]:
    """查找运行中的微信进程"""
    import ctypes
    from ctypes import wintypes

    psapi = ctypes.windll.psapi

    processes = []
    process_ids = (wintypes.DWORD * 1024)()
    bytes_returned = wintypes.DWORD()

    if psapi.EnumProcesses(ctypes.byref(process_ids), ctypes.sizeof(process_ids),
                           ctypes.byref(bytes_returned)):
        count = bytes_returned.value // 4
        for i in range(count):
            pid = process_ids[i]
            if pid == 0:
                continue
            name = _get_process_name(pid)
            if name and name in WECHAT_PROCESS_NAMES:
                processes.append({'pid': pid, 'name': name})

    if not processes:
        # 回退: 枚举窗口找微信进程
        pids = set()
        for hwnd, _ in _find_wechat_windows():
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            if pid:
                name = _get_process_name(pid)
                if name in WECHAT_PROCESS_NAMES:
                    if pid not in pids:
                        pids.add(pid)
                        processes.append({'pid': pid, 'name': name})

    return processes


def _find_wechat_windows() -> List[Tuple[int, str]]:
    """查找微信窗口"""
    windows = []

    def callback(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return
        cls = win32gui.GetClassName(hwnd)
        title = win32gui.GetWindowText(hwnd)
        # Qt 版本
        if cls.startswith('Qt') and title == '微信':
            windows.append((hwnd, title))
        # 传统Win32版本
        elif cls == 'WeChatMainWndForPC':
            windows.append((hwnd, title or '微信'))

    win32gui.EnumWindows(callback, None)
    return windows


def _get_process_name(pid: int) -> Optional[str]:
    """获取进程名称"""
    path = _query_process_exe_path(pid)
    if path:
        return Path(path).name

    try:
        handle = win32api.OpenProcess(
            win32con.PROCESS_QUERY_INFORMATION | win32con.PROCESS_VM_READ,
            False, pid
        )
        path = win32process.GetModuleFileNameEx(handle, 0)
        win32api.CloseHandle(handle)
        return Path(path).name
    except Exception:
        pass
    return None


def _get_process_exe_path(pid: int) -> Optional[str]:
    """获取进程的可执行文件完整路径"""
    path = _query_process_exe_path(pid)
    if path:
        return path

    try:
        handle = win32api.OpenProcess(
            win32con.PROCESS_QUERY_INFORMATION | win32con.PROCESS_VM_READ,
            False, pid
        )
        path = win32process.GetModuleFileNameEx(handle, 0)
        win32api.CloseHandle(handle)
        return path
    except Exception:
        pass

    # 回退: wmi / powershell
    try:
        import subprocess
        r = subprocess.run(
            ['powershell', '-Command',
             f'(Get-Process -Id {pid}).Path'],
            capture_output=True, text=True, timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW
        )
        path = r.stdout.strip()
        if path and Path(path).exists():
            return path
    except Exception:
        pass

    return None


def _query_process_exe_path(pid: int) -> Optional[str]:
    """Read a process path without spawning tasklist or another console process."""
    import ctypes
    from ctypes import wintypes

    process_query_limited_information = 0x1000
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = (
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    )
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL

    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        return None
    try:
        buffer = ctypes.create_unicode_buffer(32768)
        size = wintypes.DWORD(len(buffer))
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return None
        return buffer.value
    finally:
        kernel32.CloseHandle(handle)


def _find_in_dir(directory: Path, candidates: List[str]) -> Optional[Path]:
    """在目录中查找第一个存在的候选文件"""
    for name in candidates:
        fp = directory / name
        if fp.exists():
            return fp
    return None


# ═══════════════════════════════════════════════════════════════
# 便捷函数：供其他模块使用
# ═══════════════════════════════════════════════════════════════

def get_wechat_ocr_paths() -> Tuple[Optional[str], Optional[str]]:
    """
    获取微信 OCR 的路径，无需任何硬编码。

    Returns:
        (ocr_binary_path, mmmojo_dll_path)
    """
    env = discover_from_window()
    if env and env.ocr_available:
        return str(env.ocr_binary), str(env.ocr_dll)
    return None, None


def get_wechat_version() -> Optional[str]:
    """获取当前运行的微信版本号"""
    env = discover_from_window()
    if env:
        return env.version_dir.name
    return None
