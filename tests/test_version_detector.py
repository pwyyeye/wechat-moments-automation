from pathlib import Path
from unittest.mock import patch

from src.executor.version_detector import VersionDetector, WeChatVersion
from src.executor.wechat_discovery import WeChatEnvironment


def environment(executable: Path) -> WeChatEnvironment:
    return WeChatEnvironment(
        process_name="Weixin.exe",
        process_id=123,
        install_dir=executable.parent,
        version_dir=executable.parent,
        executable=executable,
        ocr_binary=None,
        ocr_dll=None,
        is_64bit=True,
    )


def test_detects_weixin_version_from_running_process(tmp_path: Path) -> None:
    executable = tmp_path / "Weixin.exe"
    executable.touch()
    expected = WeChatVersion(4, 1, 13, 12, "4.1.13.12")

    with (
        patch("src.executor.wechat_discovery.discover_from_window", return_value=None),
        patch(
            "src.executor.wechat_discovery.discover_wechat",
            return_value=environment(executable),
        ),
        patch.object(VersionDetector, "_load_last_version"),
        patch.object(VersionDetector, "_read_pe_version", return_value=expected) as read,
    ):
        detector = VersionDetector()
        assert detector.get_version() == expected

    read.assert_called_once_with(str(executable))


def test_detects_weixin_executable_in_explicit_directory(tmp_path: Path) -> None:
    executable = tmp_path / "Weixin.exe"
    executable.touch()
    expected = WeChatVersion(4, 1, 13, 12, "4.1.13.12")

    with (
        patch.object(VersionDetector, "_load_last_version"),
        patch.object(VersionDetector, "_read_pe_version", return_value=expected) as read,
    ):
        detector = VersionDetector(str(tmp_path))
        assert detector.get_version() == expected

    read.assert_called_once_with(str(executable))
