from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from .credential_store import PayloadProtector
from .models_v2 import PublisherV2ClaimResponse, PublisherV2TaskEvent


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class ActiveV2LedgerTask:
    source_id: str
    claim: PublisherV2ClaimResponse
    attempt: int
    state: str
    action_intent_at: str | None


@dataclass(frozen=True)
class PendingV2OutboxEvent:
    event_id: str
    source_id: str
    task_id: str
    event: PublisherV2TaskEvent
    attempt_count: int


class AgentV2Ledger:
    """Durable V2 task ledger with an atomic action-intent boundary."""

    def __init__(self, path: Path, protector: PayloadProtector) -> None:
        self.path = path
        self.protector = protector
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._initialize()

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode = WAL")
        try:
            yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS task_ledger_v2 (
                    source_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    executor_instance_id TEXT NOT NULL,
                    state TEXT NOT NULL,
                    attempt INTEGER NOT NULL,
                    lease_token_encrypted BLOB NOT NULL,
                    lease_expires_at TEXT NOT NULL,
                    task_json TEXT NOT NULL,
                    action_intent_at TEXT NULL,
                    result_json TEXT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (source_id, task_id, idempotency_key)
                );

                CREATE UNIQUE INDEX IF NOT EXISTS task_ledger_v2_one_active
                    ON task_ledger_v2 ((CASE
                        WHEN state IN ('claimed', 'executing', 'final_action_intent', 'completing')
                        THEN 1 ELSE NULL END));

                CREATE TABLE IF NOT EXISTS outbox_event_v2 (
                    event_id TEXT PRIMARY KEY,
                    source_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    payload_encrypted BLOB NOT NULL,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at TEXT NOT NULL,
                    delivered_at TEXT NULL,
                    last_error TEXT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS outbox_event_v2_pending
                    ON outbox_event_v2 (delivered_at, next_attempt_at);
                """
            )
            connection.commit()

    def record_claim(
        self,
        source_id: str,
        claim: PublisherV2ClaimResponse,
        accepted_event: PublisherV2TaskEvent,
    ) -> bool:
        task = claim.task
        now = utc_now_iso()
        encrypted_token = self.protector.protect(claim.lease.token.encode("utf-8"))
        with self._lock, self._connection() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                existing = connection.execute(
                    """SELECT state, attempt, action_intent_at FROM task_ledger_v2
                       WHERE source_id = ? AND task_id = ? AND idempotency_key = ?""",
                    (source_id, task.task_id, task.idempotency_key),
                ).fetchone()
                if existing:
                    can_retry = (
                        existing["state"] == "failed"
                        and existing["action_intent_at"] is None
                        and claim.attempt > int(existing["attempt"])
                    )
                    if not can_retry:
                        connection.rollback()
                        return False
                active = connection.execute(
                    """SELECT task_id FROM task_ledger_v2
                       WHERE state IN ('claimed', 'executing', 'final_action_intent', 'completing')
                       LIMIT 1"""
                ).fetchone()
                if active:
                    raise RuntimeError("another v2 task is already active")
                values = (
                    task.revision,
                    task.route.executor_instance_id or "",
                    claim.attempt,
                    encrypted_token,
                    claim.lease.expires_at.isoformat(),
                    task.model_dump_json(by_alias=True),
                    now,
                    source_id,
                    task.task_id,
                    task.idempotency_key,
                )
                if existing:
                    connection.execute(
                        """UPDATE task_ledger_v2
                           SET revision = ?, executor_instance_id = ?, state = 'claimed',
                               attempt = ?, lease_token_encrypted = ?, lease_expires_at = ?,
                               task_json = ?, action_intent_at = NULL, result_json = NULL,
                               updated_at = ?
                           WHERE source_id = ? AND task_id = ? AND idempotency_key = ?""",
                        values,
                    )
                else:
                    connection.execute(
                        """INSERT INTO task_ledger_v2 (
                               revision, executor_instance_id, attempt, lease_token_encrypted,
                               lease_expires_at, task_json, updated_at, source_id, task_id,
                               idempotency_key, state, created_at
                           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'claimed', ?)""",
                        (*values, now),
                    )
                self._insert_outbox(connection, source_id, accepted_event, now)
                connection.commit()
                return True
            except Exception:
                connection.rollback()
                raise

    def get_active_task(self) -> ActiveV2LedgerTask | None:
        with self._connection() as connection:
            row = connection.execute(
                """SELECT * FROM task_ledger_v2
                   WHERE state IN ('claimed', 'executing', 'final_action_intent', 'completing')
                   LIMIT 1"""
            ).fetchone()
            if row is None:
                return None
            token = self.protector.unprotect(row["lease_token_encrypted"]).decode("utf-8")
            claim = PublisherV2ClaimResponse.model_validate(
                {
                    "lease": {
                        "token": token,
                        "expiresAt": row["lease_expires_at"],
                        "renewAfterSeconds": 30,
                    },
                    "task": json.loads(row["task_json"]),
                    "attempt": row["attempt"],
                    "serverTime": row["created_at"],
                    "requestId": "local-v2-recovery",
                }
            )
            return ActiveV2LedgerTask(
                source_id=row["source_id"],
                claim=claim,
                attempt=int(row["attempt"]),
                state=row["state"],
                action_intent_at=row["action_intent_at"],
            )

    def update_lease(self, source_id: str, task_id: str, lease) -> None:
        token = self.protector.protect(lease.token.encode("utf-8"))
        with self._connection() as connection:
            connection.execute(
                """UPDATE task_ledger_v2
                   SET lease_token_encrypted = ?, lease_expires_at = ?, updated_at = ?
                   WHERE source_id = ? AND task_id = ?""",
                (token, lease.expires_at.isoformat(), utc_now_iso(), source_id, task_id),
            )
            connection.commit()

    def set_state(self, source_id: str, task_id: str, state: str) -> None:
        with self._connection() as connection:
            connection.execute(
                "UPDATE task_ledger_v2 SET state = ?, updated_at = ? WHERE source_id = ? AND task_id = ?",
                (state, utc_now_iso(), source_id, task_id),
            )
            connection.commit()

    def enqueue_event(self, source_id: str, event: PublisherV2TaskEvent) -> None:
        now = utc_now_iso()
        with self._connection() as connection:
            self._insert_outbox(connection, source_id, event, now)
            connection.commit()

    def record_action_intent(self, source_id: str, event: PublisherV2TaskEvent) -> None:
        now = utc_now_iso()
        with self._lock, self._connection() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT state, action_intent_at FROM task_ledger_v2 WHERE source_id = ? AND task_id = ?",
                    (source_id, event.task_id),
                ).fetchone()
                if row is None or row["action_intent_at"]:
                    raise RuntimeError("v2 action intent is missing or already recorded")
                connection.execute(
                    """UPDATE task_ledger_v2
                       SET state = 'final_action_intent', action_intent_at = ?, updated_at = ?
                       WHERE source_id = ? AND task_id = ?""",
                    (event.occurred_at.isoformat(), now, source_id, event.task_id),
                )
                self._insert_outbox(connection, source_id, event, now)
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def finish_task(
        self,
        source_id: str,
        task_id: str,
        state: str,
        event: PublisherV2TaskEvent,
    ) -> None:
        if state not in {"succeeded", "failed", "uncertain"}:
            raise ValueError("invalid v2 terminal state")
        now = utc_now_iso()
        with self._lock, self._connection() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """UPDATE task_ledger_v2
                       SET state = ?, result_json = ?, updated_at = ?
                       WHERE source_id = ? AND task_id = ?""",
                    (
                        state,
                        event.model_dump_json(by_alias=True, exclude_none=True),
                        now,
                        source_id,
                        task_id,
                    ),
                )
                self._insert_outbox(connection, source_id, event, now)
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def pending_outbox(self, limit: int = 20) -> list[PendingV2OutboxEvent]:
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT * FROM outbox_event_v2
                   WHERE delivered_at IS NULL AND next_attempt_at <= ?
                   ORDER BY created_at ASC LIMIT ?""",
                (utc_now_iso(), limit),
            ).fetchall()
            return [
                PendingV2OutboxEvent(
                    event_id=row["event_id"],
                    source_id=row["source_id"],
                    task_id=row["task_id"],
                    event=PublisherV2TaskEvent.model_validate_json(
                        self.protector.unprotect(row["payload_encrypted"])
                    ),
                    attempt_count=int(row["attempt_count"]),
                )
                for row in rows
            ]

    def mark_outbox_delivered(self, event_id: str) -> None:
        with self._connection() as connection:
            connection.execute(
                "UPDATE outbox_event_v2 SET delivered_at = ?, last_error = NULL WHERE event_id = ?",
                (utc_now_iso(), event_id),
            )
            connection.commit()

    def mark_outbox_failed(self, event_id: str, message: str, delay_seconds: int) -> None:
        next_attempt = datetime.fromtimestamp(
            datetime.now(timezone.utc).timestamp() + delay_seconds,
            timezone.utc,
        ).isoformat()
        with self._connection() as connection:
            connection.execute(
                """UPDATE outbox_event_v2
                   SET attempt_count = attempt_count + 1, next_attempt_at = ?, last_error = ?
                   WHERE event_id = ?""",
                (next_attempt, message[:1000], event_id),
            )
            connection.commit()

    def outbox_backlog(self) -> int:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT COUNT(*) count FROM outbox_event_v2 WHERE delivered_at IS NULL"
            ).fetchone()
            return int(row["count"])

    def recent_tasks(self, limit: int = 50) -> list[dict]:
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT source_id, task_id, idempotency_key, revision,
                          executor_instance_id, state, attempt, action_intent_at, updated_at
                   FROM task_ledger_v2 ORDER BY updated_at DESC LIMIT ?""",
                (max(1, min(limit, 200)),),
            ).fetchall()
            return [dict(row) for row in rows]

    def task_record(self, source_id: str, task_id: str, idempotency_key: str) -> dict | None:
        with self._connection() as connection:
            row = connection.execute(
                """SELECT * FROM task_ledger_v2
                   WHERE source_id = ? AND task_id = ? AND idempotency_key = ?""",
                (source_id, task_id, idempotency_key),
            ).fetchone()
            return dict(row) if row else None

    def _insert_outbox(
        self,
        connection: sqlite3.Connection,
        source_id: str,
        event: PublisherV2TaskEvent,
        now: str,
    ) -> None:
        encrypted = self.protector.protect(
            event.model_dump_json(by_alias=True, exclude_none=True).encode("utf-8")
        )
        connection.execute(
            """INSERT OR IGNORE INTO outbox_event_v2 (
                   event_id, source_id, task_id, payload_encrypted, next_attempt_at, created_at
               ) VALUES (?, ?, ?, ?, ?, ?)""",
            (event.event_id, source_id, event.task_id, encrypted, now, now),
        )
