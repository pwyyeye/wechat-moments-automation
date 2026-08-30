from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ..models import AgentSnapshot, ClaimResponse, Lease, TaskEvent


@dataclass(frozen=True)
class SourceMeta:
    protocol: str
    versions: list[str]
    source_name: str
    server_time: str


class SourceError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool,
        status_code: int | None = None,
        retry_after_seconds: int | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds


class ContentSource(Protocol):
    source_id: str

    def test_connection(self) -> SourceMeta: ...

    def heartbeat(self, snapshot: AgentSnapshot) -> None: ...

    def claim(self) -> ClaimResponse | None: ...

    def renew_lease(self, task_id: str, lease: Lease) -> Lease: ...

    def send_event(self, event: TaskEvent) -> None: ...

    def close(self) -> None: ...
