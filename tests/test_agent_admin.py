from fastapi.testclient import TestClient

from src.agent.app import PublisherAgentApp
from src.agent.credential_store import IdentityPayloadProtector, InMemoryCredentialStore
from src.agent.models import AgentSnapshot
from src.agent.sources.base import SourceMeta


class FakeExecutor:
    def snapshot(self):
        return AgentSnapshot(
            running=True,
            loggedIn=True,
            momentsWindowReady=True,
            wechatVersion="4.1.13.12",
            interactiveSession=True,
            desktopUnlocked=True,
        )

    def preflight(self, task=None):
        return self.snapshot()

    def publish(self, *args, **kwargs):
        raise AssertionError("admin test must not publish")

    def close(self):
        pass


class FakeSource:
    def __init__(self, config, agent, credential_store, instance_id=None):
        self.config = config
        self.source_id = config.id
        self.instance_id = instance_id

    def test_connection(self):
        return SourceMeta(
            "wechat-moments-publisher-source",
            ["1.0"],
            self.config.name,
            "2026-08-30T00:00:00Z",
        )

    def heartbeat(self, snapshot):
        pass

    def claim(self):
        return None

    def renew_lease(self, task_id, lease):
        return lease

    def send_event(self, event):
        pass

    def close(self):
        pass


def source_body(source_id):
    return {
        "id": source_id,
        "name": f"Source {source_id}",
        "baseUrl": "https://content.example.test/openapi/publisher-agent/v1",
        "enabled": True,
        "weight": 1,
        "accountKey": "wechat-main",
        "auth": {
            "type": "api_key_header",
            "headerName": "x-api-key",
            "credentialRef": f"dpapi://{source_id}",
        },
        "mediaSecurity": {
            "allowedHosts": ["files.example.test"],
            "allowPrivateNetwork": False,
        },
        "credential": f"secret-for-{source_id}",
    }


def test_loopback_admin_configures_multiple_sources_without_plaintext_secrets(tmp_path):
    credentials = InMemoryCredentialStore()
    app = PublisherAgentApp(
        tmp_path / "config.yaml",
        executor=FakeExecutor(),
        credential_store=credentials,
        payload_protector=IdentityPayloadProtector(),
        source_factory=FakeSource,
    )
    client = TestClient(app.admin_app)

    assert client.post("/api/sources", json=source_body("source-a")).status_code == 201
    assert client.post("/api/sources", json=source_body("source-b")).status_code == 201
    sources = client.get("/api/sources").json()
    assert {item["id"] for item in sources} == {"source-a", "source-b"}
    assert client.post("/api/sources/source-a/test").json()["ok"] is True
    assert client.post("/api/preflight").json()["momentsWindowReady"] is True

    config_text = (tmp_path / "config.yaml").read_text(encoding="utf-8")
    assert "secret-for" not in config_text
    assert credentials.get("dpapi://source-a") == "secret-for-source-a"
    assert client.get("/api/health").json()["binding"] == "loopback-only"
    app.stop()


def test_run_forever_does_not_require_console_logging(tmp_path, monkeypatch):
    app = PublisherAgentApp(
        tmp_path / "config.yaml",
        executor=FakeExecutor(),
        credential_store=InMemoryCredentialStore(),
        payload_protector=IdentityPayloadProtector(),
        source_factory=FakeSource,
    )
    captured = {}
    monkeypatch.setattr(app, "start_background", lambda: None)
    monkeypatch.setattr(app, "stop", lambda: None)
    monkeypatch.setattr(
        "src.agent.app.uvicorn.run",
        lambda application, **kwargs: captured.update(kwargs),
    )

    app.run_forever(open_browser=False)

    assert captured["log_config"] is None
    assert captured["access_log"] is False
