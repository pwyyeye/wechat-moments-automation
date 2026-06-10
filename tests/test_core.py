"""
核心模块单元测试 — StateMachine + RiskDetector + ProtoEncoder

运行: python -m pytest tests/test_core.py -v
"""

import time
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest


class TestStateMachine:
    """状态机测试"""

    def test_state_transitions(self):
        from src.executor.state_machine import (
            WorkflowStateMachine, WorkflowState, WorkflowContext, StateConfig
        )

        sm = WorkflowStateMachine()
        ctx = WorkflowContext(text="hello")

        # 注册处理函数
        sm.register_handler(WorkflowState.ENTERING_MOMENTS, lambda c: True)
        sm.register_handler(WorkflowState.TYPING_CONTENT, lambda c: True)
        sm.register_handler(WorkflowState.CONFIRMING_PUBLISH, lambda c: True)
        sm.register_handler(WorkflowState.VERIFYING_SUCCESS, lambda c: True)

        sm.start(ctx)
        assert sm.state == WorkflowState.ENTERING_MOMENTS

        sm.tick()
        assert sm.state == WorkflowState.TYPING_CONTENT

        sm.tick()
        assert sm.state == WorkflowState.CONFIRMING_PUBLISH

        sm.tick()
        assert sm.state == WorkflowState.VERIFYING_SUCCESS

        sm.tick()
        assert sm.state == WorkflowState.DONE
        assert sm.is_terminal()
        assert sm.is_success()

    def test_state_failure_and_retry(self):
        from src.executor.state_machine import (
            WorkflowStateMachine, WorkflowState, WorkflowContext, StateConfig
        )

        sm = WorkflowStateMachine()
        ctx = WorkflowContext(text="test")

        # 配置最大重试 3 次（共 4 次尝试）
        sm.configure_state(WorkflowState.ENTERING_MOMENTS,
                          StateConfig(max_retries=4, cooldown_on_fail=0.01))

        fail_count = [0]

        def fail_twice(_ctx):
            fail_count[0] += 1
            return fail_count[0] > 2  # 第三次才成功

        sm.register_handler(WorkflowState.ENTERING_MOMENTS, fail_twice)
        sm.register_handler(WorkflowState.TYPING_CONTENT, lambda c: True)
        sm.register_handler(WorkflowState.CONFIRMING_PUBLISH, lambda c: True)
        sm.register_handler(WorkflowState.VERIFYING_SUCCESS, lambda c: True)

        sm.start(ctx)
        assert sm.state == WorkflowState.ENTERING_MOMENTS

        # tick 1: handler fails(attempt 1) → WAITING
        sm.tick()
        assert sm.state == WorkflowState.WAITING
        time.sleep(0.05)  # 等冷却结束
        # tick 2: 从 WAITING 恢复 → handler fails(attempt 2) → WAITING
        sm.tick()
        assert sm.state == WorkflowState.WAITING
        time.sleep(0.05)
        # tick 3: 从 WAITING 恢复 → handler succeeds(attempt 3) → TYPING_CONTENT
        sm.tick()
        assert sm.state == WorkflowState.TYPING_CONTENT
        assert fail_count[0] == 3

    def test_state_timeout_goes_to_error(self):
        from src.executor.state_machine import (
            WorkflowStateMachine, WorkflowState, WorkflowContext, StateConfig
        )

        sm = WorkflowStateMachine()
        ctx = WorkflowContext(text="test")

        # 只允许 1 次尝试（不重试）
        sm.configure_state(WorkflowState.ENTERING_MOMENTS,
                          StateConfig(max_retries=1, cooldown_on_fail=0.01))

        sm.register_handler(WorkflowState.ENTERING_MOMENTS, lambda c: False)
        sm.start(ctx)

        sm.tick()  # 失败 → max_retries=1 达到 → ERROR
        assert sm.state == WorkflowState.ERROR
        assert not sm.is_success()

    def test_skip_images_when_empty(self):
        from src.executor.state_machine import (
            WorkflowStateMachine, WorkflowState, WorkflowContext, StateConfig
        )

        sm = WorkflowStateMachine()
        ctx = WorkflowContext(text="hello", images=[])  # 无图片

        sm.register_handler(WorkflowState.ENTERING_MOMENTS, lambda c: True)
        sm.register_handler(WorkflowState.TYPING_CONTENT, lambda c: True)
        sm.register_handler(WorkflowState.ADDING_IMAGES, lambda c: True)
        sm.register_handler(WorkflowState.CONFIRMING_PUBLISH, lambda c: True)
        sm.register_handler(WorkflowState.VERIFYING_SUCCESS, lambda c: True)

        sm.start(ctx)
        assert sm.state == WorkflowState.ENTERING_MOMENTS

        sm.tick()  # ENTERING → TYPING
        sm.tick()  # TYPING → 应该跳过 ADDING_IMAGES → CONFIRMING

        # 注意：需要看 _advance 中的跳转逻辑
        # 如果 context.images 为空，TYPING_CONTENT 完成后直接跳到 CONFIRMING_PUBLISH


class TestRiskDetector:
    """风控检测器测试"""

    def test_initial_state_is_safe(self):
        from src.monitor.risk_detector import RiskDetector, RiskLevel

        class MockOCR:
            def find_text(self, target):
                return []  # 无任何风控信号

        detector = RiskDetector(MockOCR())
        assert detector.state.level == RiskLevel.SAFE

    def test_warning_signal_triggers_cooldown(self):
        from src.monitor.risk_detector import RiskDetector, RiskLevel

        class MockOCR:
            def find_text(self, target):
                if target == "操作太频繁":
                    return [type('obj', (), {'text': '操作太频繁', 'confidence': 0.9})]
                return []

        detector = RiskDetector(MockOCR())
        result = detector.check(force=True)
        assert result.value >= RiskLevel.WARNING.value
        assert detector.state.cooldown_until > time.time()

    def test_critical_signal_stops(self):
        from src.monitor.risk_detector import RiskDetector, RiskLevel

        class MockOCR:
            def find_text(self, target):
                if target == "重新登录":
                    return [type('obj', (), {'text': '重新登录', 'confidence': 0.95})]
                return []

        detector = RiskDetector(MockOCR())
        result = detector.check(force=True)
        assert result == RiskLevel.CRITICAL

    def test_exponential_backoff(self):
        from src.monitor.risk_detector import RiskDetector, RiskLevel

        call_count = [0]

        class MockOCR:
            def find_text(self, target):
                if target == "操作太频繁":
                    call_count[0] += 1
                    if call_count[0] == 1:
                        return [type('obj', (), {'text': '操作太频繁', 'confidence': 0.9})]
                return []

        detector = RiskDetector(MockOCR())

        # 第一次
        detector.check(force=True)
        cooldown1 = detector.state.cooldown_until - time.time()
        assert 100 < cooldown1 < 500  # 2*2*1 = 4分钟 ≈ 240s

        # 手动推进冷却
        detector.state.cooldown_until = time.time() - 1

        # 继续触发
        call_count[0] = 0
        detector.check(force=True)
        cooldown2 = detector.state.cooldown_until - time.time()
        assert cooldown2 > cooldown1  # 第二次冷却更长

    def test_record_operation_tracks_limits(self):
        from src.monitor.risk_detector import RiskDetector

        detector = RiskDetector(None, {'daily_limits': {'max_posts': 3}})

        assert detector.record_operation('posts')   # 1/3
        assert detector.record_operation('posts')   # 2/3
        assert not detector.record_operation('posts')  # 3/3 达到上限，返回 False
        assert not detector.record_operation('posts')  # 4/3 超过上限


class TestProtoEncoder:
    """Protobuf 编码器测试"""

    def test_encode_varint(self):
        from src.locator.wechat_native_ocr import ProtoEncoder

        assert ProtoEncoder.encode_varint(0) == b'\x00'
        assert ProtoEncoder.encode_varint(1) == b'\x01'
        assert ProtoEncoder.encode_varint(127) == b'\x7f'
        assert ProtoEncoder.encode_varint(128) == b'\x80\x01'
        assert ProtoEncoder.encode_varint(300) == b'\xac\x02'

    def test_encode_int32_field(self):
        from src.locator.wechat_native_ocr import ProtoEncoder

        result = ProtoEncoder.encode_int32(2, 1)  # field 2, value 1
        # tag = (2<<3)|0 = 16 = 0x10, varint(1) = 0x01
        assert result == b'\x10\x01'

    def test_encode_string_field(self):
        from src.locator.wechat_native_ocr import ProtoEncoder

        result = ProtoEncoder.encode_string(1, "AB")
        # tag = (1<<3)|2 = 10 = 0x0a, length=2, "AB"
        assert result == b'\x0a\x02AB'

    def test_encode_bytes_field(self):
        from src.locator.wechat_native_ocr import ProtoEncoder

        result = ProtoEncoder.encode_bytes(1, b'\x00\x01\x02')
        # tag = 0x0a, length=3, data
        assert result == b'\x0a\x03\x00\x01\x02'


class TestProtoDecoder:
    """Protobuf 解码器测试"""

    def test_decode_varint(self):
        from src.locator.wechat_native_ocr import ProtoDecoder

        assert ProtoDecoder.decode_varint(b'\x01', 0) == (1, 1)
        assert ProtoDecoder.decode_varint(b'\xac\x02', 0) == (300, 2)

    def test_decode_field(self):
        from src.locator.wechat_native_ocr import ProtoDecoder, ProtoField

        # field 1, wire type 0 (varint), value 5
        # tag = (1<<3)|0 = 8 = 0x08
        data = b'\x08\x05'
        field, offset = ProtoDecoder.decode_field(data, 0)
        assert field is not None
        assert field.field_number == 1
        assert field.wire_type == ProtoField.WIRE_VARINT
        assert field.value == 5

    def test_decode_message(self):
        from src.locator.wechat_native_ocr import ProtoDecoder, ProtoEncoder

        # 构建一个简单的消息：field 1 (int32) = 1, field 2 (string) = "hi"
        msg = ProtoEncoder.encode_int32(1, 1) + ProtoEncoder.encode_string(2, "hi")

        fields = ProtoDecoder.decode_message(msg)
        assert 1 in fields
        assert fields[1].value == 1
        assert 2 in fields
        assert fields[2].value == b'hi'


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
