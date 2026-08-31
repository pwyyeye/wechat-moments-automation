from fastapi.testclient import TestClient

from src.agent.app import PublisherAgentApp
from src.agent.config import DEFAULT_SOURCE_ID, AgentConfig, default_source_config, save_config
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

    class FakeServer:
        def __init__(self, config):
            captured["config"] = config
            self.should_exit = False

        def run(self):
            captured["run"] = True

    monkeypatch.setattr(app, "start_background", lambda: None)
    monkeypatch.setattr(app, "stop", lambda: None)
    monkeypatch.setattr("src.agent.app.uvicorn.Server", FakeServer)

    app.run_forever(open_browser=False)

    assert captured["config"].log_config is None
    assert captured["config"].access_log is False
    assert captured["run"] is True


def test_admin_exposes_safe_shutdown_and_manual_identity_actions(tmp_path, monkeypatch):
    app = PublisherAgentApp(
        tmp_path / "config.yaml",
        executor=FakeExecutor(),
        credential_store=InMemoryCredentialStore(),
        payload_protector=IdentityPayloadProtector(),
        source_factory=FakeSource,
    )
    client = TestClient(app.admin_app)
    requested = []
    monkeypatch.setattr(
        app,
        "recognize_wechat_identity",
        lambda: {
            "recognized": True,
            "nickname": "番石榴",
            "wechatId": "higuava001",
            "diagnostic": {"state": "identified"},
        },
    )
    monkeypatch.setattr(app, "request_shutdown", lambda: requested.append(True))

    assert client.post("/api/wechat/identify").status_code == 403
    identified = client.post(
        "/api/wechat/identify",
        headers={"X-Local-Agent-Action": "confirmed"},
    )
    assert identified.status_code == 200
    assert identified.json()["nickname"] == "番石榴"

    assert client.post("/api/shutdown").status_code == 403
    shutdown = client.post(
        "/api/shutdown",
        headers={"X-Local-Agent-Action": "confirmed"},
    )
    assert shutdown.status_code == 202
    import time

    deadline = time.monotonic() + 1
    while not requested and time.monotonic() < deadline:
        time.sleep(0.02)
    assert requested == [True]
    app.stop()


def test_admin_html_handles_non_json_errors_without_masking_them(tmp_path):
    app = PublisherAgentApp(
        tmp_path / "config.yaml",
        executor=FakeExecutor(),
        credential_store=InMemoryCredentialStore(),
        payload_protector=IdentityPayloadProtector(),
        source_factory=FakeSource,
    )
    html = TestClient(app.admin_app).get("/").text

    assert "try{data=JSON.parse(raw)}catch{data=raw}" in html
    assert "安全退出 Agent" in html
    assert "重新识别微信" in html
    assert "首次启动会自动注册默认内容中心" in html
    assert "编辑 URL" in html
    assert "f.authType.value=s.authType" in html
    app.stop()


def test_default_source_gets_an_opaque_local_credential(tmp_path):
    config_path = tmp_path / "config.yaml"
    save_config(AgentConfig(sources=[default_source_config()]), config_path)
    credentials = InMemoryCredentialStore()

    app = PublisherAgentApp(
        config_path,
        executor=FakeExecutor(),
        credential_store=credentials,
        payload_protector=IdentityPayloadProtector(),
        source_factory=FakeSource,
    )

    secret = credentials.get(f"dpapi://{DEFAULT_SOURCE_ID}")
    assert len(secret) >= 32
    app.stop()
