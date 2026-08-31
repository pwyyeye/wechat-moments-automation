import json
from datetime import datetime, timezone

import httpx
import pytest

from src.agent.config import AgentConfig, SourceConfig
from src.agent.credential_store import InMemoryCredentialStore
from src.agent.models import AgentSnapshot
from src.agent.sources.base import SourceError
from src.agent.sources.standard_http_v1 import StandardHttpSource


def build_source(handler):
    source = SourceConfig.model_validate(
        {
            "id": "auto-content",
            "name": "Auto Content",
            "baseUrl": "https://content.example.test/openapi/publisher-agent/v1",
            "accountKey": "wechat-main",
            "auth": {
                "type": "api_key_header",
                "headerName": "x-api-key",
                "credentialRef": "dpapi://auto-content",
            },
        }
    )
    config = AgentConfig(sources=[source])
    credentials = InMemoryCredentialStore()
    credentials.set("dpapi://auto-content", "test-api-key")
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return StandardHttpSource(
        source,
        config,
        credentials,
        client=client,
        instance_id="instance-test",
    )


def test_standard_http_routes_headers_and_empty_claim():
    requests = []

    def handler(request):
        requests.append(request)
        assert request.headers["x-api-key"] == "test-api-key"
        assert request.headers["x-agent-id"].startswith("agent-")
        assert request.headers["x-agent-instance-id"] == "instance-test"
        if request.url.path.endswith("/meta"):
            return httpx.Response(
                200,
                json={
                    "protocol": "wechat-moments-publisher-source",
                    "versions": ["1.0"],
                    "sourceName": "auto-content",
                    "serverTime": datetime.now(timezone.utc).isoformat(),
                    "requestId": "request-meta",
                },
            )
        if request.url.path.endswith("/agents/heartbeat"):
            assert request.headers["idempotency-key"]
            body = json.loads(request.content)
            assert body["wechat"]["nickname"] == "番石榴"
            assert body["wechat"]["wechatId"] == "higuava001"
            return httpx.Response(
                200,
                json={
                    "accepted": True,
                    "serverTime": datetime.now(timezone.utc).isoformat(),
                    "heartbeatIntervalSeconds": 15,
                    "leaseSeconds": 180,
                    "requestId": "request-heartbeat",
                },
            )
        if request.url.path.endswith("/tasks/claim"):
            return httpx.Response(204)
        raise AssertionError(request.url)

    source = build_source(handler)
    assert source.test_connection().versions == ["1.0"]
    source.heartbeat(
        AgentSnapshot(
            running=True,
            loggedIn=True,
            momentsWindowReady=True,
            wechatVersion="4.1.13.12",
            wechatNickname="番石榴",
            wechatId="higuava001",
            interactiveSession=True,
            desktopUnlocked=True,
        )
    )
    assert source.claim() is None
    assert len(requests) == 3


def test_problem_response_maps_auth_failure_without_exposing_secret():
    source = build_source(
        lambda request: httpx.Response(
            401,
            headers={"Content-Type": "application/problem+json"},
            json={
                "code": "SOURCE_AUTH_FAILED",
                "detail": "credential revoked",
                "retryable": False,
            },
        )
    )
    with pytest.raises(SourceError) as error:
        source.test_connection()
    assert error.value.code == "SOURCE_AUTH_FAILED"
    assert error.value.retryable is False
    assert "test-api-key" not in str(error.value)
