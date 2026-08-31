from pathlib import Path

import httpx
import pytest

from src.agent.admin.gui import LocalAgentClient, LocalAgentError


def test_native_gui_client_reads_loopback_status_without_browser(monkeypatch):
    captured = {}

    def request(method, url, **kwargs):
        captured.update(method=method, url=url, kwargs=kwargs)
        return httpx.Response(
            200,
            json={"agent": {"version": "0.4.1"}, "sources": []},
            request=httpx.Request(method, url),
        )

    monkeypatch.setattr("src.agent.admin.gui.httpx.request", request)
    client = LocalAgentClient("http://127.0.0.1:17821")

    status = client.status()

    assert status["agent"]["version"] == "0.4.1"
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


def test_operator_entry_points_use_native_control_panel():
    root = Path(__file__).resolve().parents[1]
    installer = (root / "packaging" / "installer.iss").read_text(encoding="utf-8")
    startup = (root / "scripts" / "install-startup.ps1").read_text(encoding="utf-8")
    app = (root / "src" / "agent" / "app.py").read_text(encoding="utf-8")

    assert 'Parameters: "--agent-ui"' in installer
    assert '-Argument "--agent"' in startup
    assert "webbrowser" not in app
