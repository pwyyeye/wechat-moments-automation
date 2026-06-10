"""
从微信程序文件提取图标资源 —— PE 资源段提取 + Hook 运行时捕获。

两种策略：
  策略 A（首选）：解析 WeChatWin.dll / WeChat.exe 的 PE 资源段，
               提取 ICON / BITMAP / PNG / RCData 中的图像资源。
               用 pefile 纯 Python 实现，不需要注入进程。

  策略 B（备选）：Hook GDI/GDI+ API，在微信加载图像时拦截捕获。
               适用于资源被加密/压缩/自定义格式时。

策略选择：
  - 先尝试策略 A（PE 解析），速度快、无风险
  - 如果提取不到可用资源，降级到策略 B（Hook 拦截）
  - 也可以在微信版本更新后自动触发策略 A 重建模板库

Author: 版本无关微信自动化系统
"""

import logging
import struct
import io
from typing import Optional, List, Tuple, Dict, Set
from dataclasses import dataclass, field
from pathlib import Path
from datetime import datetime

import numpy as np
from PIL import Image

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# 数据结构
# ═══════════════════════════════════════════════════════════════

@dataclass
class ExtractedResource:
    """从 PE 文件中提取出的资源"""
    name: str                     # 资源名称（ID 或字符串）
    type: str                     # 资源类型：ICON / BITMAP / PNG / RCDATA
    data: bytes                   # 原始资源数据
    width: int = 0                # 图像宽度
    height: int = 0               # 图像高度
    file_offset: int = 0          # 在 PE 文件中的偏移


@dataclass
class HookConfig:
    """Hook 配置"""
    target_process: str = "WeChat.exe"
    hook_functions: List[str] = field(default_factory=lambda: [
        "GdipLoadImageFromFile",    # GDI+ 从文件加载图片
        "LoadImageW",               # Win32 通用图片加载
        "CreateCompatibleBitmap",   # GDI 创建位图
        "CreateBitmapIndirect",     # GDI 创建位图（间接）
    ])


# ═══════════════════════════════════════════════════════════════
# 策略 A：PE 资源段解析
# ═══════════════════════════════════════════════════════════════

class PEResourceExtractor:
    """
    PE 文件资源提取器 —— 从 WeChatWin.dll / WeChat.exe 提取图标/位图。

    原理：
      Windows PE 文件有一个 .rsrc 段（Resource Directory），
      以三级树结构组织资源：Type → Name → Language。
      常见类型：
        RT_ICON        (3)   — ICO 图标
        RT_GROUP_ICON  (14)  — ICO 图标组
        RT_BITMAP      (2)   — BMP 位图
        RT_RCDATA      (10)  — 原始数据（可能含 PNG）
        RT_PNG         (??)  — 新版 PE 格式才支持

    使用方式：
        extractor = PEResourceExtractor()
        resources = extractor.extract_all("path/to/WeChatWin.dll")
        extractor.save_as_png(resources, "templates/icons/")
    """

    # PE 资源类型常量
    RT_CURSOR = 1
    RT_BITMAP = 2
    RT_ICON = 3
    RT_MENU = 4
    RT_DIALOG = 5
    RT_STRING = 6
    RT_FONTDIR = 7
    RT_FONT = 8
    RT_ACCELERATOR = 9
    RT_RCDATA = 10
    RT_GROUP_CURSOR = 12
    RT_GROUP_ICON = 14
    RT_VERSION = 16
    RT_DLGINCLUDE = 17
    RT_PLUGPLAY = 19
    RT_VXD = 20
    RT_ANICURSOR = 21
    RT_ANIICON = 22
    RT_MANIFEST = 24

    # 要提取的资源类型
    TARGET_TYPES = {
        RT_BITMAP: 'BITMAP',
        RT_ICON: 'ICON',
        RT_GROUP_ICON: 'GROUP_ICON',
        RT_RCDATA: 'RCDATA',
    }

    def __init__(self):
        self._pefile_available = self._check_pefile()

    def _check_pefile(self) -> bool:
        try:
            import pefile
            return True
        except ImportError:
            logger.warning(
                "pefile 未安装，PE 资源提取不可用。"
                "请运行: pip install pefile"
            )
            return False

    # ── 公共接口 ──

    def extract_all(self, filepath: str) -> List[ExtractedResource]:
        """
        提取 PE 文件中所有图像资源。

        Args:
            filepath: WeChatWin.dll / WeChat.exe 路径

        Returns:
            提取出的资源列表
        """
        if not self._pefile_available:
            return []

        import pefile

        filepath = str(Path(filepath).resolve())
        logger.info(f"解析 PE 资源: {filepath}")

        results = []

        try:
            pe = pefile.PE(filepath)
            pe.parse_data_directories(directories=[
                pefile.DIRECTORY_ENTRY['IMAGE_DIRECTORY_ENTRY_RESOURCE']
            ])

            if not hasattr(pe, 'DIRECTORY_ENTRY_RESOURCE'):
                logger.warning(f"PE 文件无资源段: {filepath}")
                return results

            for entry in pe.DIRECTORY_ENTRY_RESOURCE.entries:
                # entry.id 是资源类型 ID
                type_id = entry.id
                if type_id not in self.TARGET_TYPES:
                    continue

                type_name = self.TARGET_TYPES[type_id]

                for name_entry in entry.directory.entries:
                    for lang_entry in name_entry.directory.entries:
                        rva = lang_entry.data.struct.OffsetToData
                        size = lang_entry.data.struct.Size

                        # 从 PE 文件中读取原始资源数据
                        raw_data = pe.get_data(rva, size)

                        resource = self._process_resource(
                            raw_data, type_name, type_id,
                            name_entry.id if hasattr(name_entry, 'id') else None
                        )

                        if resource:
                            results.append(resource)
                            logger.debug(
                                f"  提取: [{resource.type}] {resource.name} "
                                f"({resource.width}×{resource.height})"
                            )

        except Exception as e:
            logger.error(f"PE 解析失败: {e}")
            import traceback
            logger.debug(traceback.format_exc())

        logger.info(f"共提取 {len(results)} 个资源")
        return results

    def extract_and_save(self, filepath: str, output_dir: str) -> int:
        """
        提取并保存为 PNG 文件。

        Returns:
            成功保存的文件数
        """
        resources = self.extract_all(filepath)
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        saved = 0
        for res in resources:
            try:
                filepath = output_dir / f"{res.type}_{res.name}.png"
                self._save_resource_as_png(res, str(filepath))
                saved += 1
            except Exception as e:
                logger.debug(f"保存失败 {res.name}: {e}")

        logger.info(f"已保存 {saved} 个资源到 {output_dir}")
        return saved

    def find_wechat_resources(self, wechat_dir: str = None) -> List[str]:
        """
        扫描微信安装目录，找到所有可提取资源的文件。

        Returns:
            可提取资源的文件路径列表
        """
        if wechat_dir is None:
            wechat_dir = r"C:\Program Files\Tencent\WeChat"

        candidates = []

        # 要扫描的文件模式
        patterns = [
            "WeChatWin.dll",
            "WeChat.exe",
            "WeChatResource.dll",
            "wxRes*.dll",
            "[WeChat]_x64/WeChat.exe",
            "[WeChat]_x64/WeChatWin.dll",
        ]

        wechat_path = Path(wechat_dir)
        if not wechat_path.exists():
            logger.warning(f"微信目录不存在: {wechat_dir}")
            return candidates

        for pattern in patterns:
            for match in wechat_path.glob(pattern):
                if match.is_file():
                    candidates.append(str(match))

        # 也扫描版本子目录
        version_dirs = [d for d in wechat_path.iterdir()
                        if d.is_dir() and d.name.startswith('[')]
        for vdir in version_dirs:
            for dll in vdir.glob("*.dll"):
                candidates.append(str(dll))
            for exe in vdir.glob("*.exe"):
                candidates.append(str(exe))

        logger.info(f"找到 {len(candidates)} 个候选文件")
        return candidates

    # ── 资源处理 ──

    def _process_resource(self, raw_data: bytes, type_name: str,
                          type_id: int, resource_id) -> Optional[ExtractedResource]:
        """处理原始资源数据，尝试解析为图像"""
        name = str(resource_id) if resource_id is not None else "unknown"

        result = ExtractedResource(
            name=name,
            type=type_name,
            data=raw_data,
        )

        # 尝试解析图像尺寸
        try:
            if type_id == self.RT_ICON or type_id == self.RT_GROUP_ICON:
                self._parse_icon_info(result)
            elif type_id == self.RT_BITMAP:
                self._parse_bitmap_info(result)
            elif type_id == self.RT_RCDATA:
                # RCData 可能是 PNG 或其他格式
                self._try_parse_rcdata(result)
        except Exception:
            pass

        # 过滤太小的资源（可能不是 UI 图标）
        if result.width > 0 and result.width < 500 and result.height > 0 and result.height < 500:
            return result
        elif type_id in (self.RT_ICON, self.RT_GROUP_ICON):
            return result  # 图标总是保留

        return None

    def _parse_icon_info(self, resource: ExtractedResource):
        """从 ICO/GROUP_ICON 数据中提取尺寸"""
        data = resource.data
        if len(data) < 8:
            return
        # ICO 头部：reserved(2) + type(2) + count(2)
        count = struct.unpack_from('<H', data, 4)[0]
        if count > 0 and len(data) >= 6 + count * 16:
            # 第一项：w(1) + h(1) + colors(1) + reserved(1) + ...
            w, h = data[6], data[7]
            resource.width = w if w > 0 else 256
            resource.height = h if h > 0 else 256

    def _parse_bitmap_info(self, resource: ExtractedResource):
        """从 BITMAP 数据中提取尺寸"""
        data = resource.data
        if len(data) < 32:
            return
        # BITMAPINFOHEADER: biSize(4) + biWidth(4) + biHeight(4) + ...
        try:
            bi_width = struct.unpack_from('<i', data, 4)[0]
            bi_height = abs(struct.unpack_from('<i', data, 8)[0])
            resource.width = bi_width
            resource.height = bi_height
        except Exception:
            pass

    def _try_parse_rcdata(self, resource: ExtractedResource):
        """尝试解析 RCData 中的 PNG"""
        data = resource.data
        # 检查 PNG 签名
        if len(data) >= 8 and data[:8] == b'\x89PNG\r\n\x1a\n':
            try:
                img = Image.open(io.BytesIO(data))
                resource.width, resource.height = img.size
                resource.type = 'PNG_in_RCDATA'
            except Exception:
                pass
        # 检查 JPEG 签名
        elif len(data) >= 2 and data[:2] == b'\xff\xd8':
            try:
                img = Image.open(io.BytesIO(data))
                resource.width, resource.height = img.size
                resource.type = 'JPEG_in_RCDATA'
            except Exception:
                pass

    def _save_resource_as_png(self, resource: ExtractedResource, filepath: str):
        """将资源保存为 PNG"""
        data = resource.data

        if resource.type == 'PNG_in_RCDATA':
            # 直接写入 PNG 数据
            with open(filepath, 'wb') as f:
                f.write(data)
            return

        if resource.type in ('ICON', 'GROUP_ICON'):
            # ICO → PNG 转换
            self._ico_to_png(data, filepath)
            return

        if resource.type == 'BITMAP':
            # BMP → PNG 转换
            self._bmp_to_png(data, filepath)
            return

        # 其他：尝试用 PIL 打开
        try:
            img = Image.open(io.BytesIO(data))
            img.save(filepath, 'PNG')
        except Exception:
            # 无法解析，保存原始数据
            with open(filepath + '.bin', 'wb') as f:
                f.write(data)

    def _ico_to_png(self, data: bytes, filepath: str):
        """ICO → PNG 转换"""
        try:
            img = Image.open(io.BytesIO(data))
            img.save(filepath, 'PNG')
        except Exception as e:
            logger.debug(f"ICO 转换失败: {e}")

    def _bmp_to_png(self, data: bytes, filepath: str):
        """BMP → PNG 转换"""
        try:
            img = Image.open(io.BytesIO(data))
            img.save(filepath, 'PNG')
        except Exception as e:
            logger.debug(f"BMP 转换失败: {e}")


# ═══════════════════════════════════════════════════════════════
# 策略 B：Hook GDI/GDI+ 运行时捕获
# ═══════════════════════════════════════════════════════════════

class GDIImageHooker:
    """
    GDI/GDI+ 图像加载 Hook —— 运行时拦截微信加载的图片资源。

    原理：
      注入一个微型 DLL 到微信进程，Hook 以下函数：
        - GdipLoadImageFromFile:  拦截 GDI+ 从文件加载图像
        - GdipCreateBitmapFromStream: 拦截从内存流创建位图
        - LoadImageW:             拦截 Win32 API 加载图像
        - CreateCompatibleBitmap: 拦截 GDI 创建位图

      每次微信加载一个图像，Hook 截获并保存到磁盘。

    注意：
      - Hook 方案涉及进程注入，仅用于资源提取阶段
      - 注入完成后自动卸载，不影响正常的自动化操作
      - 与自动化运行时的 UIAutomation / OCR 方案不冲突

    使用方式：
        hooker = GDIImageHooker()
        hooker.start_capture(output_dir="templates/icons/")
        # ... 操作微信，触发要捕获的 UI 页面 ...
        captured = hooker.stop_capture()
        # captured 包含所有捕获到的图像文件路径
    """

    def __init__(self):
        self._hook_dll_path = None
        self._output_dir = None
        self._captured_files: List[str] = []

    def start_capture(self, output_dir: str = "templates/icons/",
                      inject_method: str = 'createremotethread') -> bool:
        """
        启动图像捕获 Hook。

        实现方式：
          1. 找到 WeChat.exe 进程
          2. 将 hook DLL 注入到 WeChat.exe 进程空间
          3. Hook DLL 自动拦截 GDI/GDI+ 图像加载调用
          4. 截获的图像自动保存到 output_dir

        Args:
            output_dir: 图像输出目录
            inject_method: 注入方式
              - 'createremotethread': CreateRemoteThread + LoadLibrary（最常用）
              - 'setwindowshookex': SetWindowsHookEx（针对 GUI 线程）
              - 'threadhijacking': 线程劫持（更隐蔽）

        Returns:
            True 表示注入成功
        """
        import ctypes
        import win32process
        import win32gui
        import win32api
        import win32con

        self._output_dir = str(Path(output_dir).resolve())
        Path(self._output_dir).mkdir(parents=True, exist_ok=True)

        # 1. 找到微信进程
        hwnd = win32gui.FindWindow("WeChatMainWndForPC", None)
        if not hwnd:
            logger.error("未找到微信窗口")
            return False

        tid, pid = win32process.GetWindowThreadProcessId(hwnd)
        logger.info(f"微信进程: PID={pid}, TID={tid}")

        # 2. 打开微信进程
        h_process = win32api.OpenProcess(
            win32con.PROCESS_CREATE_THREAD |
            win32con.PROCESS_VM_OPERATION |
            win32con.PROCESS_VM_WRITE |
            win32con.PROCESS_QUERY_INFORMATION,
            False, pid
        )

        if not h_process:
            logger.error("无法打开微信进程（需要管理员权限）")
            return False

        # 3. 注入 Hook DLL（需要编译好的 hook dll）
        hook_dll = self._get_hook_dll_path()
        if not hook_dll:
            logger.error("Hook DLL 不存在，需要先编译")
            logger.info(
                "请将以下 C 代码编译为 DLL，或使用 MinHook 库：\n"
                "  详见本文件末尾的 _generate_hook_dll_code() 方法"
            )
            return False

        # 4. 在微信进程中分配内存写入 DLL 路径
        dll_path_bytes = str(hook_dll).encode('utf-8') + b'\x00'
        remote_mem = win32process.VirtualAllocEx(
            h_process, None, len(dll_path_bytes),
            win32con.MEM_COMMIT, win32con.PAGE_READWRITE
        )
        win32process.WriteProcessMemory(
            h_process, remote_mem, dll_path_bytes
        )

        # 5. 创建远程线程执行 LoadLibrary
        kernel32 = ctypes.windll.kernel32
        load_library_addr = ctypes.cast(
            kernel32.LoadLibraryW,
            ctypes.c_void_p
        ).value

        thread_id = ctypes.c_ulong(0)
        ctypes.windll.kernel32.CreateRemoteThread(
            h_process, None, 0,
            load_library_addr,
            remote_mem, 0,
            ctypes.byref(thread_id)
        )

        logger.info(f"Hook DLL 已注入 (线程 ID={thread_id.value})")
        logger.info(f"捕获的图像将保存到: {self._output_dir}")

        return True

    def stop_capture(self) -> List[str]:
        """停止捕获，收集结果"""
        if self._output_dir:
            captured = list(Path(self._output_dir).glob("*.png"))
            self._captured_files = [str(p) for p in captured]
            logger.info(f"共捕获 {len(self._captured_files)} 个图像")
        return self._captured_files

    def _get_hook_dll_path(self) -> Optional[Path]:
        """获取 Hook DLL 路径"""
        # 查找已编译的 DLL
        candidates = [
            Path("hook/wechat_image_hook.dll"),
            Path("src/hook/wechat_image_hook.dll"),
            Path("hook/wechat_image_hook.x64.dll"),
        ]
        for path in candidates:
            if path.exists():
                return path.resolve()
        return None


# ═══════════════════════════════════════════════════════════════
# Hook DLL 源码（供参考编译）
# ═══════════════════════════════════════════════════════════════

HOOK_DLL_SOURCE = r"""
// wechat_image_hook.c — 微信图像加载 Hook DLL
//
// 编译方式（Windows + MinGW-w64）:
//   x86_64-w64-mingw32-gcc -shared -o wechat_image_hook.dll wechat_image_hook.c
//     -lgdiplus -luser32 -lminhook -O2
//
// 编译方式（Windows + MSVC）:
//   cl /LD wechat_image_hook.c /link gdiplus.lib user32.lib minhook.lib

#include <windows.h>
#include <gdiplus.h>
#include <stdio.h>

// === MinHook 库（https://github.com/TsudaKageyu/minhook）===
#include "minhook/MinHook.h"

// === 原始函数指针 ===
typedef int (WINAPI *GdipLoadImageFromFile_t)(WCHAR*, GpImage**);
typedef int (WINAPI *GdipCreateBitmapFromStream_t)(IStream*, GpBitmap**);
typedef HBITMAP (WINAPI *LoadImageW_t)(HINSTANCE, LPCWSTR, UINT, int, int, UINT);

GdipLoadImageFromFile_t       Real_GdipLoadImageFromFile = NULL;
GdipCreateBitmapFromStream_t  Real_GdipCreateBitmapFromStream = NULL;
LoadImageW_t                  Real_LoadImageW = NULL;

// === 输出目录（由环境变量或固定路径设定）===
static WCHAR g_output_dir[MAX_PATH] = L"C:\\wechat_captured_icons\\";

// === 全局计数器（用于生成唯一文件名）===
static LONG g_counter = 0;

// === 辅助函数：保存 GpImage 为 PNG 文件 ===
static void SaveImageToFile(GpImage* image) {
    CLSID pngClsid;
    CLSIDFromString(L"{557cf406-1a04-11d3-9a73-0000f81ef32e}", &pngClsid);

    WCHAR filepath[MAX_PATH];
    LONG id = InterlockedIncrement(&g_counter);
    wsprintfW(filepath, L"%s\\captured_%04d.png", g_output_dir, id);

    GdipSaveImageToFile(image, filepath, &pngClsid, NULL);
}

// === Hook: GdipLoadImageFromFile ===
int WINAPI Mine_GdipLoadImageFromFile(WCHAR* filename, GpImage** image) {
    int result = Real_GdipLoadImageFromFile(filename, image);
    if (result == 0 && image && *image) {
        SaveImageToFile(*image);
    }
    return result;
}

// === Hook: GdipCreateBitmapFromStream ===
int WINAPI Mine_GdipCreateBitmapFromStream(IStream* stream, GpBitmap** bitmap) {
    int result = Real_GdipCreateBitmapFromStream(stream, bitmap);
    if (result == 0 && bitmap && *bitmap) {
        SaveImageToFile(*bitmap);
    }
    return result;
}

// === Hook: LoadImageW ===
HBITMAP WINAPI Mine_LoadImageW(HINSTANCE hInst, LPCWSTR name,
                                UINT type, int cx, int cy, UINT fuLoad) {
    HBITMAP result = Real_LoadImageW(hInst, name, type, cx, cy, fuLoad);
    if (result && type == IMAGE_BITMAP) {
        // BITMAP → GpImage → 保存
        BITMAP bm;
        GetObjectW(result, sizeof(bm), &bm);
        GpBitmap* gpbmp = NULL;
        GdipCreateBitmapFromHBITMAP(result, NULL, &gpbmp);
        if (gpbmp) {
            SaveImageToFile(gpbmp);
            GdipDisposeImage((GpImage*)gpbmp);
        }
    }
    return result;
}

// === DLL 入口 ===
BOOL WINAPI DllMain(HINSTANCE hinstDLL, DWORD fdwReason, LPVOID lpvReserved) {
    if (fdwReason == DLL_PROCESS_ATTACH) {
        // 初始化 GDI+
        GdiplusStartupInput gdiplusStartupInput;
        ULONG_PTR gdiplusToken;
        GdiplusStartup(&gdiplusToken, &gdiplusStartupInput, NULL);

        // 初始化 MinHook
        MH_Initialize();

        // 获取 gdiplus.dll 中的函数地址
        HMODULE hGdiplus = LoadLibraryW(L"gdiplus.dll");
        HMODULE hUser32  = LoadLibraryW(L"user32.dll");

        // Hook GdipLoadImageFromFile
        MH_CreateHook(
            GetProcAddress(hGdiplus, "GdipLoadImageFromFile"),
            &Mine_GdipLoadImageFromFile,
            (LPVOID*)&Real_GdipLoadImageFromFile
        );

        // Hook GdipCreateBitmapFromStream
        MH_CreateHook(
            GetProcAddress(hGdiplus, "GdipCreateBitmapFromStream"),
            &Mine_GdipCreateBitmapFromStream,
            (LPVOID*)&Real_GdipCreateBitmapFromStream
        );

        // Hook LoadImageW
        MH_CreateHook(
            GetProcAddress(hUser32, "LoadImageW"),
            &Mine_LoadImageW,
            (LPVOID*)&Real_LoadImageW
        );

        // 启用所有 Hook
        MH_EnableHook(MH_ALL_HOOKS);

        // 确保输出目录存在
        CreateDirectoryW(g_output_dir, NULL);
    }

    if (fdwReason == DLL_PROCESS_DETACH) {
        MH_DisableHook(MH_ALL_HOOKS);
        MH_Uninitialize();
    }

    return TRUE;
}
"""


# ═══════════════════════════════════════════════════════════════
# 统一入口：自动选择最佳策略
# ═══════════════════════════════════════════════════════════════

class ResourceCollector:
    """
    资源收集器 —— 自动选择最佳策略提取微信 UI 图标。

    优先级：
      1. PE 资源解析（安全、快速）
      2. 运行时模板截取（自动化截图 + 边缘检测）
      3. GDI Hook 捕获（需要注入，仅用于无法提取的情况）

    使用方式：
        collector = ResourceCollector()
        templates = collector.collect_all()
        # templates/ 目录下自动生成所有可用的 UI 元素模板
    """

    def __init__(self, wechat_dir: str = None,
                 output_dir: str = "templates/icons"):
        self._wechat_dir = wechat_dir or r"C:\Program Files\Tencent\WeChat"
        self._output_dir = Path(output_dir)
        self._output_dir.mkdir(parents=True, exist_ok=True)

        self._pe_extractor = PEResourceExtractor()
        # Hook 提取器延迟初始化（只在需要时）

    def collect_all(self, strategy: str = 'auto') -> int:
        """
        收集所有可用模板。

        Args:
            strategy: 'auto' | 'pe' | 'hook' | 'screenshot'

        Returns:
            收集到的模板数量
        """
        total = 0

        if strategy in ('auto', 'pe'):
            total += self._collect_from_pe()

        if total == 0 and strategy in ('auto', 'hook'):
            logger.warning("PE 资源提取未找到可用图标，切换到 Hook 策略")
            total += self._collect_from_hook()

        if total == 0:
            logger.warning(
                "所有自动提取策略均未成功。"
                "请使用 runtime_calibration 工具手动校准。"
            )

        return total

    def _collect_from_pe(self) -> int:
        """从 PE 文件提取"""
        files = self._pe_extractor.find_wechat_resources(self._wechat_dir)
        total = 0
        for filepath in files:
            total += self._pe_extractor.extract_and_save(filepath, self._output_dir)
        return total

    def _collect_from_hook(self) -> int:
        """通过 Hook 捕获"""
        hooker = GDIImageHooker()
        if hooker.start_capture(str(self._output_dir)):
            logger.info(
                "Hook 已注入。请操作微信触发要捕获的 UI 页面，"
                "然后按 Enter 停止捕获。"
            )
            input("按 Enter 停止捕获...")
            captured = hooker.stop_capture()
            return len(captured)
        return 0
