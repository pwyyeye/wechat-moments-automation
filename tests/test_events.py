"""
EventBus 单元测试 — 不依赖微信，完全独立运行。

运行: python -m pytest tests/test_events.py -v
"""

import time
import threading
import pytest

# 需要在项目根目录运行，或设置 PYTHONPATH
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.core.events import EventBus, Event, EventType


class TestEventBus:
    """EventBus 核心功能测试"""

    def setup_method(self):
        self.bus = EventBus(history_size=50)

    def test_emit_and_subscribe(self):
        """测试发布/订阅"""
        received = []

        self.bus.on(EventType.TEXT_APPEARED, lambda e: received.append(e))
        event = Event(EventType.TEXT_APPEARED, "test", {'text': 'hello'})
        self.bus.emit(event)

        assert len(received) == 1
        assert received[0].payload['text'] == 'hello'
        assert received[0].source == 'test'

    def test_multiple_subscribers(self):
        """测试多个订阅者"""
        results = []

        self.bus.on(EventType.STEP_COMPLETED, lambda e: results.append(1))
        self.bus.on(EventType.STEP_COMPLETED, lambda e: results.append(2))
        self.bus.emit(Event(EventType.STEP_COMPLETED, "test", {}))

        assert results == [1, 2]

    def test_wildcard_subscriber(self):
        """测试通配符订阅"""
        events = []

        self.bus.on_any(lambda e: events.append(e.type))
        self.bus.emit(Event(EventType.TEXT_APPEARED, "t", {}))
        self.bus.emit(Event(EventType.STEP_COMPLETED, "t", {}))

        assert len(events) == 2
        assert EventType.TEXT_APPEARED in events
        assert EventType.STEP_COMPLETED in events

    def test_once(self):
        """测试一次性订阅"""
        count = [0]

        self.bus.once(EventType.TEXT_APPEARED, lambda e: count.__setitem__(0, count[0] + 1))
        self.bus.emit(Event(EventType.TEXT_APPEARED, "t", {}))
        self.bus.emit(Event(EventType.TEXT_APPEARED, "t", {}))  # 第二次不应触发

        assert count[0] == 1

    def test_wait_for(self):
        """测试等待事件"""
        def delayed_emit():
            time.sleep(0.1)
            self.bus.emit(Event(EventType.TEXT_APPEARED, "test", {'text': 'arrived'}))

        t = threading.Thread(target=delayed_emit, daemon=True)
        t.start()

        event = self.bus.wait_for(EventType.TEXT_APPEARED, timeout=2.0)
        assert event is not None
        assert event.payload['text'] == 'arrived'

    def test_wait_for_timeout(self):
        """测试等待超时"""
        event = self.bus.wait_for(EventType.TEXT_APPEARED, timeout=0.1)
        assert event is None

    def test_wait_for_payload_match(self):
        """测试带载荷匹配的等待"""
        def emit():
            time.sleep(0.05)
            self.bus.emit(Event(EventType.TEXT_APPEARED, "t", {'matched': 'wrong'}))
            time.sleep(0.05)
            self.bus.emit(Event(EventType.TEXT_APPEARED, "t", {'matched': 'correct'}))

        threading.Thread(target=emit, daemon=True).start()

        event = self.bus.wait_for(
            EventType.TEXT_APPEARED,
            payload_match={'matched': 'correct'},
            timeout=2.0,
        )
        assert event is not None
        assert event.payload['matched'] == 'correct'

    def test_wait_any(self):
        """测试等待任意事件"""
        def emit():
            time.sleep(0.1)
            self.bus.emit(Event(EventType.TIMER_EXPIRED, "t", {}))

        threading.Thread(target=emit, daemon=True).start()

        event = self.bus.wait_any(
            [EventType.TEXT_APPEARED, EventType.TIMER_EXPIRED],
            timeout=2.0,
        )
        assert event is not None
        assert event.type == EventType.TIMER_EXPIRED

    def test_history(self):
        """测试事件历史"""
        for i in range(5):
            self.bus.emit(Event(EventType.STEP_STARTED, "test", {'index': i}))

        history = self.bus.history(EventType.STEP_STARTED)
        assert len(history) == 5
        assert history[0].payload['index'] == 0
        assert history[-1].payload['index'] == 4

    def test_history_cap(self):
        """测试历史记录上限"""
        bus = EventBus(history_size=10)
        for i in range(20):
            bus.emit(Event(EventType.STEP_STARTED, "test", {'i': i}))

        history = bus.history()
        assert len(history) == 10
        assert history[0].payload['i'] == 10  # 最早的已被淘汰
        assert history[-1].payload['i'] == 19

    def test_handler_exception_does_not_crash_bus(self):
        """测试处理器异常不影响事件总线"""
        self.bus.on(EventType.TEXT_APPEARED, lambda e: 1 / 0)  # 故意抛异常
        self.bus.on(EventType.TEXT_APPEARED, lambda e: ...)

        # 不应抛异常
        self.bus.emit(Event(EventType.TEXT_APPEARED, "test", {}))

    def test_off(self):
        """测试取消订阅"""
        results = []

        def handler(e):
            results.append(1)

        self.bus.on(EventType.TEXT_APPEARED, handler)
        self.bus.emit(Event(EventType.TEXT_APPEARED, "t", {}))
        assert len(results) == 1

        self.bus.off(EventType.TEXT_APPEARED, handler)
        self.bus.emit(Event(EventType.TEXT_APPEARED, "t", {}))
        assert len(results) == 1  # 不再增加


class TestEventType:
    """EventType 枚举测试"""

    def test_all_event_types_have_unique_values(self):
        values = [e.value for e in EventType]
        assert len(values) == len(set(values))

    def test_event_type_string_format(self):
        assert EventType.TEXT_APPEARED.value == "text.appeared"
        assert EventType.WINDOW_MOVED.value == "window.moved"
        assert EventType.STEP_COMPLETED.value == "step.completed"


class TestEvent:
    """Event 数据类测试"""

    def test_event_creation(self):
        e = Event(EventType.TEXT_APPEARED, "ocr", {'text': 'hello', 'confidence': 0.9})
        assert e.type == EventType.TEXT_APPEARED
        assert e.source == "ocr"
        assert e.payload['text'] == 'hello'
        assert e.payload['confidence'] == 0.9
        assert e.timestamp > 0

    def test_event_default_payload(self):
        e = Event(EventType.STEP_COMPLETED, "test")
        assert e.payload == {}

    def test_event_repr(self):
        e = Event(EventType.TEXT_APPEARED, "ocr", {'text': 'hello'})
        r = repr(e)
        assert 'text.appeared' in r
        assert 'ocr' in r
        assert 'text=hello' in r


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
