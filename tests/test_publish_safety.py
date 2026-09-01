from unittest.mock import Mock

from src.core.publisher import EventDrivenPublisher, PublishTask
from src.executor.state_machine import WorkflowContext, WorkflowState, WorkflowStateMachine
from src.locator.ocr_locator import TextBlock


def build_publisher() -> EventDrivenPublisher:
    publisher = EventDrivenPublisher.__new__(EventDrivenPublisher)
    publisher.bus = Mock()
    publisher._stats = []
    publisher._pre_check = Mock(return_value=True)
    publisher._step_enter_moments = Mock(return_value=True)
    publisher._step_type_text = Mock(return_value=True)
    publisher._step_add_images = Mock(return_value=True)
    publisher._step_publish = Mock(return_value=True)
    publisher.operator = Mock()
    publisher.operator.close_moments_window.return_value = True
    return publisher


def test_publish_defaults_to_stopping_before_final_click() -> None:
    publisher = build_publisher()

    result = publisher.publish(PublishTask(text="安全预览"))

    assert result.success
    assert result.stopped_before_publish
    assert not result.published
    publisher._step_publish.assert_not_called()
    publisher._pre_check.assert_called_once_with(will_publish=False)
    publisher.operator.close_moments_window.assert_not_called()


def test_publish_requires_explicit_confirmation_for_final_click() -> None:
    publisher = build_publisher()
    before_final_click = Mock()

    result = publisher.publish(
        PublishTask(
            text="确认发布",
            confirm_publish=True,
            before_final_click=before_final_click,
        )
    )

    assert result.success
    assert result.published
    assert result.final_click_intent
    assert not result.stopped_before_publish
    before_final_click.assert_called_once_with()
    publisher._step_publish.assert_called_once_with("确认发布")
    publisher._pre_check.assert_called_once_with(will_publish=True)
    publisher.operator.close_moments_window.assert_called_once_with()


def test_uncertain_publish_result_preserves_the_moments_window() -> None:
    publisher = build_publisher()
    publisher._step_publish.return_value = False

    result = publisher.publish(
        PublishTask(text="结果待确认", confirm_publish=True)
    )

    assert not result.success
    assert result.final_click_intent
    publisher.operator.close_moments_window.assert_not_called()


def test_cleanup_failure_does_not_change_a_confirmed_publish_result() -> None:
    publisher = build_publisher()
    publisher.operator.close_moments_window.side_effect = RuntimeError(
        "close failed"
    )

    result = publisher.publish(
        PublishTask(text="已经发布", confirm_publish=True)
    )

    assert result.success
    assert result.published


def test_publish_stops_when_final_click_intent_cannot_be_persisted() -> None:
    publisher = build_publisher()

    def fail_before_click() -> None:
        raise RuntimeError("ledger unavailable")

    result = publisher.publish(
        PublishTask(
            text="不会点击",
            confirm_publish=True,
            before_final_click=fail_before_click,
        )
    )

    assert not result.success
    assert not result.final_click_intent
    assert "ledger unavailable" in result.error_message
    publisher._step_publish.assert_not_called()


def test_desktop_preselected_image_is_not_added_twice() -> None:
    publisher = build_publisher()

    def enter_moments(images) -> bool:
        publisher._prepared_image_count = 1
        return True

    publisher._step_enter_moments.side_effect = enter_moments

    result = publisher.publish(
        PublishTask(text="安全预览", images=["first.png", "second.png"])
    )

    assert result.success
    publisher._step_add_images.assert_called_once_with(["second.png"])


def test_state_machine_stops_before_confirming_publish() -> None:
    state_machine = WorkflowStateMachine()
    confirming_handler = Mock(return_value=True)
    state_machine.register_handler(WorkflowState.ENTERING_MOMENTS, lambda context: True)
    state_machine.register_handler(WorkflowState.TYPING_CONTENT, lambda context: True)
    state_machine.register_handler(WorkflowState.CONFIRMING_PUBLISH, confirming_handler)
    state_machine.start(WorkflowContext(text="安全预览", confirm_publish=False))

    state_machine.tick()
    state_machine.tick()

    assert state_machine.state == WorkflowState.READY_FOR_REVIEW
    assert state_machine.is_terminal()
    assert state_machine.is_ready_for_review()
    confirming_handler.assert_not_called()


def test_final_publish_verification_does_not_repeat_the_click(monkeypatch) -> None:
    publisher = EventDrivenPublisher.__new__(EventDrivenPublisher)
    publisher.sim = Mock()
    publisher.operator = Mock()
    publisher.operator.click_element.return_value = True
    publisher.operator.activate_moments_window.return_value = True
    publisher.operator.active_window_region.return_value = (1000, 400, 600, 800)
    publisher.ocr = Mock()
    publisher.ocr.scan_screen.side_effect = [
        [
            TextBlock("AI 自动化发布验证", 1, 1, 10, 10, 0.9, []),
            TextBlock("朋友圈", 1, 1, 10, 10, 0.9, []),
            TextBlock("1分钟前", 1, 1, 10, 10, 0.9, []),
        ],
    ]
    monkeypatch.setattr("src.core.publisher.time.sleep", Mock())

    assert publisher._step_publish("AI 自动化发布验证")
    publisher.operator.click_element.assert_called_once()
