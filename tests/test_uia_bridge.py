import subprocess
from unittest.mock import Mock

from src.executor.uia_bridge import UIABridge


def test_window_monitor_starts_without_a_console_window(
    monkeypatch,
    tmp_path,
) -> None:
    helper = tmp_path / "WeChatUIA.exe"
    helper.write_bytes(b"")
    process = Mock()
    process.stdout = []
    popen = Mock(return_value=process)
    monkeypatch.setattr("src.executor.uia_bridge.subprocess.Popen", popen)

    bridge = UIABridge(str(helper))
    bridge.start_window_monitor(run_in_background=False)

    assert popen.call_args.kwargs["creationflags"] == getattr(
        subprocess,
        "CREATE_NO_WINDOW",
        0,
    )
