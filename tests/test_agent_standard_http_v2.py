import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from src.agent.config import AgentConfig, SourceConfig
from src.agent.credential_store import InMemoryCredentialStore
from src.agent.models import AgentSnapshot
from src.agent.models_v2 import PublisherV2Account, PublisherV2Executor
from src.agent.sources.base import SourceError
from src.agent.sources.standard_http_v2 import StandardHttpV2Source


def build_source(handler):
    source = SourceConfig.model_validate(
        {
            "id": "auto-content-v2",
            "name": "Auto Content V2",
            "type": "standard-http-v2",
            "baseUrl": "https://content.example.test/openapi/publisher-agent/v2",
            "accountKey": "wechat-main",
            "auth": {
                "type": "api_key_header",
                "headerName": "x-api-key",
                "credentialRef": "dpapi://auto-content-v2",
            },
        }
    )
    config = AgentConfig(sources=[source], wechatSyncProfiles=[])
    credentials = InMemoryCredentialStore()
    credentials.set("dpapi://auto-content-v2", "v2-secret")
    return StandardHttpV2Source(
        source,
        config,
        credentials,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        instance_id="instance-v2",
    )


def executor_and_account():
    executor = PublisherV2Executor.model_validate(
        {
            "executorInstanceId": "wechatsync:profile-a",
            "providerKey": "wechatsync",
            "executionMode": "browser_bridge",
            "profileId": "profile-a",
            "status": "ready",
            "capabilities": [
                {
                    "platform": "zhihu",
                    "operations": ["create_draft"],
                    "contentTypes": ["article"],
                }
            ],
        }
    )
    account = PublisherV2Account.model_validate(
        {
            "executorInstanceId": executor.executor_instance_id,
            "platform": "zhihu",
            "accountStableId": "zhihu-user-1",
            "nickname": "知乎账号",
            "profileId": "profile-a",
            "authState": "authenticated",
            "status": "ready",
        }
    )
    return executor, account


def claim_response(*, account_stable_id="zhihu-user-1"):
    now = datetime.now(timezone.utc)
    return {
        "lease": {
            "token": "lease-token-v2-0001",
            "expiresAt": (now + timedelta(minutes=3)).isoformat(),
            "renewAfterSeconds": 30,
        },
        "task": {
            "specVersion": "content-publisher/task-v2",
            "taskId": "task-v2-1",
            "idempotencyKey": "idempotency-v2-1",
            "revision": 1,
            "createdAt": now.isoformat(),
            "priority": 50,
            "route": {
                "providerKey": "wechatsync",
                "operation": "create_draft",
                "platform": "zhihu",
                "accountStableId": account_stable_id,
                "profileId": "profile-a",
            },
            "content": {"title": "文章", "markdown": "正文", "media": []},
            "options": {"draftOnly": True},
            "schedule": {
                "notBefore": now.isoformat(),
                "expiresAt": None,
                "timezone": "Asia/Shanghai",
                "misfirePolicy": "manual",
            },
            "policy": {"maxPreActionAttempts": 2, "completionStrategy": "sync"},
            "extensions": {},
        },
        "attempt": 1,
        "serverTime": now.isoformat(),
        "requestId": "request-v2",
    }


def snapshot():
    return AgentSnapshot(
        running=True,
        loggedIn=True,
        momentsWindowReady=True,
        wechatVersion="4.1.13.12",
        wechatNickname="微信账号",
        wechatId="wx-1",
        interactiveSession=True,
        desktopUnlocked=True,
    )


def test_v2_source_heartbeat_and_claim_include_exact_executor_route():
    requests = []
    enrolled_credentials = []
    executor, account = executor_and_account()

    def handler(request):
        requests.append(request)
        assert request.headers["x-api-key"] == "v2-secret"
        if request.url.path.endswith("/agents/enroll"):
            proposed = request.headers["x-agent-credential"]
            enrolled_credentials.append(proposed)
            body = json.loads(request.content)
            assert body["protocolVersion"] == "2.0"
            return httpx.Response(
                201,
                json={
                    "approved": True,
                    "deviceId": "device-v2",
                    "credential": proposed,
                    "credentialVersion": 1,
                    "issuedAt": datetime.now(timezone.utc).isoformat(),
                    "requestId": "request-enroll-v2",
                },
            )
        if request.url.path.endswith("/meta"):
            return httpx.Response(
                200,
                json={
                    "protocol": "content-publisher-source",
                    "versions": ["2.0"],
                    "sourceName": "v2",
                    "serverTime": datetime.now(timezone.utc).isoformat(),
                },
            )
        if request.url.path.endswith("/agents/heartbeat"):
            assert request.headers["x-agent-credential"] == (
                enrolled_credentials[0]
            )
            body = json.loads(request.content)
            assert body["executors"][0]["executorInstanceId"] == executor.executor_instance_id
            assert body["accounts"][0]["accountStableId"] == account.account_stable_id
            return httpx.Response(200, json={"accepted": True})
        if request.url.path.endswith("/tasks/claim"):
            body = json.loads(request.content)
            assert body["executorInstanceId"] == executor.executor_instance_id
            assert body["currentAccounts"][0]["profileId"] == "profile-a"
            return httpx.Response(200, json=claim_response())
        raise AssertionError(request.url)

    source = build_source(handler)
    assert source.test_connection().versions == ["2.0"]
    source.heartbeat(snapshot(), [executor], [account])
    claim = source.claim(executor, [account])

    assert claim.task.route.executor_instance_id == executor.executor_instance_id
    assert len(requests) == 4


def test_v2_source_rejects_claim_if_account_changed_after_request():
    executor, account = executor_and_account()

    def handler(request):
        if request.url.path.endswith("/agents/enroll"):
            proposed = request.headers["x-agent-credential"]
            return httpx.Response(
                201,
                json={
                    "approved": True,
                    "deviceId": "device-v2",
                    "credential": proposed,
                },
            )
        return httpx.Response(
            200,
            json=claim_response(account_stable_id="other"),
        )

    source = build_source(handler)

    with pytest.raises(SourceError, match="current authenticated account") as error:
        source.claim(executor, [account])

    assert error.value.code == "ACCOUNT_MISMATCH"
