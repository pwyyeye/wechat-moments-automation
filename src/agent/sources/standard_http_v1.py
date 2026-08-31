from __future__ import annotations

import platform
from datetime import datetime, timezone
from uuid import uuid4

import httpx

from ..config import AgentConfig, SourceConfig
from ..credential_store import CredentialStore
from ..models import AgentCapabilities, AgentSnapshot, ClaimResponse, Lease, TaskEvent
from .base import SourceError, SourceMeta


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class StandardHttpSource:
    def __init__(
        self,
        source: SourceConfig,
        agent: AgentConfig,
        credential_store: CredentialStore,
        *,
        client: httpx.Client | None = None,
        instance_id: str | None = None,
    ):
        self.config = source
        self.agent_config = agent
        self.credential_store = credential_store
        self.source_id = source.id
        self.instance_id = instance_id or f"instance-{uuid4().hex[:12]}"
        self.base_url = str(source.base_url).rstrip("/")
        self.client = client or httpx.Client(
            timeout=httpx.Timeout(15.0, connect=5.0),
            follow_redirects=False,
        )

    def _headers(self, *, request_id: str, idempotency_key: str | None = None):
        try:
            secret = self.credential_store.get(self.config.auth.credential_ref)
        except (FileNotFoundError, KeyError, ValueError) as error:
            raise SourceError(
                "SOURCE_CREDENTIAL_MISSING",
                "Data source credential has not been configured.",
                retryable=False,
            ) from error
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-Agent-Id": self.agent_config.agent.id,
            "X-Agent-Instance-Id": self.instance_id,
            "X-Request-Id": request_id,
        }
        if self.config.auth.type == "bearer":
            headers["Authorization"] = f"Bearer {secret}"
        else:
            headers[self.config.auth.header_name or "X-Api-Key"] = secret
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        return headers

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body=None,
        idempotency_key: str | None = None,
        allow_no_content: bool = False,
    ) -> httpx.Response:
        request_id = str(uuid4())
        try:
            response = self.client.request(
                method,
                f"{self.base_url}{path}",
                headers=self._headers(
                    request_id=request_id,
                    idempotency_key=idempotency_key,
                ),
                json=json_body,
            )
        except httpx.HTTPError as error:
            raise SourceError(
                "SOURCE_UNREACHABLE",
                f"Data source request failed: {error.__class__.__name__}",
                retryable=True,
            ) from error
        if allow_no_content and response.status_code == 204:
            return response
        if response.is_success:
            return response

        problem = {}
        try:
            problem = response.json()
        except ValueError:
            pass
        code = problem.get("code") or (
            "SOURCE_AUTH_FAILED"
            if response.status_code in {401, 403}
            else "SOURCE_REQUEST_FAILED"
        )
        retry_after = response.headers.get("Retry-After")
        raise SourceError(
            code,
            problem.get("detail") or f"Data source returned HTTP {response.status_code}",
            retryable=bool(
                problem.get(
                    "retryable",
                    response.status_code in {429, 500, 502, 503, 504},
                )
            ),
            status_code=response.status_code,
            retry_after_seconds=int(retry_after)
            if retry_after and retry_after.isdigit()
            else None,
        )

    def test_connection(self) -> SourceMeta:
        response = self._request("GET", "/meta")
        payload = response.json()
        if (
            payload.get("protocol") != "wechat-moments-publisher-source"
            or "1.0" not in payload.get("versions", [])
        ):
            raise SourceError(
                "SOURCE_PROTOCOL_INCOMPATIBLE",
                "Data source does not advertise protocol version 1.0",
                retryable=False,
            )
        return SourceMeta(
            protocol=payload["protocol"],
            versions=list(payload["versions"]),
            source_name=payload.get("sourceName", self.config.name),
            server_time=payload["serverTime"],
        )

    def heartbeat(self, snapshot: AgentSnapshot) -> None:
        payload = {
            "protocolVersion": "1.0",
            "agent": {
                "agentId": self.agent_config.agent.id,
                "instanceId": self.instance_id,
                "displayName": self.agent_config.agent.display_name,
                "agentVersion": "0.4.0",
                "os": f"{platform.system()} {platform.release()}",
                "interactiveSession": snapshot.interactive_session,
                "desktopUnlocked": snapshot.desktop_unlocked,
            },
            "wechat": {
                "version": snapshot.wechat_version,
                "running": snapshot.running,
                "loggedIn": snapshot.logged_in,
                "momentsWindowReady": snapshot.moments_window_ready,
                "accountKeys": [self.config.account_key],
                "nickname": snapshot.wechat_nickname,
                "wechatId": snapshot.wechat_id,
            },
            "capabilities": AgentCapabilities().model_dump(by_alias=True),
            "occurredAt": utc_now_iso(),
        }
        self._request(
            "POST",
            "/agents/heartbeat",
            json_body=payload,
            idempotency_key=str(uuid4()),
        )

    def claim(self) -> ClaimResponse | None:
        payload = {
            "protocolVersion": "1.0",
            "agentId": self.agent_config.agent.id,
            "instanceId": self.instance_id,
            "accountKeys": [self.config.account_key],
            "maxTasks": 1,
            "requestedLeaseSeconds": self.agent_config.runtime.default_lease_seconds,
        }
        response = self._request(
            "POST",
            "/tasks/claim",
            json_body=payload,
            idempotency_key=str(uuid4()),
            allow_no_content=True,
        )
        if response.status_code == 204:
            return None
        return ClaimResponse.model_validate(response.json())

    def renew_lease(self, task_id: str, lease: Lease) -> Lease:
        payload = {
            "protocolVersion": "1.0",
            "agentId": self.agent_config.agent.id,
            "instanceId": self.instance_id,
            "leaseToken": lease.token,
            "requestedLeaseSeconds": self.agent_config.runtime.default_lease_seconds,
            "occurredAt": utc_now_iso(),
        }
        response = self._request(
            "POST",
            f"/tasks/{task_id}/lease/renew",
            json_body=payload,
            idempotency_key=str(uuid4()),
        )
        return Lease.model_validate(response.json())

    def send_event(self, event: TaskEvent) -> None:
        self._request(
            "POST",
            f"/tasks/{event.task_id}/events",
            json_body=event.model_dump(by_alias=True, mode="json", exclude_none=True),
            idempotency_key=event.event_id,
        )

    def close(self) -> None:
        self.client.close()
