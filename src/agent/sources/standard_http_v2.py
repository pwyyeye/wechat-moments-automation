from __future__ import annotations

import platform
from uuid import uuid4

from ..models import AgentSnapshot, Lease
from ..models_v2 import (
    PublisherV2Account,
    PublisherV2ClaimResponse,
    PublisherV2Executor,
    PublisherV2TaskEvent,
)
from ..version import AGENT_VERSION
from .base import SourceError, SourceMeta
from .standard_http_v1 import StandardHttpSource, utc_now_iso


class StandardHttpV2Source(StandardHttpSource):
    protocol_version = "2.0"

    def test_connection(self) -> SourceMeta:
        response = self._request(
            "GET", "/meta", require_device_credential=False
        )
        payload = response.json()
        if (
            payload.get("protocol") != "content-publisher-source"
            or "2.0" not in payload.get("versions", [])
        ):
            raise SourceError(
                "SOURCE_PROTOCOL_INCOMPATIBLE",
                "Data source does not advertise protocol version 2.0",
                retryable=False,
            )
        meta = SourceMeta(
            protocol=payload["protocol"],
            versions=list(payload["versions"]),
            source_name=payload.get("sourceName", self.config.name),
            server_time=payload["serverTime"],
        )
        self._ensure_enrolled()
        return meta

    def heartbeat(
        self,
        snapshot: AgentSnapshot,
        executors: list[PublisherV2Executor],
        accounts: list[PublisherV2Account],
    ) -> None:
        payload = {
            "protocolVersion": "2.0",
            "agent": {
                "agentId": self.agent_config.agent.id,
                "instanceId": self.instance_id,
                "displayName": self.agent_config.agent.display_name,
                "agentVersion": AGENT_VERSION,
                "os": f"{platform.system()} {platform.release()}",
                "interactiveSession": snapshot.interactive_session,
                "desktopUnlocked": snapshot.desktop_unlocked,
            },
            "executors": [
                item.model_dump(by_alias=True, mode="json", exclude_none=True)
                for item in executors
            ],
            "accounts": [
                item.model_dump(by_alias=True, mode="json", exclude_none=True)
                for item in accounts
            ],
            "occurredAt": utc_now_iso(),
        }
        self._request(
            "POST",
            "/agents/heartbeat",
            json_body=payload,
            idempotency_key=str(uuid4()),
        )

    def claim(
        self,
        executor: PublisherV2Executor,
        accounts: list[PublisherV2Account],
    ) -> PublisherV2ClaimResponse | None:
        current_accounts = [
            {
                "platform": account.platform,
                "accountStableId": account.account_stable_id,
                "profileId": account.profile_id,
            }
            for account in accounts
            if account.executor_instance_id == executor.executor_instance_id
            and account.auth_state == "authenticated"
            and account.status == "ready"
        ]
        payload = {
            "protocolVersion": "2.0",
            "agentId": self.agent_config.agent.id,
            "instanceId": self.instance_id,
            "executorInstanceId": executor.executor_instance_id,
            "currentAccounts": current_accounts,
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
        claim = PublisherV2ClaimResponse.model_validate(response.json())
        self._assert_exact_route(claim, executor, current_accounts)
        if claim.task.route.executor_instance_id is None:
            claim.task.route.executor_instance_id = executor.executor_instance_id
        return claim

    def renew_lease_v2(
        self,
        task_id: str,
        lease: Lease,
        executor_instance_id: str,
    ) -> Lease:
        payload = {
            "protocolVersion": "2.0",
            "agentId": self.agent_config.agent.id,
            "instanceId": self.instance_id,
            "executorInstanceId": executor_instance_id,
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

    def send_event_v2(self, event: PublisherV2TaskEvent) -> None:
        self._request(
            "POST",
            f"/tasks/{event.task_id}/events",
            json_body=event.model_dump(by_alias=True, mode="json", exclude_none=True),
            idempotency_key=event.event_id,
        )

    @staticmethod
    def _assert_exact_route(
        claim: PublisherV2ClaimResponse,
        executor: PublisherV2Executor,
        current_accounts: list[dict],
    ) -> None:
        route = claim.task.route
        if route.executor_instance_id and route.executor_instance_id != executor.executor_instance_id:
            raise SourceError(
                "CONNECTOR_UNAVAILABLE",
                "Claimed task targets another executor instance.",
                retryable=False,
            )
        if route.provider_key != executor.provider_key:
            raise SourceError(
                "CAPABILITY_MISMATCH",
                "Claimed task provider does not match the executor.",
                retryable=False,
            )
        if route.profile_id and route.profile_id != executor.profile_id:
            raise SourceError(
                "ACCOUNT_MISMATCH",
                "Claimed task profile does not match the executor profile.",
                retryable=False,
            )
        capability = next(
            (
                item
                for item in executor.capabilities
                if item.platform == route.platform and route.operation in item.operations
            ),
            None,
        )
        if capability is None:
            raise SourceError(
                "CAPABILITY_MISMATCH",
                "Claimed task operation is not supported by the executor.",
                retryable=False,
            )
        account = next(
            (
                item
                for item in current_accounts
                if item["platform"] == route.platform
                and item["accountStableId"] == route.account_stable_id
                and (not route.profile_id or item.get("profileId") == route.profile_id)
            ),
            None,
        )
        if account is None:
            raise SourceError(
                "ACCOUNT_MISMATCH",
                "Claimed task account does not match the current authenticated account.",
                retryable=False,
            )
