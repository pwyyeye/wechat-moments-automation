import os
import subprocess
import sys
from pathlib import Path

from src.executor.wechat_discovery import (
    _get_process_exe_path,
    _get_process_name,
)


def test_current_process_is_discovered_without_spawning_tasklist(monkeypatch):
    def fail_if_spawned(*_args, **_kwargs):
        raise AssertionError("process discovery must not spawn a console command")

    monkeypatch.setattr(subprocess, "run", fail_if_spawned)

    assert _get_process_name(os.getpid()).lower() == Path(sys.executable).name.lower()


def test_current_process_path_uses_native_windows_query(monkeypatch):
    def fail_if_spawned(*_args, **_kwargs):
        raise AssertionError("process discovery must not spawn a console command")

    monkeypatch.setattr(subprocess, "run", fail_if_spawned)

    discovered = _get_process_exe_path(os.getpid())

    assert discovered is not None
    assert Path(discovered).is_file()
    assert Path(discovered).name.lower() == Path(sys.executable).name.lower()
