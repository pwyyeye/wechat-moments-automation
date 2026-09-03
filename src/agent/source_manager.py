from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable
from uuid import uuid4

from .config import AgentConfig, SourceConfig
from .credential_store import CredentialStore
from .ledger import AgentLedger
from .models import AgentSnapshot, ClaimResponse, Lease, TaskEvent
from .models_v2 import (
    PublisherV2Account,
    PublisherV2ClaimResponse,
    PublisherV2Executor,
    PublisherV2TaskEvent,
)
from .scheduler import WeightedFairScheduler
from .sources.base import SourceError, SourceMeta
from .sources.standard_http_v1 import (
    StandardHttpSource,
    device_credential_ref,
    device_enrollment_marker_ref,
)
from .sources.standard_http_v2 import StandardHttpV2Source

logger = logging.getLogger(__name__)


@dataclass
class SourceRuntime:
    source_id: str
    health_state: str = "unknown"
    consecutive_failures: int = 0
    backoff_until: datetime | None = None
    last_error_code: str | None = None
    last_error_message: str | None = None
    request_count: int = 0
    error_count: int = 0
    latency_total_ms: float = 0.0
    last_latency_ms: float | None = None
    last_operation: str | None = None

    @property
    def available(self) -> bool:
        if self.health_state in {
            "auth_error",
            "incompatible",
            "disabled",
            "unconfigured",
        }:
            return False
        return self.backoff_until is None or self.backoff_until <= datetime.now(timezone.utc)


class SourceManager:
    """Own source adapters and isolate failures between configured sources."""

    def __init__(
        self,
        config: AgentConfig,
        credential_store: CredentialStore,
        ledger: AgentLedger,
        *,
        source_factory: Callable[..., StandardHttpSource] | None = None,
    ) -> None:
        self.config = config
        self.credential_store = credential_store
        self.ledger = ledger
        self.source_factory = source_factory
        self.scheduler = WeightedFairScheduler()
        self.instance_id = f"instance-{uuid4().hex[:12]}"
        self._lock = threading.RLock()
        self._sources: dict[str, StandardHttpSource | StandardHttpV2Source] = {}
        self._runtime: dict[str, SourceRuntime] = {}
        self.reload(config)

    def reload(self, config: AgentConfig) -> None:
        with self._lock:
            self.config = config
            configured_ids = {item.id for item in config.sources}
            for source_id in set(self._sources) - configured_ids:
                self._sources.pop(source_id).close()
                self._runtime.pop(source_id, None)
            for item in config.sources:
                current = self._sources.get(item.id)
                if current is not None and current.config == item:
                    runtime = self._runtime.setdefault(item.id, SourceRuntime(item.id))
                    has_credential = self._credential_available(item)
                    if not item.enabled:
                        runtime.health_state = "disabled"
                    elif not has_credential:
                        runtime.health_state = "unconfigured"
                        runtime.last_error_code = "SOURCE_CREDENTIAL_MISSING"
                        runtime.last_error_message = "数据源尚未导入或填写 API Key"
                    elif runtime.health_state in {"disabled", "unconfigured", "auth_error"}:
                        runtime.health_state = "unknown"
                        runtime.last_error_code = None
                        runtime.last_error_message = None
                    continue
                if current is not None:
                    current.close()
                factory = self.source_factory or (
                    StandardHttpV2Source
                    if item.type == "standard-http-v2"
                    else StandardHttpSource
                )
                self._sources[item.id] = factory(
                    item,
                    config,
                    self.credential_store,
                    instance_id=self.instance_id,
                )
                has_credential = self._credential_available(item)
                self._runtime[item.id] = SourceRuntime(
                    source_id=item.id,
                    health_state=(
                        "disabled"
                        if not item.enabled
                        else "unknown"
                        if has_credential
                        else "unconfigured"
                    ),
                    last_error_code=(
                        None
                        if has_credential
                        else "SOURCE_CREDENTIAL_MISSING"
                    ),
                    last_error_message=(
                        None
                        if has_credential
                        else "数据源尚未导入或填写 API Key"
                    ),
                )
            self.scheduler.remove_missing(configured_ids)

    def source(self, source_id: str) -> StandardHttpSource | StandardHttpV2Source:
        with self._lock:
            try:
                return self._sources[source_id]
            except KeyError as error:
                raise KeyError(f"unknown source {source_id}") from error

    def config_for(self, source_id: str) -> SourceConfig:
        with self._lock:
            for source in self.config.sources:
                if source.id == source_id:
                    return source
        raise KeyError(f"unknown source {source_id}")

    def test_connection(self, source_id: str) -> SourceMeta:
        adapter = self.source(source_id)
        started = time.perf_counter()
        try:
            result = adapter.test_connection()
        except SourceError as error:
            self._record_error(source_id, error, "test", started)
            raise
        self._record_success(source_id, operation="test", started=started)
        return result

    def heartbeat_all(self, snapshot: AgentSnapshot) -> None:
        for config in self._enabled_configs(include_backoff=False, source_type="standard-http-v1"):
            started = time.perf_counter()
            try:
                self.source(config.id).heartbeat(snapshot)
            except SourceError as error:
                self._record_error(config.id, error, "heartbeat", started)
                continue
            self._record_success(
                config.id,
                heartbeat=True,
                operation="heartbeat",
                started=started,
            )

    def claim_next(self) -> tuple[str, ClaimResponse] | None:
        candidates = self.scheduler.candidates(
            self._enabled_configs(include_backoff=False, source_type="standard-http-v1")
        )
        for config in candidates:
            started = time.perf_counter()
            try:
                claim = self.source(config.id).claim()
            except SourceError as error:
                self._record_error(config.id, error, "claim", started)
                continue
            self._record_success(
                config.id,
                claimed=True,
                operation="claim",
                started=started,
            )
            if claim is None:
                continue
            if claim.task.target.account_key != config.account_key:
                error = SourceError(
                    "ACCOUNT_KEY_MISMATCH",
                    "Claimed task accountKey does not match the local source binding.",
                    retryable=False,
                )
                self._record_error(config.id, error, "claim", started, count_request=False)
                raise error
            self.scheduler.mark_served(config)
            self.ledger.update_source_state(
                config.id,
                health_state="healthy",
                claimed=True,
                served=True,
            )
            return config.id, claim
        return None

    def heartbeat_v2(
        self,
        snapshot: AgentSnapshot,
        executors: list[PublisherV2Executor],
        accounts: list[PublisherV2Account],
    ) -> None:
        for config in self._enabled_configs(
            include_backoff=False,
            source_type="standard-http-v2",
        ):
            started = time.perf_counter()
            try:
                adapter = self.source(config.id)
                if not isinstance(adapter, StandardHttpV2Source) and self.source_factory is None:
                    continue
                adapter.heartbeat(snapshot, executors, accounts)
            except SourceError as error:
                self._record_error(config.id, error, "heartbeat", started)
                continue
            self._record_success(
                config.id,
                heartbeat=True,
                operation="heartbeat",
                started=started,
            )

    def claim_next_v2(
        self,
        executors: list[PublisherV2Executor],
        accounts: list[PublisherV2Account],
    ) -> tuple[str, PublisherV2ClaimResponse] | None:
        configs = self.scheduler.candidates(
            self._enabled_configs(include_backoff=False, source_type="standard-http-v2")
        )
        for config in configs:
            adapter = self.source(config.id)
            for executor in executors:
                if executor.status != "ready":
                    continue
                started = time.perf_counter()
                try:
                    claim = adapter.claim(executor, accounts)
                except SourceError as error:
                    if error.code in {
                        "ACCOUNT_MISMATCH",
                        "CAPABILITY_MISMATCH",
                        "CONNECTOR_UNAVAILABLE",
                    }:
                        logger.info(
                            "v2 route skipped sourceId=%s executor=%s code=%s",
                            config.id,
                            executor.executor_instance_id,
                            error.code,
                        )
                        continue
                    self._record_error(config.id, error, "claim", started)
                    break
                self._record_success(
                    config.id,
                    claimed=claim is not None,
                    operation="claim",
                    started=started,
                )
                if claim is None:
                    continue
                self.scheduler.mark_served(config)
                self.ledger.update_source_state(
                    config.id,
                    health_state="healthy",
                    claimed=True,
                    served=True,
                )
                return config.id, claim
        return None

    def renew_lease_v2(
        self,
        source_id: str,
        task_id: str,
        lease: Lease,
        executor_instance_id: str,
    ) -> Lease:
        started = time.perf_counter()
        try:
            renewed = self.source(source_id).renew_lease_v2(
                task_id,
                lease,
                executor_instance_id,
            )
        except SourceError as error:
            self._record_error(source_id, error, "renew", started)
            raise
        self._record_success(source_id, operation="renew", started=started)
        return renewed

    def send_event_v2(self, source_id: str, event: PublisherV2TaskEvent) -> None:
        started = time.perf_counter()
        try:
            self.source(source_id).send_event_v2(event)
        except SourceError as error:
            self._record_error(source_id, error, "event", started)
            raise
        self._record_success(source_id, operation="event", started=started)

    def renew_lease(self, source_id: str, task_id: str, lease: Lease) -> Lease:
        started = time.perf_counter()
        try:
            renewed = self.source(source_id).renew_lease(task_id, lease)
        except SourceError as error:
            self._record_error(source_id, error, "renew", started)
            raise
        self._record_success(source_id, operation="renew", started=started)
        return renewed

    def send_event(self, source_id: str, event: TaskEvent) -> None:
        started = time.perf_counter()
        try:
            self.source(source_id).send_event(event)
        except SourceError as error:
            self._record_error(source_id, error, "event", started)
            raise
        self._record_success(source_id, operation="event", started=started)

    def status(self) -> list[dict]:
        with self._lock:
            return [
                {
                    "id": source.id,
                    "name": source.name,
                    "type": source.type,
                    "enabled": source.enabled,
                    "weight": source.weight,
                    "baseUrl": str(source.base_url),
                    "accountKey": source.account_key,
                    "authType": source.auth.type,
                    "headerName": source.auth.header_name,
                    "allowedHosts": source.media_security.allowed_hosts,
                    "allowPrivateNetwork": source.media_security.allow_private_network,
                    "hasCredential": self._credential_available(source),
                    "deviceEnrolled": self._device_enrolled(source),
                    "healthState": self._runtime[source.id].health_state,
                    "backoffUntil": (
                        self._runtime[source.id].backoff_until.isoformat()
                        if self._runtime[source.id].backoff_until
                        else None
                    ),
                    "lastErrorCode": self._runtime[source.id].last_error_code,
                    "lastErrorMessage": self._runtime[source.id].last_error_message,
                    "requestCount": self._runtime[source.id].request_count,
                    "errorCount": self._runtime[source.id].error_count,
                    "lastLatencyMs": self._runtime[source.id].last_latency_ms,
                    "averageLatencyMs": (
                        round(
                            self._runtime[source.id].latency_total_ms
                            / self._runtime[source.id].request_count,
                            1,
                        )
                        if self._runtime[source.id].request_count
                        else None
                    ),
                    "lastOperation": self._runtime[source.id].last_operation,
                    "consecutiveFailures": self._runtime[source.id].consecutive_failures,
                }
                for source in self.config.sources
            ]

    def close(self) -> None:
        with self._lock:
            for source in self._sources.values():
                source.close()
            self._sources.clear()

    def _enabled_configs(
        self,
        *,
        include_backoff: bool,
        source_type: str | None = None,
    ) -> list[SourceConfig]:
        with self._lock:
            return [
                source
                for source in self.config.sources
                if source.enabled
                and (source_type is None or source.type == source_type)
                and (include_backoff or self._runtime[source.id].available)
            ]

    def _record_success(
        self,
        source_id: str,
        *,
        heartbeat: bool = False,
        claimed: bool = False,
        operation: str,
        started: float,
    ) -> None:
        with self._lock:
            runtime = self._runtime.get(source_id)
            if runtime is None:
                return
            self._record_latency(runtime, operation, started)
            runtime.health_state = "healthy"
            runtime.consecutive_failures = 0
            runtime.backoff_until = None
            runtime.last_error_code = None
            runtime.last_error_message = None
        self.ledger.update_source_state(
            source_id,
            health_state="healthy",
            heartbeat=heartbeat,
            claimed=claimed,
        )

    def _record_error(
        self,
        source_id: str,
        error: SourceError,
        operation: str,
        started: float,
        *,
        count_request: bool = True,
    ) -> None:
        with self._lock:
            runtime = self._runtime.get(source_id)
            if runtime is None:
                return
            if count_request:
                self._record_latency(runtime, operation, started)
            runtime.error_count += 1
            runtime.consecutive_failures += 1
            runtime.last_error_code = error.code
            runtime.last_error_message = str(error)
            if error.code == "SOURCE_CREDENTIAL_MISSING":
                runtime.health_state = "unconfigured"
                runtime.backoff_until = None
            elif error.code == "DEVICE_DISABLED":
                runtime.health_state = "disabled_by_server"
                runtime.backoff_until = datetime.now(timezone.utc) + timedelta(seconds=60)
            elif error.code == "SOURCE_AUTH_FAILED" or error.status_code in {401, 403}:
                runtime.health_state = "auth_error"
                runtime.backoff_until = None
            elif error.code == "SOURCE_PROTOCOL_INCOMPATIBLE":
                runtime.health_state = "incompatible"
                runtime.backoff_until = None
            else:
                runtime.health_state = "degraded"
                configured = error.retry_after_seconds
                delay = configured if configured is not None else min(
                    300,
                    2 ** min(runtime.consecutive_failures + 1, 8),
                )
                runtime.backoff_until = datetime.now(timezone.utc) + timedelta(seconds=delay)
        self.ledger.update_source_state(
            source_id,
            health_state=runtime.health_state,
            error_code=error.code,
            error_message=str(error),
            backoff_until=(
                runtime.backoff_until.isoformat() if runtime.backoff_until else None
            ),
        )
        logger.warning(
            "source operation failed sourceId=%s code=%s retryable=%s",
            source_id,
            error.code,
            error.retryable,
        )

    def _credential_available(self, source: SourceConfig) -> bool:
        try:
            return bool(self.credential_store.get(source.auth.credential_ref))
        except (FileNotFoundError, KeyError, ValueError):
            return False

    def _device_enrolled(self, source: SourceConfig) -> bool:
        try:
            return bool(
                self.credential_store.get(device_credential_ref(source.id))
                and self.credential_store.get(device_enrollment_marker_ref(source.id))
            )
        except (FileNotFoundError, KeyError, ValueError):
            return False

    @staticmethod
    def _record_latency(
        runtime: SourceRuntime,
        operation: str,
        started: float,
    ) -> None:
        latency_ms = round((time.perf_counter() - started) * 1000, 1)
        runtime.request_count += 1
        runtime.latency_total_ms += latency_ms
        runtime.last_latency_ms = latency_ms
        runtime.last_operation = operation
