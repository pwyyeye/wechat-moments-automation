from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from src.agent.admin.gui import LocalAgentClient, LocalAgentError, stop_agent_backend
from src.agent.admin.launcher import ensure_agent_backend
from src.agent.single_instance import SingleInstance


def test_native_gui_client_reads_loopback_status_without_browser(monkeypatch):
    captured = {}

    def request(method, url, **kwargs):
        captured.update(method=method, url=url, kwargs=kwargs)
        return httpx.Response(
            200,
            json={"agent": {"version": "0.4.3"}, "sources": []},
            request=httpx.Request(method, url),
        )

    monkeypatch.setattr("src.agent.admin.gui.httpx.request", request)
    client = LocalAgentClient("http://127.0.0.1:17821")

    status = client.status()

    assert status["agent"]["version"] == "0.4.3"
    assert captured["method"] == "GET"
    assert captured["url"] == "http://127.0.0.1:17821/api/status"
    assert captured["kwargs"]["headers"]["X-Local-Agent-Action"] == "confirmed"


def test_native_gui_client_writes_source_configuration(monkeypatch):
    captured = {}

    def request(method, url, **kwargs):
        captured.update(method=method, url=url, kwargs=kwargs)
        return httpx.Response(
            200,
            json={"saved": True},
            request=httpx.Request(method, url),
        )

    monkeypatch.setattr("src.agent.admin.gui.httpx.request", request)
    payload = {
        "id": "source-a",
        "name": "Source A",
        "baseUrl": "https://content.example.test/openapi/publisher-agent/v1",
    }

    result = LocalAgentClient("http://127.0.0.1:17821").update_source(
        "source-a", payload
    )

    assert result == {"saved": True}
    assert captured["method"] == "PUT"
    assert captured["url"].endswith("/api/sources/source-a")
    assert captured["kwargs"]["json"] == payload


def test_native_gui_client_surfaces_structured_agent_errors(monkeypatch):
    def request(method, url, **kwargs):
        return httpx.Response(
            409,
            json={
                "detail": {
                    "code": "PUBLISH_ACTIVE",
                    "message": "当前有发布任务正在执行",
                }
            },
            request=httpx.Request(method, url),
        )

    monkeypatch.setattr("src.agent.admin.gui.httpx.request", request)

    with pytest.raises(LocalAgentError, match="PUBLISH_ACTIVE"):
        LocalAgentClient("http://127.0.0.1:17821").shutdown()


def test_native_gui_client_reports_unreachable_backend(monkeypatch):
    def request(method, url, **kwargs):
        raise httpx.ConnectError("refused", request=httpx.Request(method, url))

    monkeypatch.setattr("src.agent.admin.gui.httpx.request", request)

    with pytest.raises(LocalAgentError, match="无法连接本机 Agent"):
        LocalAgentClient("http://127.0.0.1:17821").status()


def test_native_gui_client_queries_filtered_logs(monkeypatch):
    captured = {}

    def request(method, url, **kwargs):
        captured.update(method=method, url=url, kwargs=kwargs)
        return httpx.Response(
            200,
            json={"entries": [], "logDirectory": "C:/agent/logs"},
            request=httpx.Request(method, url),
        )

    monkeypatch.setattr("src.agent.admin.gui.httpx.request", request)

    LocalAgentClient("http://127.0.0.1:17821").logs(
        level="WARNING",
        query="SOURCE_AUTH_FAILED",
    )

    assert captured["url"].endswith("/api/logs")
    assert captured["kwargs"]["params"] == {
        "level": "WARNING",
        "limit": 300,
        "query": "SOURCE_AUTH_FAILED",
    }


def test_native_gui_client_creates_local_schedule(monkeypatch):
    captured = {}

    def request(method, url, **kwargs):
        captured.update(method=method, url=url, kwargs=kwargs)
        return httpx.Response(
            201,
            json={"task_id": "local-1", "state": "pending"},
            request=httpx.Request(method, url),
        )

    monkeypatch.setattr("src.agent.admin.gui.httpx.request", request)
    payload = {
        "text": "定时文案",
        "imagePaths": ["D:/pictures/one.png"],
        "scheduledAt": "2026-09-01T18:30:00+08:00",
    }

    result = LocalAgentClient("http://127.0.0.1:17821").create_local_schedule(payload)

    assert result["state"] == "pending"
    assert captured["method"] == "POST"
    assert captured["url"].endswith("/api/local-schedules")
    assert captured["kwargs"]["json"] == payload


class _ShutdownClient:
    def __init__(self, *, shutdown_error: Exception | None = None, stopped=True):
        self.shutdown_error = shutdown_error
        self.stopped = stopped
        self.shutdown_calls = 0
        self.wait_timeouts = []

    def shutdown(self):
        self.shutdown_calls += 1
        if self.shutdown_error:
            raise self.shutdown_error

    def wait_until_stopped(self, timeout):
        self.wait_timeouts.append(timeout)
        return self.stopped


def test_native_gui_close_waits_for_backend_to_stop():
    client = _ShutdownClient()

    stop_agent_backend(client, timeout=12.0)

    assert client.shutdown_calls == 1
    assert client.wait_timeouts == [12.0]


def test_native_gui_close_accepts_shutdown_response_connection_loss():
    client = _ShutdownClient(
        shutdown_error=LocalAgentError("无法连接本机 Agent"),
        stopped=True,
    )

    stop_agent_backend(client)

    assert client.shutdown_calls == 1
    assert client.wait_timeouts == [1.0]


def test_native_gui_close_stays_open_when_backend_cannot_stop():
    client = _ShutdownClient(stopped=False)

    with pytest.raises(LocalAgentError, match="未能在 15 秒内退出"):
        stop_agent_backend(client)


def test_native_gui_window_close_uses_safe_shutdown_and_scrollable_panels():
    source = (
        Path(__file__).resolve().parents[1] / "src" / "agent" / "admin" / "gui.py"
    ).read_text(encoding="utf-8")

    assert 'protocol("WM_DELETE_WINDOW", self._on_window_close)' in source
    assert 'protocol("WM_DELETE_WINDOW", self.root.destroy)' not in source
    assert "stop_agent_backend(self.client)" in source
    assert "create_window((0, 0), window=content" in source
    assert 'orient="vertical"' in source
    assert 'bind_all("<MouseWheel>"' in source
    assert "after_idle(self._reset_panel_scroll_positions)" in source


def test_operator_entry_points_use_native_control_panel():
    root = Path(__file__).resolve().parents[1]
    installer = (root / "packaging" / "installer.iss").read_text(encoding="utf-8")
    startup = (root / "scripts" / "install-startup.ps1").read_text(encoding="utf-8")
    app = (root / "src" / "agent" / "app.py").read_text(encoding="utf-8")

    assert 'Parameters: "--agent-ui"' in installer
    assert 'Parameters: "--agent"; Flags: nowait' not in installer
    assert '#define AppName "微信小助手"' in installer
    assert "OutputBaseFilename=微信小助手-{#AppVersion}-setup" in installer
    assert 'Excludes: "_internal\\ucrtbase.dll"' in installer
    assert "SolidCompression=no" in installer
    assert '" --agent' in startup
    assert "CurrentVersion\\Run" in startup
    assert 'ValueName: "WechatPublisherAgent"' in installer
    assert "Register-ScheduledTask" not in startup
    assert "webbrowser" not in app


class _LauncherClient:
    def __init__(self, *, ready: bool, version: str = "0.6.4") -> None:
        self.ready = ready
        self.version = version
        self.shutdown_called = False
        self.stopped = True

    def wait_until_ready(self, _timeout: float) -> bool:
        return self.ready

    def status(self) -> dict:
        return {"agent": {"version": self.version}}

    def shutdown(self) -> None:
        self.shutdown_called = True

    def wait_until_stopped(self, _timeout: float) -> bool:
        return self.stopped


def test_ui_launcher_reuses_matching_backend():
    calls = []
    client = _LauncherClient(ready=True)

    started = ensure_agent_backend(
        client,
        expected_version="0.6.4",
        command=["agent.exe", "--agent"],
        working_directory="D:/agent",
        popen=lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    assert started is False
    assert client.shutdown_called is False
    assert calls == []


def test_ui_launcher_replaces_old_backend_and_starts_once():
    calls = []
    client = _LauncherClient(ready=True, version="0.6.3")

    started = ensure_agent_backend(
        client,
        expected_version="0.6.4",
        command=["agent.exe", "--agent", "--agent-config", "D:/agent/config.yaml"],
        working_directory="D:/agent",
        popen=lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    assert started is True
    assert client.shutdown_called is True
    assert len(calls) == 1
    assert calls[0][0][0][-2:] == ["--agent-config", "D:/agent/config.yaml"]


def test_ui_launcher_refuses_to_overlap_backend_that_cannot_stop():
    client = _LauncherClient(ready=True, version="0.6.3")
    client.stopped = False

    with pytest.raises(RuntimeError, match="未能在 15 秒内退出"):
        ensure_agent_backend(
            client,
            expected_version="0.6.4",
            command=["agent.exe", "--agent"],
            working_directory="D:/agent",
            popen=lambda *args, **kwargs: None,
        )


def test_windows_single_instance_mutex_blocks_duplicate_process_role():
    mutex_name = f"WechatPublisherAgent.Test.{uuid4().hex}"
    first = SingleInstance(mutex_name)
    second = SingleInstance(mutex_name)
    replacement = SingleInstance(mutex_name)
    try:
        assert first.acquire() is True
        assert second.acquire() is False
        first.close()
        assert replacement.acquire() is True
    finally:
        first.close()
        second.close()
        replacement.close()
