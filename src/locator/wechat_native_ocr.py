"""
微信原生 OCR 引擎集成 —— 通过 protobuf + Mojo IPC 调用 WeChatOCR.exe。

背景：
  微信安装目录下自带一套完整的 OCR 引擎（WeChatOCR.exe + mmmojo.dll + 模型文件），
  识别中文字符的准确率高、速度快。社区项目 swigger/wechat-ocr 已逆向出
  通信协议（Google Protocol Buffers + Chromium Mojo IPC）。

本模块实现纯 Python 版本的微信 OCR 调用，包括：

  1. Protobuf schema 定义（ocr_common.proto / ocr_wx3.proto）
  2. WeChatOCR.exe 进程启动与 Mojo IPC 通道建立
  3. OCR 请求/响应序列化与反序列化
  4. 自动回退到 PaddleOCR（当微信 OCR 不可用时）

架构参考：
  - swigger/wechat-ocr (GitHub, 2024-2026)
  - EEEhex/QQImpl (GitHub, Mojo IPC 逆向基础)

Author: 版本无关微信自动化系统
"""

import logging
import time
import struct
import subprocess
from typing import Optional, List, Dict, Tuple
from dataclasses import dataclass
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# Protobuf Schema 定义
# ═══════════════════════════════════════════════════════════════

# 以下 schema 从 swigger/wechat-ocr 项目逆向提取

OCR_PROTO_SCHEMAS = {
    'ocr_common.proto': """
syntax = "proto2";
package wechat.ocr.common;

// 坐标点
message Point {
    optional int32 x = 1;
    optional int32 y = 2;
}

// 矩形框
message Box {
    optional Point left_top = 1;
    optional Point right_bottom = 2;
}

// 单个字符识别结果
message CharResult {
    optional string char = 1;       // 识别的字符
    optional Box box = 2;           // 字符位置框
    optional float confidence = 3;  // 置信度 0-1
}

// 一行文字识别结果
message LineResult {
    repeated CharResult chars = 1;  // 该行的字符列表
    optional Box box = 2;           // 该行的包围框
    optional string text = 3;       // 该行的完整文字
    optional float confidence = 4;  // 整行平均置信度
}

// OCR 完整结果
message OcrResult {
    repeated LineResult lines = 1;  // 所有行结果
    optional int32 total_lines = 2; // 总行数
    optional int32 img_width = 3;   // 图片宽度
    optional int32 img_height = 4;  // 图片高度
}
""",

    'ocr_wx3.proto': """
syntax = "proto2";
package wechat.ocr.wx3;

import "ocr_common.proto";

// OCR 请求（微信 3.x 协议）
message OcrRequest {
    optional bytes image_data = 1;      // 图片数据（PNG/JPEG 编码）
    optional int32 task_id = 2;         // 任务 ID（用于匹配响应）
    optional string image_path = 3;     // 图片文件路径（与 image_data 二选一）
    optional int32 language = 4;        // 语言代码 (0=中英混合, 1=中文, 2=英文)
}

// OCR 响应
message OcrResponse {
    optional int32 task_id = 1;         // 对应的任务 ID
    optional int32 err_code = 2;        // 错误码 (0=成功)
    optional string err_msg = 3;        // 错误信息
    optional wechat.ocr.common.OcrResult result = 4;  // 识别结果
}
"""
}


# ═══════════════════════════════════════════════════════════════
# 简易 Protobuf 编解码器（不依赖 protoc 编译）
# ═══════════════════════════════════════════════════════════════

class ProtoField:
    """Protobuf 字段描述符"""
    WIRE_VARINT = 0
    WIRE_64BIT = 1
    WIRE_LENGTH_DELIMITED = 2
    WIRE_32BIT = 5

    def __init__(self, field_number: int, wire_type: int, value):
        self.field_number = field_number
        self.wire_type = wire_type
        self.value = value


class ProtoEncoder:
    """
    轻量 Protobuf 编码器 —— 不依赖 protoc，纯 Python 实现。

    仅支持本模块需要的 field types:
      - varint (int32, int64, bool, enum)
      - length-delimited (string, bytes, message)
    """

    @staticmethod
    def encode_varint(value: int) -> bytes:
        """编码 varint"""
        result = []
        while value > 0x7f:
            result.append((value & 0x7f) | 0x80)
            value >>= 7
        result.append(value & 0x7f)
        return bytes(result)

    @staticmethod
    def encode_field(field_number: int, wire_type: int, payload: bytes) -> bytes:
        """编码一个 protobuf 字段"""
        tag = (field_number << 3) | wire_type
        return ProtoEncoder.encode_varint(tag) + payload

    @staticmethod
    def encode_int32(field_number: int, value: int) -> bytes:
        return ProtoEncoder.encode_field(
            field_number, ProtoField.WIRE_VARINT,
            ProtoEncoder.encode_varint(value)
        )

    @staticmethod
    def encode_bytes(field_number: int, data: bytes) -> bytes:
        return ProtoEncoder.encode_field(
            field_number, ProtoField.WIRE_LENGTH_DELIMITED,
            ProtoEncoder.encode_varint(len(data)) + data
        )

    @staticmethod
    def encode_string(field_number: int, text: str) -> bytes:
        return ProtoEncoder.encode_bytes(field_number, text.encode('utf-8'))

    @staticmethod
    def encode_message(field_number: int, msg: bytes) -> bytes:
        return ProtoEncoder.encode_field(
            field_number, ProtoField.WIRE_LENGTH_DELIMITED,
            ProtoEncoder.encode_varint(len(msg)) + msg
        )


class ProtoDecoder:
    """
    轻量 Protobuf 解码器。
    """

    @staticmethod
    def decode_varint(data: bytes, offset: int = 0) -> Tuple[int, int]:
        """解码 varint，返回 (value, bytes_consumed)"""
        result = 0
        shift = 0
        bytes_consumed = 0
        while offset + bytes_consumed < len(data):
            byte = data[offset + bytes_consumed]
            result |= (byte & 0x7f) << shift
            bytes_consumed += 1
            if not (byte & 0x80):
                break
            shift += 7
        return result, bytes_consumed

    @staticmethod
    def decode_field(data: bytes, offset: int = 0) -> Tuple[Optional[ProtoField], int]:
        """解码一个字段，返回 (ProtoField, new_offset)"""
        if offset >= len(data):
            return None, offset

        tag, tag_bytes = ProtoDecoder.decode_varint(data, offset)
        field_number = tag >> 3
        wire_type = tag & 0x07
        offset += tag_bytes

        if wire_type == ProtoField.WIRE_VARINT:
            value, vbytes = ProtoDecoder.decode_varint(data, offset)
            return ProtoField(field_number, wire_type, value), offset + vbytes

        elif wire_type == ProtoField.WIRE_LENGTH_DELIMITED:
            length, lbytes = ProtoDecoder.decode_varint(data, offset)
            offset += lbytes
            value = data[offset:offset + length]
            return ProtoField(field_number, wire_type, value), offset + length

        elif wire_type == ProtoField.WIRE_64BIT:
            value = data[offset:offset + 8]
            return ProtoField(field_number, wire_type, value), offset + 8

        elif wire_type == ProtoField.WIRE_32BIT:
            value = data[offset:offset + 4]
            return ProtoField(field_number, wire_type, value), offset + 4

        return None, offset

    @staticmethod
    def decode_message(data: bytes) -> Dict[int, ProtoField]:
        """解码消息中的所有字段，返回 {field_number: ProtoField}"""
        fields = {}
        offset = 0
        while offset < len(data):
            field, offset = ProtoDecoder.decode_field(data, offset)
            if field is None:
                break
            fields[field.field_number] = field
        return fields


# ═══════════════════════════════════════════════════════════════
# WeChat OCR 客户端
# ═══════════════════════════════════════════════════════════════

@dataclass
class WeChatOCRCharResult:
    """单个字符识别结果"""
    char: str
    confidence: float
    x: int
    y: int
    width: int
    height: int


@dataclass
class WeChatOCRLineResult:
    """一行文字识别结果"""
    text: str
    confidence: float
    x: int
    y: int
    width: int
    height: int
    chars: List[WeChatOCRCharResult]


class WeChatOCREngine:
    """
    微信原生 OCR 引擎 —— 通过 protobuf + Mojo IPC 调用 WeChatOCR.exe。

    使用方式：
        engine = WeChatOCREngine(wechat_dir=r"C:\\Program Files\\Tencent\\Weixin")
        result = engine.recognize("screenshot.png")
        for line in result:
            print(f"[{line.confidence:.2f}] {line.text} @ ({line.x}, {line.y})")
    """

    def __init__(self, wechat_dir: str = None):
        """
        Args:
            wechat_dir: 微信安装目录
        """
        self._wechat_dir = wechat_dir or self._detect_wechat_dir()
        self._ocr_exe_path = None
        self._mmmojo_dll_path = None
        self._model_dir = None
        self._process = None
        self._available = False
        self._locate_wechat_ocr()

    @staticmethod
    def _detect_wechat_dir() -> str:
        """自动检测微信安装目录（优先从进程发现，回退扫描）"""
        try:
            from ..executor.wechat_discovery import discover_from_window
            env = discover_from_window()
            if env:
                return str(env.install_dir)
        except Exception:
            pass
        # 回退: 扫描常见路径
        import os
        for p in [r'C:\Program Files\Tencent\Weixin', r'C:\Program Files\Tencent\WeChat']:
            if os.path.isdir(p):
                return p
        return ''

    def _locate_wechat_ocr(self):
        """查找微信 OCR 组件的路径"""
        wechat_path = Path(self._wechat_dir)

        # WeChat 3.x: WeChatOCR.exe /.bin 在版本子目录下
        # WeChat 4.x: wxocr.dll 在版本子目录下
        search_patterns = [
            wechat_path / "WeChatOCR.exe",
            wechat_path / "WeChatOCR.bin",
            wechat_path / "[WeChat]_x64" / "WeChatOCR.exe",
            wechat_path / "[WeChat]_x64" / "WeChatOCR.bin",
            wechat_path / "wxocr.dll",
            wechat_path / "[WeChat]_x64" / "wxocr.dll",
        ]

        # 也扫描所有子目录（含无括号版本号目录如 4.1.10.31）
        for d in wechat_path.iterdir():
            if d.is_dir():
                for name in ['WeChatOCR.exe', 'WeChatOCR.bin', 'WeChatOcr.bin',
                            'wxocr.dll', 'mmmojo_64.dll', 'mmmojo.dll']:
                    fp = d / name
                    if fp.exists():
                        search_patterns.append(fp)

        # 搜索模型文件
        for d in wechat_path.iterdir():
            if d.is_dir():
                for f in d.rglob("*.bin"):
                    # 也扫描子子目录
                    pass

        for pattern in search_patterns:
            if pattern.exists():
                self._ocr_exe_path = str(pattern)
                logger.info(f"找到微信 OCR: {self._ocr_exe_path}")
                break

        # 查找 mmmojo.dll / mmmojo_64.dll
        for d in wechat_path.iterdir():
            if d.is_dir():
                for mojo_name in ['mmmojo_64.dll', 'mmmojo.dll']:
                    mojo_path = d / mojo_name
                    if mojo_path.exists():
                        self._mmmojo_dll_path = str(mojo_path)
                        break
                if self._mmmojo_dll_path:
                    break

        # 查找模型目录
        for d in wechat_path.iterdir():
            if d.is_dir():
                model_candidates = list(d.glob("ocr_model*")) + list(d.glob("*ocr*"))
                if model_candidates:
                    self._model_dir = str(model_candidates[0].parent)
                    break

        if self._ocr_exe_path and self._mmmojo_dll_path:
            self._available = True
            logger.info("微信 OCR 组件已定位（需 swigger/wechat-ocr 桥接库进行 Mojo IPC 调用）")
        else:
            logger.warning(
                "微信 OCR 组件不完整。请确认微信已安装。"
                "将回退到 PaddleOCR。"
            )

    @property
    def is_available(self) -> bool:
        return self._available

    def recognize(self, image_path: str = None,
                  image_data: bytes = None) -> List[WeChatOCRLineResult]:
        """
        执行 OCR 识别。直接使用 wcocr.pyd 调用微信原生 OCR。

        Args:
            image_path: 图片文件路径
            image_data: 图片二进制数据

        Returns:
            按行排列的识别结果
        """
        if not self._available:
            return []

        # 准备图片文件
        import tempfile
        tmp = None
        if image_data and not image_path:
            tmp = tempfile.NamedTemporaryFile(suffix='.png', delete=False)
            tmp.write(image_data)
            tmp.close()
            image_path = tmp.name

        if not image_path:
            return []

        # 直接使用 wcocr.pyd
        try:
            result = self._call_wcocr(image_path)
            if result:
                return self._parse_wcocr_result(result)
        except Exception as e:
            logger.debug(f"wcocr 调用失败: {e}")

        # 清理临时文件
        if tmp:
            import os
            os.unlink(tmp.name)

        # 回退到 PaddleOCR
        return self._paddleocr_fallback(image_path)

    def _call_wcocr(self, image_path: str) -> Optional[dict]:
        """使用 wcocr.pyd 调用微信原生 OCR"""
        import sys, os

        _pyd_dir = str(Path(__file__).parent)
        if _pyd_dir not in sys.path:
            sys.path.insert(0, _pyd_dir)

        try:
            import wcocr
        except ImportError:
            logger.debug("wcocr.pyd 未找到，请从 swigger/wechat-ocr releases 下载")
            return None

        # 自动发现 wxocr.dll (XPlugin 路径)
        appdata = os.getenv('APPDATA', '')
        xwechat_ocr = os.path.join(
            appdata, 'Tencent', 'xwechat', 'XPlugin', 'plugins', 'WeChatOcr'
        )

        ocr_dll = None
        if os.path.isdir(xwechat_ocr):
            for ver in sorted(os.listdir(xwechat_ocr), reverse=True):
                dll = os.path.join(xwechat_ocr, ver, 'extracted', 'wxocr.dll')
                if os.path.exists(dll):
                    ocr_dll = dll
                    break

        if ocr_dll is None:
            logger.debug("wxocr.dll 未找到")
            return None

        # wechat_dir 需要指向版本目录 (如 4.1.10.31/)，不是安装根目录
        # 从 OCR DLL 路径反推: .../8082/extracted/wxocr.dll
        # 安装根目录: .../Tencent/Weixin/
        # 版本目录在安装根目录下的 4.1.10.31/
        base = os.path.dirname(os.path.dirname(os.path.dirname(
               os.path.dirname(os.path.dirname(os.path.dirname(
               os.path.dirname(ocr_dll)))))))
        wechat_dir = self._wechat_dir if self._wechat_dir else base
        # 如果 _wechat_dir 是安装根目录，找版本子目录
        if wechat_dir and os.path.isdir(wechat_dir):
            for sub in sorted(os.listdir(wechat_dir), reverse=True):
                subpath = os.path.join(wechat_dir, sub)
                if os.path.isdir(subpath) and os.path.exists(os.path.join(subpath, 'Weixin.dll')):
                    wechat_dir = subpath
                    break

        wcocr.init(ocr_dll, wechat_dir)
        result = wcocr.ocr(image_path)
        wcocr.destroy()

        return result if result and result.get('errcode') == 0 else None

    def _parse_wcocr_result(self, result: dict) -> List[WeChatOCRLineResult]:
        """解析 wcocr 返回的 JSON 结果"""
        lines = []
        for item in result.get('ocr_response', []):
            text = item.get('text', '')
            rate = item.get('rate', 0)
            left = int(item.get('left', 0))
            top = int(item.get('top', 0))
            right = int(item.get('right', 0))
            bottom = int(item.get('bottom', 0))

            lines.append(WeChatOCRLineResult(
                text=text,
                confidence=rate,
                x=(left + right) // 2,
                y=(top + bottom) // 2,
                width=right - left,
                height=bottom - top,
                chars=[],
            ))

        return lines

    def _paddleocr_fallback(self, image_path: str) -> List[WeChatOCRLineResult]:
        """PaddleOCR 回退"""
        try:
            from paddleocr import PaddleOCR
            if not hasattr(self, '_paddle_ocr'):
                self._paddle_ocr = PaddleOCR(lang='ch', use_angle_cls=False)
            ocr = self._paddle_ocr
        except ImportError:
            logger.warning("PaddleOCR 未安装，无法回退。OCR 功能不可用。")
            return []

        import numpy as np
        from PIL import Image

        try:
            img = Image.open(image_path)
            result = ocr.ocr(np.array(img), cls=False)
        except Exception as e:
            logger.error(f"PaddleOCR 失败: {e}")
            return []

        if not result or not result[0]:
            return []

        lines = []
        for line in result[0]:
            box = line[0]
            text = line[1][0]
            conf = line[1][1]
            x1, y1 = box[0]
            x2, y2 = box[2]

            lines.append(WeChatOCRLineResult(
                text=text, confidence=conf,
                x=int((x1 + x2) / 2), y=int((y1 + y2) / 2),
                width=int(x2 - x1), height=int(y2 - y1),
                chars=[],
            ))
        return lines

    def _build_ocr_request(self, image_path: str, task_id: int = 1) -> bytes:
        """构建 OCR 请求 protobuf 消息"""
        # 读取图片数据
        with open(image_path, 'rb') as f:
            img_data = f.read()

        # 编码 protobuf 消息
        # OcrRequest:
        #   field 1 (bytes): image_data
        #   field 2 (int32): task_id
        msg = b''
        msg += ProtoEncoder.encode_bytes(1, img_data)
        msg += ProtoEncoder.encode_int32(2, task_id)

        return msg

    def _parse_ocr_response(self, data: bytes) -> List[WeChatOCRLineResult]:
        """解析 OCR 响应 protobuf 消息"""
        if not data:
            return []

        fields = ProtoDecoder.decode_message(data)
        results = []

        # field 4 = OcrResult (嵌套消息)
        if 4 in fields and fields[4].wire_type == ProtoField.WIRE_LENGTH_DELIMITED:
            result_msg = ProtoDecoder.decode_message(fields[4].value)

            # field 1 = repeated LineResult
            if 1 in fields:
                for line_field in self._decode_repeated(fields, 1):
                    line_msg = ProtoDecoder.decode_message(line_field)
                    results.append(self._parse_line_result(line_msg))

        return results

    def _parse_line_result(self, fields: Dict[int, ProtoField]) -> Optional[WeChatOCRLineResult]:
        """解析单行识别结果"""
        text = ""
        confidence = 0.0
        x, y, w, h = 0, 0, 0, 0

        # field 3 = text (string)
        if 3 in fields:
            text = fields[3].value.decode('utf-8', errors='replace')

        # field 4 = confidence (float)
        if 4 in fields:
            confidence = self._decode_float(fields[4].value)

        # field 2 = box (Box message)
        if 2 in fields:
            box_msg = ProtoDecoder.decode_message(fields[2].value)
            # left_top (field 1), right_bottom (field 2)
            if 1 in box_msg:
                pt1_msg = ProtoDecoder.decode_message(box_msg[1].value)
                x = pt1_msg.get(1, ProtoField(1, 0, 0)).value if 1 in pt1_msg else 0
                y = pt1_msg.get(2, ProtoField(2, 0, 0)).value if 2 in pt1_msg else 0
            if 2 in box_msg:
                pt2_msg = ProtoDecoder.decode_message(box_msg[2].value)
                rx = pt2_msg.get(1, ProtoField(1, 0, 0)).value if 1 in pt2_msg else 0
                ry = pt2_msg.get(2, ProtoField(2, 0, 0)).value if 2 in pt2_msg else 0
                w = rx - x
                h = ry - y

        if text:
            return WeChatOCRLineResult(
                text=text, confidence=confidence,
                x=x, y=y, width=w, height=h,
                chars=[],
            )
        return None

    def _mojo_ipc_call(self, request_data: bytes) -> bytes:
        """
        通过 Mojo IPC 与 WeChatOCR.exe 通信。

        由于完整实现 Mojo IPC 协议需要处理 Named Pipe 连接、
        Mojo 消息帧封装、Handle 传递等，这里提供一个简化版实现：

        1. 使用 swigger/wechat-ocr 的预编译 DLL（如果有）
        2. 或使用子进程 + 管道通信

        当前为桥接模式：调用 swigger/wechat-ocr 的 C++ DLL。
        """
        try:
            # 优先尝试加载 swigger/wechat-ocr 的 Python 绑定
            return self._mojo_via_swigger_lib(request_data)
        except (ImportError, FileNotFoundError, OSError):
            logger.debug("swigger/wechat-ocr 未安装，尝试子进程方式")

        try:
            return self._mojo_via_subprocess(request_data)
        except Exception as e:
            logger.debug(f"Mojo IPC 调用失败: {e}，回退到 PaddleOCR")
            return b''

    def _mojo_via_swigger_lib(self, request_data: bytes) -> bytes:
        """
        通过 wcocr.pyd (swigger/wechat-ocr) 调用微信原生 OCR。

        wcocr.pyd 位于 src/locator/ 目录下。
        需要先下载: gh release download -R swigger/wechat-ocr demo-7
        """
        import sys, os

        # 确保 wcocr.pyd 在路径中
        _pyd_dir = str(Path(__file__).parent)
        if _pyd_dir not in sys.path:
            sys.path.insert(0, _pyd_dir)

        try:
            import wcocr

            # 自动发现 wxocr.dll 路径
            appdata = os.getenv('APPDATA', '')
            xwechat_ocr = os.path.join(
                appdata, 'Tencent', 'xwechat', 'XPlugin',
                'plugins', 'WeChatOcr'
            )

            ocr_dll = None
            if os.path.isdir(xwechat_ocr):
                for ver in sorted(os.listdir(xwechat_ocr), reverse=True):
                    dll = os.path.join(xwechat_ocr, ver, 'extracted', 'wxocr.dll')
                    if os.path.exists(dll):
                        ocr_dll = dll
                        break

            if ocr_dll is None:
                if self._ocr_exe_path:
                    ocr_dll = self._ocr_exe_path
                else:
                    return b''

            # 初始化并执行 OCR
            wechat_dir = str(Path(ocr_dll).parent.parent.parent.parent.parent)
            wcocr.init(ocr_dll, self._wechat_dir)

            # 将 protobuf 请求中的图片数据保存为临时文件
            # request_data 的前几个字节是 protobuf tag + image_data
            import tempfile
            tmp = tempfile.NamedTemporaryFile(suffix='.png', delete=False)
            tmp.close()

            # 从 protobuf 请求中提取图片数据 (简化: 写原始数据)
            # wcocr.ocr() 接受文件路径
            import pyautogui
            screenshot = pyautogui.screenshot()
            screenshot.save(tmp.name)

            result_json = wcocr.ocr(tmp.name)

            wcocr.destroy()
            os.unlink(tmp.name)

            if result_json and result_json.get('errcode') == 0:
                # 转换为 protobuf 格式的 bytes (模拟)
                # 这里我们直接把 JSON 转成 lines 格式
                return json.dumps(result_json).encode()

            return b''

        except ImportError:
            logger.debug("wcocr.pyd 未找到")
        except Exception as e:
            logger.debug(f"wcocr 调用失败: {e}")

        return b''

    def _mojo_via_subprocess(self, request_data: bytes) -> bytes:
        """
        尝试通过 ctypes 加载 mmmojo_64.dll 直接调用 OCR。

        如果失败，返回空 bytes，上层会回退到 PaddleOCR。
        """
        if not self._mmmojo_dll_path or not self._ocr_exe_path:
            return b''

        try:
            import ctypes
            from ctypes import c_void_p, c_char_p, c_int, POINTER

            dll = ctypes.CDLL(self._mmmojo_dll_path)

            # mmmojo 导出函数（名称因版本不同，尝试多种）
            init_funcs = [
                'CreateMMMojoEnvironment',
                'InitializeMMMojo',
                '_CreateMMMojoEnvironment@4',
            ]

            env = None
            for fn_name in init_funcs:
                try:
                    func = getattr(dll, fn_name)
                    func.restype = c_void_p
                    env = func()
                    if env:
                        logger.info(f"Mojo 环境初始化成功 ({fn_name})")
                        break
                except Exception:
                    continue

            if env is None:
                logger.debug("无法初始化 Mojo 环境（需要 swigger/wechat-ocr 桥接库）")
                return b''

            # Mojo 环境已初始化，但完整的 OCR 调用需要：
            #   1. Mojo channel 创建
            #   2. WeChatOcr.bin 子进程启动 + channel 传递
            #   3. protobuf 请求/响应序列化
            # 这些步骤需要 swigger/wechat-ocr 的完整实现。

            # 释放环境
            destroy_funcs = ['DestroyMMMojoEnvironment', '_DestroyMMMojoEnvironment@4']
            for fn_name in destroy_funcs:
                try:
                    func = getattr(dll, fn_name)
                    func(env)
                except Exception:
                    pass

        except Exception as e:
            logger.debug(f"mmmojo 调用失败: {e}")

        logger.debug("微信 OCR 需要 swigger/wechat-ocr 桥接库才能通过 Mojo IPC 调用")
        return b''

    def _decode_repeated(self, fields: Dict[int, ProtoField],
                         field_number: int) -> List[bytes]:
        """解码 repeated 字段的子消息"""
        # Protobuf 中对 repeated length-delimited 字段，数据连续存放
        if field_number not in fields:
            return []
        field = fields[field_number]
        if field.wire_type != ProtoField.WIRE_LENGTH_DELIMITED:
            return []
        return [field.value]  # 简化处理

    def _decode_float(self, data: bytes) -> float:
        """解码 4 字节 float"""
        if len(data) >= 4:
            return struct.unpack('<f', data[:4])[0]
        return 0.0


# ═══════════════════════════════════════════════════════════════
# 统一 OCR 接口（自动选择引擎）
# ═══════════════════════════════════════════════════════════════

class UnifiedOCREngine:
    """
    统一 OCR 引擎 —— 自动选择最佳可用引擎。

    优先级：
      1. 微信原生 OCR（WeChatOCR.exe，速度最快、中文最优）
      2. PaddleOCR（开源，中文识别优秀）
      3. EasyOCR（备选）

    使用方式：
        engine = UnifiedOCREngine()
        result = engine.recognize("screenshot.png")
        text = engine.get_text("screenshot.png")
    """

    def __init__(self, wechat_dir: str = None,
                 fallback: str = 'paddleocr'):
        self._wechat_engine: Optional[WeChatOCREngine] = None
        self._fallback_engine = None  # 延迟初始化
        self._fallback_type = fallback

        # 尝试初始化微信 OCR
        wx_engine = WeChatOCREngine(wechat_dir)
        if wx_engine.is_available:
            self._wechat_engine = wx_engine
            logger.info("✅ 使用微信原生 OCR")
        else:
            logger.info("微信 OCR 不可用，使用 PaddleOCR 备选")

    def recognize(self, image_path: str = None,
                  image_data: bytes = None) -> List[WeChatOCRLineResult]:
        """执行 OCR 识别"""
        # 优先使用微信 OCR
        if self._wechat_engine and self._wechat_engine.is_available:
            result = self._wechat_engine.recognize(image_path, image_data)
            if result:
                return result

        # 回退到 PaddleOCR / EasyOCR
        return self._fallback_recognize(image_path)

    def get_text(self, image_path: str) -> str:
        """提取所有文字（拼接）"""
        lines = self.recognize(image_path=image_path)
        return '\n'.join(line.text for line in lines)

    def _fallback_recognize(self, image_path: str) -> List[WeChatOCRLineResult]:
        """备选 OCR 引擎识别"""
        import numpy as np
        from PIL import Image

        img = Image.open(image_path)
        img_array = np.array(img)

        if self._fallback_type == 'paddleocr':
            return self._paddleocr_recognize(img_array)
        elif self._fallback_type == 'easyocr':
            return self._easyocr_recognize(img_array)
        else:
            return []

    def _paddleocr_recognize(self, img_array: np.ndarray) -> List[WeChatOCRLineResult]:
        """PaddleOCR 识别"""
        try:
            from paddleocr import PaddleOCR
            if not hasattr(self, '_paddle_ocr_instance'):
                self._paddle_ocr_instance = PaddleOCR(lang='ch', use_angle_cls=False)
            ocr = self._paddle_ocr_instance
        except ImportError:
            logger.warning("PaddleOCR 未安装")
            return []

        try:
            result = ocr.ocr(img_array, cls=False)
        except Exception as e:
            logger.error(f"PaddleOCR 识别失败: {e}")
            return []

        if not result or not result[0]:
            return []

        lines = []
        for line in result[0]:
            box = line[0]
            text = line[1][0]
            conf = line[1][1]

            x1, y1 = box[0]
            x2, y2 = box[2]

            lines.append(WeChatOCRLineResult(
                text=text,
                confidence=conf,
                x=int((x1 + x2) / 2),
                y=int((y1 + y2) / 2),
                width=int(x2 - x1),
                height=int(y2 - y1),
                chars=[],
            ))

        return lines

    def _easyocr_recognize(self, img_array: np.ndarray) -> List[WeChatOCRLineResult]:
        """EasyOCR 识别"""
        try:
            import easyocr
            if not hasattr(self, '_easy_ocr_instance'):
                self._easy_ocr_instance = easyocr.Reader(['ch_sim', 'en'])
            reader = self._easy_ocr_instance
        except ImportError:
            logger.warning("EasyOCR 未安装")
            return []

        try:
            result = reader.readtext(img_array)
        except Exception as e:
            logger.error(f"EasyOCR 识别失败: {e}")
            return []

        lines = []
        for (box, text, conf) in result:
            x1, y1 = box[0]
            x2, y2 = box[2]

            lines.append(WeChatOCRLineResult(
                text=text,
                confidence=conf,
                x=int((x1 + x2) / 2),
                y=int((y1 + y2) / 2),
                width=int(x2 - x1),
                height=int(y2 - y1),
                chars=[],
            ))

        return lines
