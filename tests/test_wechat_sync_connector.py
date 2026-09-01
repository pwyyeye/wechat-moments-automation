import json
import socket
import threading
import time
from datetime import datetime, timezone

import pytest
from websockets.sync.client import connect

from src.agent.config import WechatSyncProfileConfig
from src.agent.connectors.wechatsync import ConnectorError, WechatSyncConnector
from src.agent.connectors.wechatsync_bridge import WechatSyncBridge
from src.agent.credential_store import InMemoryCredentialStore
from src.agent.models_v2 import PublisherV2Task


class FakeBridge:
    connected = True
    last_request = None

    def request(self, method, params, timeout=60):
        self.last_request = (method, params, timeout)
        if method == "checkAuth":
            return {
                "isAuthenticated": True,
                "userId": f"{params['platform']}-stable-user",
                "username": f"{params['platform']}昵称",
            }
        if method == "syncArticle":
            platform = params["platforms"][0]
            return {
                "syncId": "sync-1",
                "results": [
                    {
                        "platform": platform,
                        "success": True,
                        "postId": "draft-1",
                        "postUrl": "https://example.test/drafts/1",
                        "draftOnly": True,
                    }
                ],
            }
        raise AssertionError(method)

    def start(self):
        pass

    def stop(self):
        pass


def profile(port=9527):
    return WechatSyncProfileConfig.model_validate(
        {
            "id": "profile-a",
            "name": "Chrome A",
            "bridgePort": port,
            "tokenRef": "dpapi://profile-a",
            "platforms": ["zhihu", "juejin"],
        }
    )


def task(account="zhihu-stable-user"):
    now = datetime.now(timezone.utc).isoformat()
    return PublisherV2Task.model_validate(
        {
            "specVersion": "content-publisher/task-v2",
            "taskId": "task-1",
            "idempotencyKey": "task-key-1",
            "revision": 1,
            "createdAt": now,
            "priority": 50,
            "route": {
                "providerKey": "wechatsync",
                "operation": "create_draft",
                "platform": "zhihu",
                "accountStableId": account,
                "profileId": "profile-a",
                "executorInstanceId": "wechatsync:profile-a",
            },
            "content": {"title": "标题", "markdown": "正文", "media": []},
            "options": {"draftOnly": True},
            "schedule": {
                "notBefore": now,
                "expiresAt": None,
                "timezone": "Asia/Shanghai",
                "misfirePolicy": "manual",
            },
            "policy": {"maxPreActionAttempts": 2, "completionStrategy": "sync"},
            "extensions": {},
        }
    )


def connector():
    credentials = InMemoryCredentialStore()
    credentials.set("dpapi://profile-a", "bridge-token")
    result = WechatSyncConnector(profile(), credentials)
    result.bridge = FakeBridge()
    return result


def test_connector_reports_stable_accounts_and_returns_draft_result():
    target = connector()
    accounts = target.refresh_accounts(force=True)

    assert {(item.platform, item.account_stable_id) for item in accounts} == {
        ("zhihu", "zhihu-stable-user"),
        ("juejin", "juejin-stable-user"),
    }
    result = target.create_draft(task(), [])
    assert result == {
        "syncId": "sync-1",
        "postId": "draft-1",
        "postUrl": "https://example.test/drafts/1",
        "draftOnly": True,
    }


def test_connector_refuses_task_for_another_authenticated_account():
    target = connector()
    target.refresh_accounts()

    with pytest.raises(ConnectorError) as error:
        target.create_draft(task(account="another-user"), [])

    assert error.value.code == "ACCOUNT_MISMATCH"
    assert target.bridge.last_request[0] == "checkAuth"


def test_bridge_uses_wechatsync_wire_format_on_loopback():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    bridge = WechatSyncBridge("127.0.0.1", port, "local-token")
    bridge.start()

    def extension_client():
        with connect(f"ws://127.0.0.1:{port}") as websocket:
            message = json.loads(websocket.recv())
            assert message["token"] == "local-token"
            assert message["method"] == "checkAuth"
            websocket.send(
                json.dumps(
                    {
                        "id": message["id"],
                        "result": {"isAuthenticated": True, "userId": "user-1"},
                    }
                )
            )

    thread = threading.Thread(target=extension_client, daemon=True)
    thread.start()
    deadline = time.monotonic() + 2
    while not bridge.connected and time.monotonic() < deadline:
        time.sleep(0.01)

    assert bridge.request("checkAuth", {"platform": "zhihu"}, timeout=2)["userId"] == "user-1"
    thread.join(timeout=2)
    bridge.stop()
