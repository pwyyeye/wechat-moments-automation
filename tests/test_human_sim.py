from unittest.mock import Mock

from src.executor.human_sim import HumanSimulator


def build_simulator() -> HumanSimulator:
    simulator = HumanSimulator.__new__(HumanSimulator)
    simulator._paste_with_delay = Mock()
    simulator._type_character_by_character = Mock()
    return simulator


def test_short_chinese_text_uses_clipboard() -> None:
    simulator = build_simulator()

    simulator.type_text("中文短文案")

    simulator._paste_with_delay.assert_called_once_with("中文短文案")
    simulator._type_character_by_character.assert_not_called()


def test_short_ascii_text_can_use_keyboard_simulation() -> None:
    simulator = build_simulator()

    simulator.type_text("hello")

    simulator._type_character_by_character.assert_called_once_with("hello")
    simulator._paste_with_delay.assert_not_called()
