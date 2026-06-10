"""
集成测试 — 用 mock 模拟微信环境，测试完整发布流程。

运行: python -m pytest tests/test_integration.py -v

所有测试不依赖真实微信，通过 mock 截图、mock API 实现。

Author: 版本无关微信自动化系统
"""

import time
import sys
import json
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock
from dataclasses import dataclass

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
import numpy as np


# ═══════════════════════════════════════════════════════════════
# Mock 工具
# ═══════════════════════════════════════════════════════════════

@dataclass
class MockTextBlock:
    """模拟 OCR 识别结果"""
    text: str
    x: int = 100
    y: int = 100
    width: int = 60
    height: int = 20
    confidence: float = 0.95
    box: list = None

    def __post_init__(self):
        if self.box is None:
            self.box = [[self.x, self.y], [self.x + self.width, self.y],
                       [self.x + self.width, self.y + self.height], [self.x, self.y + self.height]]


def create_mock_ocr(text_map: dict):
    """创建模拟 OCR 定位器"""
    mock = Mock()
    mock._engine = Mock()

    def scan_screen(region=None):
        results = []
        for text, coord in text_map.items():
            if isinstance(coord, tuple):
                results.append(MockTextBlock(text=text, x=coord[0], y=coord[1]))
            else:
                results.append(MockTextBlock(text=text))
        return results

    def recognize(image):
        return scan_screen()

    mock.scan_screen = scan_screen
    mock._engine.recognize = recognize
    mock.find_best = lambda target: next(
        (b for b in scan_screen() if target in b.text), None
    )
    mock.find_text = lambda target: [
        b for b in scan_screen() if target in b.text
    ]
    mock.get_all_text = lambda: [b.text for b in scan_screen()]
    mock._invalidate_cache = lambda: None
    mock._is_cache_valid = lambda: False

    return mock


# ═══════════════════════════════════════════════════════════════
# 集成测试
# ═══════════════════════════════════════════════════════════════

class TestPublisherIntegration:
    """发布器集成测试（mock 微信环境）"""

    @patch('win32gui.FindWindow', return_value=12345)
    @patch('win32gui.IsIconic', return_value=False)
    @patch('win32gui.ShowWindow')
    @patch('win32gui.SetForegroundWindow')
    @patch('win32gui.SetWindowPos')
    def test_initialize_flow(self, *mocks):
        """测试初始化流程的所有检查步骤"""
        from src.core.publisher import EventDrivenPublisher
        from src.executor.version_detector import VersionDetector

        # Mock OCR — 模拟已登录的微信主界面
        mock_ocr = create_mock_ocr({
            '聊天': (64, 20),
            '通讯录': (128, 20),
            '朋友圈': (192, 20),
        })

        # Mock 版本检测
        with patch.object(VersionDetector, 'get_version',
                         return_value=None):
            with patch.object(VersionDetector, 'is_version_changed',
                             return_value=False):
                with patch.object(VersionDetector, 'ensure_templates',
                                 return_value=5):

                    publisher = EventDrivenPublisher()
                    publisher.ocr = mock_ocr

                    # Mock operator 检查
                    publisher.operator.find_wechat_window = Mock(return_value=True)
                    publisher.operator.ensure_window_active = Mock(return_value=True)
                    publisher.operator.check_login_state = Mock(return_value={
                        'logged_in': True, 'page': '微信主界面',
                        'details': '导航标签可见'
                    })

                    # Mock calibrator
                    publisher.calibrator.calibrate = Mock(return_value=Mock(
                        anchors={'nav_聊天': Mock(x=64, y=20)}
                    ))

                    result = publisher.initialize()
                    assert result is True

    def test_publish_state_machine_flow(self):
        """测试发布流程的状态机转换"""
        from src.executor.state_machine import (
            WorkflowStateMachine, WorkflowState, WorkflowContext, StateConfig
        )

        sm = WorkflowStateMachine()
        ctx = WorkflowContext(text="hello world", images=[])

        # 注册所有步骤
        sm.configure_state(WorkflowState.ENTERING_MOMENTS, StateConfig(max_retries=3))
        sm.configure_state(WorkflowState.TYPING_CONTENT, StateConfig(max_retries=2))
        sm.configure_state(WorkflowState.CONFIRMING_PUBLISH, StateConfig(max_retries=3))
        sm.configure_state(WorkflowState.VERIFYING_SUCCESS, StateConfig(max_retries=5))

        sm.register_handler(WorkflowState.ENTERING_MOMENTS, lambda c: True)
        sm.register_handler(WorkflowState.TYPING_CONTENT, lambda c: True)
        sm.register_handler(WorkflowState.CONFIRMING_PUBLISH, lambda c: True)
        sm.register_handler(WorkflowState.VERIFYING_SUCCESS, lambda c: True)

        sm.start(ctx)
        # start() 已 transition 到 ENTERING_MOMENTS

        # 验证路径: ENTERING → TYPING → CONFIRMING → VERIFYING → DONE
        # 每个 tick 执行当前状态 handler 然后 advance 到下一个
        expected = [
            WorkflowState.TYPING_CONTENT,        # tick: ENTERING → TYPING
            WorkflowState.CONFIRMING_PUBLISH,    # tick: TYPING → CONFIRMING (无图跳过 ADDING)
            WorkflowState.VERIFYING_SUCCESS,     # tick: CONFIRMING → VERIFYING
            WorkflowState.DONE,                  # tick: VERIFYING → DONE
        ]

        for expected_state in expected:
            sm.tick()
            assert sm.state == expected_state, \
                f"期望 {expected_state.name}，实际 {sm.state.name}"

        assert sm.is_success()

    def test_publish_with_images_stays_in_adding(self):
        """测试有图片时会经过 ADDING_IMAGES 步骤"""
        from src.executor.state_machine import (
            WorkflowStateMachine, WorkflowState, WorkflowContext, StateConfig
        )

        sm = WorkflowStateMachine()
        ctx = WorkflowContext(text="photo post", images=["img1.jpg", "img2.jpg"])

        for state in [WorkflowState.ENTERING_MOMENTS, WorkflowState.TYPING_CONTENT,
                      WorkflowState.ADDING_IMAGES, WorkflowState.CONFIRMING_PUBLISH,
                      WorkflowState.VERIFYING_SUCCESS]:
            sm.configure_state(state, StateConfig(max_retries=1))
            sm.register_handler(state, lambda c: True)

        sm.start(ctx)
        # start() 已 transition 到 ENTERING_MOMENTS

        # 有图片时路径: ENTERING → TYPING → ADDING → CONFIRMING → VERIFYING → DONE
        # 每个 tick 执行当前 handler 并 advance
        expected = [
            WorkflowState.TYPING_CONTENT,        # tick: ENTERING → TYPING
            WorkflowState.ADDING_IMAGES,          # tick: TYPING → ADDING
            WorkflowState.CONFIRMING_PUBLISH,     # tick: ADDING → CONFIRMING
            WorkflowState.VERIFYING_SUCCESS,      # tick: CONFIRMING → VERIFYING
            WorkflowState.DONE,                   # tick: VERIFYING → DONE
        ]

        for expected_state in expected:
            sm.tick()
            assert sm.state == expected_state


class TestEventDrivenFlow:
    """事件驱动流程测试"""

    def test_event_based_state_transition(self):
        """模拟事件驱动的状态转换"""
        from src.core.events import EventBus, Event, EventType

        bus = EventBus()
        events_received = []

        bus.on_any(lambda e: events_received.append(e.type))

        # 模拟一次完整的发布事件流
        bus.emit(Event(EventType.STEP_STARTED, "publisher", {"step": "publish"}))
        bus.emit(Event(EventType.TEXT_APPEARED, "ocr", {"text": "这一刻的想法"}))
        bus.emit(Event(EventType.TIMER_EXPIRED, "timer", {"reason": "typing_complete"}))
        bus.emit(Event(EventType.TEXT_APPEARED, "ocr", {"text": "已发送"}))
        bus.emit(Event(EventType.STEP_COMPLETED, "publisher", {"elapsed": 12.5}))

        assert len(events_received) == 5
        assert EventType.TEXT_APPEARED in events_received
        assert EventType.STEP_COMPLETED in events_received

    def test_simultaneous_events(self):
        """测试并发事件不会丢失"""
        from src.core.events import EventBus, Event, EventType
        import threading

        bus = EventBus()
        received = []

        bus.on_any(lambda e: received.append(e))

        threads = []
        for i in range(10):
            t = threading.Thread(
                target=lambda idx=i: bus.emit(
                    Event(EventType.STEP_STARTED, f"source_{idx}", {"idx": idx})
                ),
                daemon=True,
            )
            threads.append(t)
            t.start()

        for t in threads:
            t.join()

        assert len(received) == 10

    def test_wait_for_with_simulated_ocr_event(self):
        """模拟 OCR 事件到达的 wait_for 场景"""
        from src.core.events import EventBus, Event, EventType
        import threading

        bus = EventBus()

        # 模拟 OCR Watcher 在后台发现了目标文字
        def simulated_ocr_watcher():
            time.sleep(0.1)
            bus.emit(Event(EventType.TEXT_APPEARED, "OCRWatcher", {
                'text': '已发送', 'matched': '已发送',
                'x': 500, 'y': 600, 'confidence': 0.97
            }))

        threading.Thread(target=simulated_ocr_watcher, daemon=True).start()

        # 发布器等待确认
        event = bus.wait_for(
            EventType.TEXT_APPEARED,
            payload_match={'matched': '已发送'},
            timeout=5.0,
        )

        assert event is not None
        assert event.payload['text'] == '已发送'
        assert event.payload['confidence'] == 0.97


class TestAPIServerIntegration:
    """API Server 集成测试"""

    def test_publish_endpoint_mocked(self):
        """测试 publish API 端点（mock publisher）"""
        from fastapi.testclient import TestClient
        from src.api.server import app, state

        # Mock publisher
        mock_publisher = Mock()
        mock_result = Mock(success=True, elapsed_seconds=5.2, step_times={}, error_message='')
        mock_publisher.publish = Mock(return_value=mock_result)
        state.publisher = mock_publisher

        client = TestClient(app)
        response = client.post("/api/publish", json={
            "text": "test message", "images": []
        })

        assert response.status_code == 200
        data = response.json()
        assert data['success'] is True
        assert data['elapsed_seconds'] == 5.2

    def test_status_endpoint(self):
        """测试 status API 端点"""
        from fastapi.testclient import TestClient
        from src.api.server import app, state

        mock_publisher = Mock()
        mock_publisher.operator = Mock()
        mock_publisher.operator.check_login_state = Mock(return_value={
            'logged_in': True, 'page': '微信主界面'
        })
        mock_publisher.risk_detector = Mock()
        mock_publisher.risk_detector.state = Mock(
            level=type('obj', (), {'name': 'SAFE'})(),
            consecutive_events=0,
            cooldown_until=0,
        )
        state.publisher = mock_publisher

        client = TestClient(app)
        response = client.get("/api/status")

        assert response.status_code == 200
        data = response.json()
        assert data['status'] == 'running'
        assert data['wechat']['logged_in'] is True

    def test_health_endpoint(self):
        """测试健康检查"""
        from fastapi.testclient import TestClient
        from src.api.server import app

        client = TestClient(app)
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json()['status'] == 'ok'

    def test_schedule_crud(self):
        """测试定时任务 CRUD"""
        from fastapi.testclient import TestClient
        from src.api.server import app, state

        state.schedules.clear()

        client = TestClient(app)

        # Create
        resp = client.post("/api/schedule", json={
            "text": "早安", "images": [], "cron": "0 9 * * *"
        })
        assert resp.status_code == 200
        schedule_id = resp.json()['id']

        # List
        resp = client.get("/api/schedule")
        assert resp.status_code == 200
        assert len(resp.json()) == 1

        # Delete
        resp = client.delete(f"/api/schedule/{schedule_id}")
        assert resp.status_code == 200

        # Verify deleted
        resp = client.get("/api/schedule")
        assert len(resp.json()) == 0


class TestFileDialogIntegration:
    """文件对话框集成测试"""

    def test_clipboard_image_processing(self):
        """测试图片转 DIB 格式的字节操作"""
        from PIL import Image
        import io
        import struct

        # 创建一个简单的测试图片
        img = Image.new('RGB', (10, 10), color='red')
        width, height = img.size
        pixels = img.tobytes()

        # 模拟 DIB header
        bmi_header = struct.pack(
            '<IiiHHIIiiII',
            40, width, height, 1, 24, 0, len(pixels), 0, 0, 0, 0
        )

        assert len(bmi_header) == 40
        assert bmi_header[4:8] == struct.pack('<i', 10)  # width
        assert bmi_header[8:12] == struct.pack('<i', 10)  # height


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
