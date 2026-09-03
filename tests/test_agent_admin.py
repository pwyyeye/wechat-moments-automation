import json
from datetime import timedelta

from fastapi.testclient import TestClient
from PIL import Image

from src.agent.app import PublisherAgentApp
from src.agent.config import DEFAULT_SOURCE_ID, AgentConfig, default_source_config, save_config
from src.agent.credential_store import IdentityPayloadProtector, InMemoryCredentialStore
from src.agent.local_schedule import utc_now
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


def test_loopback_admin_creates_updates_and_cancels_local_schedule(tmp_path):
    class IdentifiedExecutor(FakeExecutor):
        def snapshot(self):
            return AgentSnapshot(
                running=True,
                loggedIn=True,
                momentsWindowReady=True,
                wechatVersion="4.1.13.12",
                wechatNickname="番石榴",
                wechatId="higuava001",
                interactiveSession=True,
                desktopUnlocked=True,
            )

    image_path = tmp_path / "schedule.png"
    Image.new("RGB", (16, 16), color="green").save(image_path, format="PNG")
    app = PublisherAgentApp(
        tmp_path / "config.yaml",
        executor=IdentifiedExecutor(),
        credential_store=InMemoryCredentialStore(),
        payload_protector=IdentityPayloadProtector(),
        source_factory=FakeSource,
    )
    client = TestClient(app.admin_app)
    headers = {"X-Local-Agent-Action": "confirmed"}
    created = client.post(
        "/api/local-schedules",
        headers=headers,
        json={
            "text": "定时文案",
            "imagePaths": [str(image_path)],
            "scheduledAt": (utc_now() + timedelta(minutes=10)).isoformat(),
        },
    )

    assert created.status_code == 201
    body = created.json()
    assert body["kind"] == "local_schedule"
    assert body["target_nickname"] == "番石榴"
    assert body["media_paths"][0] != str(image_path)

    updated = client.put(
        f"/api/local-schedules/{body['task_id']}",
        headers=headers,
        json={
            "text": "更新文案",
            "scheduledAt": (utc_now() + timedelta(minutes=20)).isoformat(),
        },
    )
    assert updated.status_code == 200
    assert updated.json()["text"] == "更新文案"
    tasks = client.get("/api/tasks").json()
    assert any(item["task_id"] == body["task_id"] for item in tasks)

    cancelled = client.post(
        f"/api/local-schedules/{body['task_id']}/cancel",
        headers=headers,
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["state"] == "cancelled"
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

    app.run_forever()

    assert captured["config"].log_config is None
    assert captured["config"].access_log is False
    assert captured["run"] is True


def test_background_start_never_creates_wechat_identity_thread(tmp_path, monkeypatch):
    app = PublisherAgentApp(
        tmp_path / "config.yaml",
        executor=FakeExecutor(),
        credential_store=InMemoryCredentialStore(),
        payload_protector=IdentityPayloadProtector(),
        source_factory=FakeSource,
    )
    created = []

    class FakeThread:
        def __init__(self, *, target, name, daemon):
            self.target = target
            self.name = name
            self.daemon = daemon
            created.append(self)

        def start(self):
            pass

        def is_alive(self):
            return False

        def join(self, timeout=None):
            pass

    monkeypatch.setattr("src.agent.app.threading.Thread", FakeThread)
    monkeypatch.setattr(app.connector_registry, "start", lambda: None)

    app.start_background()

    assert [thread.name for thread in created] == [
        "publisher-worker",
        "publisher-heartbeat",
    ]
    assert not hasattr(app, "_identity_thread")
    app.stop()


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


def test_admin_exposes_filtered_local_logs(tmp_path):
    app = PublisherAgentApp(
        tmp_path / "config.yaml",
        executor=FakeExecutor(),
        credential_store=InMemoryCredentialStore(),
        payload_protector=IdentityPayloadProtector(),
        source_factory=FakeSource,
    )
    log_directory = tmp_path / "logs"
    log_directory.mkdir(exist_ok=True)
    (log_directory / "agent.log").write_text(
        "2026-08-31 10:00:00,000 INFO source healthy\n"
        "2026-08-31 10:00:01,000 ERROR source SOURCE_AUTH_FAILED\n",
        encoding="utf-8",
    )

    response = TestClient(app.admin_app).get(
        "/api/logs",
        params={"level": "ERROR", "query": "AUTH"},
    )

    assert response.status_code == 200
    body = response.json()
    assert len(body["entries"]) == 1
    assert body["entries"][0]["message"] == "SOURCE_AUTH_FAILED"
    assert body["logDirectory"] == str(log_directory)
    app.stop()


def test_shutdown_is_allowed_while_a_local_desktop_action_holds_the_worker_lock(
    tmp_path, monkeypatch
):
    app = PublisherAgentApp(
        tmp_path / "config.yaml",
        executor=FakeExecutor(),
        credential_store=InMemoryCredentialStore(),
        payload_protector=IdentityPayloadProtector(),
        source_factory=FakeSource,
    )
    client = TestClient(app.admin_app)
    requested = []
    monkeypatch.setattr(app, "request_shutdown", lambda: requested.append(True))

    with app.worker.exclusive_desktop_action():
        response = client.post(
            "/api/shutdown",
            headers={"X-Local-Agent-Action": "confirmed"},
        )

    assert response.status_code == 202
    import time

    deadline = time.monotonic() + 1
    while not requested and time.monotonic() < deadline:
        time.sleep(0.02)
    assert requested == [True]
    app.stop()


def test_shutdown_watchdog_forces_exit_when_server_cannot_drain(tmp_path):
    app = PublisherAgentApp(
        tmp_path / "config.yaml",
        executor=FakeExecutor(),
        credential_store=InMemoryCredentialStore(),
        payload_protector=IdentityPayloadProtector(),
        source_factory=FakeSource,
    )

    class StuckServer:
        should_exit = False

    forced = []
    app._server = StuckServer()
    app._shutdown_grace_seconds = 0.02
    app._force_exit = lambda code: forced.append(code)

    app.request_shutdown()

    import time

    deadline = time.monotonic() + 1
    while not forced and time.monotonic() < deadline:
        time.sleep(0.02)
    assert app._server.should_exit is True
    assert forced == [0]
    app._server_stopped.set()
    app._server = None
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
    assert "设备首次连接会自动登记" in html
    assert "测试并登记" in html
    assert "f.authType.value=s.authType" in html
    assert "timeoutMs=10000" in html
    assert "最多等待 20 秒" in html
    assert "shutdownAgent(event)" in html
    assert "进程也会在 8 秒内结束" in html
    app.stop()


def test_default_source_without_bootstrap_is_marked_unconfigured(tmp_path):
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

    source = app.source_manager.status()[0]
    assert source["id"] == DEFAULT_SOURCE_ID
    assert source["healthState"] == "unconfigured"
    assert source["hasCredential"] is False
    app.stop()


def test_bootstrap_imports_source_credential_and_removes_plaintext(tmp_path):
    config_path = tmp_path / "config.yaml"
    save_config(AgentConfig(sources=[default_source_config()]), config_path)
    bootstrap_path = tmp_path / "bootstrap.json"
    bootstrap_path.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "sources": [
                    {
                        "id": DEFAULT_SOURCE_ID,
                        "name": "智能内容运营平台",
                        "baseUrl": "https://content.example.test/openapi/publisher-agent/v1",
                        "accountKey": "wechat-main",
                        "auth": {"type": "bearer", "credential": "real-api-key"},
                        "allowedHosts": ["content.example.test"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    credentials = InMemoryCredentialStore()

    app = PublisherAgentApp(
        config_path,
        executor=FakeExecutor(),
        credential_store=credentials,
        payload_protector=IdentityPayloadProtector(),
        source_factory=FakeSource,
    )

    assert not bootstrap_path.exists()
    assert credentials.get(f"dpapi://{DEFAULT_SOURCE_ID}") == "real-api-key"
    source = app.source_manager.status()[0]
    assert source["baseUrl"].startswith("https://content.example.test/")
    assert source["healthState"] == "unknown"
    assert source["hasCredential"] is True
    app.stop()


def test_bootstrap_base_url_change_requires_new_device_enrollment(tmp_path):
    config_path = tmp_path / "config.yaml"
    save_config(AgentConfig(sources=[default_source_config()]), config_path)
    bootstrap_path = tmp_path / "bootstrap.json"
    bootstrap_path.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "sources": [
                    {
                        "id": DEFAULT_SOURCE_ID,
                        "name": "新内容中心",
                        "baseUrl": "https://new.example.test/openapi/publisher-agent/v2",
                        "auth": {"type": "bearer", "credential": "new-api-key"},
                        "allowedHosts": ["new.example.test"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    credentials = InMemoryCredentialStore()
    credentials.set(f"dpapi://{DEFAULT_SOURCE_ID}.device", "old-device-key")
    credentials.set(f"dpapi://{DEFAULT_SOURCE_ID}.device-enrolled", "old-device-id")

    app = PublisherAgentApp(
        config_path,
        executor=FakeExecutor(),
        credential_store=credentials,
        payload_protector=IdentityPayloadProtector(),
        source_factory=FakeSource,
    )

    assert credentials.get(f"dpapi://{DEFAULT_SOURCE_ID}") == "new-api-key"
    assert f"dpapi://{DEFAULT_SOURCE_ID}.device" not in credentials.values
    assert f"dpapi://{DEFAULT_SOURCE_ID}.device-enrolled" not in credentials.values
    assert app.source_manager.status()[0]["deviceEnrolled"] is False
    app.stop()


def test_invalid_bootstrap_does_not_take_down_local_admin(tmp_path):
    config_path = tmp_path / "config.yaml"
    save_config(AgentConfig(sources=[default_source_config()]), config_path)
    bootstrap_path = tmp_path / "bootstrap.json"
    bootstrap_path.write_text("not-json", encoding="utf-8")

    app = PublisherAgentApp(
        config_path,
        executor=FakeExecutor(),
        credential_store=InMemoryCredentialStore(),
        payload_protector=IdentityPayloadProtector(),
        source_factory=FakeSource,
    )

    assert TestClient(app.admin_app).get("/api/health").json()["ok"] is True
    assert bootstrap_path.exists()
    assert app.source_manager.status()[0]["healthState"] == "unconfigured"
    app.stop()


def test_admin_manages_multiple_wechat_sync_profiles_and_protects_token(tmp_path):
    credentials = InMemoryCredentialStore()
    app = PublisherAgentApp(
        tmp_path / "config.yaml",
        executor=FakeExecutor(),
        credential_store=credentials,
        payload_protector=IdentityPayloadProtector(),
        source_factory=FakeSource,
    )
    client = TestClient(app.admin_app)
    profile = {
        "id": "profile-b",
        "name": "Chrome B",
        "enabled": True,
        "bridgePort": 9537,
        "platforms": ["zhihu", "juejin"],
        "chromeExecutable": None,
        "userDataDir": "D:/browser-data/profile-b",
        "profileDirectory": "Default",
        "extensionPath": None,
        "autoLaunch": False,
    }

    assert client.post("/api/connectors/wechatsync", json=profile).status_code == 201
    profiles = client.get("/api/connectors/wechatsync").json()
    assert {item["id"] for item in profiles} == {"chrome-default", "profile-b"}
    assert client.get("/api/connectors/wechatsync/profile-b/token").status_code == 403
    token = client.get(
        "/api/connectors/wechatsync/profile-b/token",
        headers={"X-Local-Agent-Action": "confirmed"},
    ).json()["token"]
    assert len(token) >= 32
    assert token not in (tmp_path / "config.yaml").read_text(encoding="utf-8")

    assert client.delete("/api/connectors/wechatsync/profile-b").status_code == 200
    app.stop()
