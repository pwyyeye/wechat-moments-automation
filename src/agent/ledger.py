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
from .models import ClaimResponse, TaskEvent


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class PendingOutboxEvent:
    event_id: str
    source_id: str
    task_id: str
    event: TaskEvent
    attempt_count: int


@dataclass(frozen=True)
class ActiveLedgerTask:
    source_id: str
    claim: ClaimResponse
    attempt: int
    state: str
    final_click_intent_at: str | None


class AgentLedger:
    """Durable task state and transactional outbox for at-most-once UI execution."""

    def __init__(self, path: Path, protector: PayloadProtector):
        self.path = path
        self.protector = protector
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._initialize()

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        try:
            yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS task_ledger (
                    source_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    attempt INTEGER NOT NULL,
                    lease_token_encrypted BLOB NOT NULL,
                    lease_expires_at TEXT NOT NULL,
                    task_json TEXT NOT NULL,
                    final_click_intent_at TEXT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (source_id, task_id, idempotency_key)
                );

                CREATE UNIQUE INDEX IF NOT EXISTS task_ledger_one_active_task
                    ON task_ledger ((CASE
                        WHEN state IN ('claimed', 'executing', 'final_click_intent', 'confirming')
                        THEN 1 ELSE NULL END));

                CREATE TABLE IF NOT EXISTS outbox_event (
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

                CREATE INDEX IF NOT EXISTS outbox_pending
                    ON outbox_event (delivered_at, next_attempt_at);

                CREATE TABLE IF NOT EXISTS source_runtime_state (
                    source_id TEXT PRIMARY KEY,
                    last_heartbeat_at TEXT NULL,
                    last_claim_at TEXT NULL,
                    last_served_at TEXT NULL,
                    backoff_until TEXT NULL,
                    health_state TEXT NOT NULL DEFAULT 'unknown',
                    last_error_code TEXT NULL,
                    last_error_message TEXT NULL
                );
                """
            )
            connection.commit()

    def has_active_task(self) -> bool:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM task_ledger
                WHERE state IN ('claimed', 'executing', 'final_click_intent', 'confirming')
                LIMIT 1
                """
            ).fetchone()
            return row is not None

    def record_claim(
        self,
        source_id: str,
        claim: ClaimResponse,
        accepted_event: TaskEvent | None = None,
    ) -> bool:
        task = claim.task
        now = utc_now_iso()
        encrypted_token = self.protector.protect(claim.lease.token.encode("utf-8"))
        task_json = task.model_dump_json(by_alias=True)
        with self._lock, self._connection() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                existing = connection.execute(
                    """
                    SELECT state, attempt, final_click_intent_at FROM task_ledger
                    WHERE source_id = ? AND task_id = ? AND idempotency_key = ?
                    """,
                    (source_id, task.task_id, task.idempotency_key),
                ).fetchone()
                if existing:
                    can_retry = (
                        existing["state"] == "failed"
                        and existing["final_click_intent_at"] is None
                        and claim.attempt > int(existing["attempt"])
                    )
                    if not can_retry:
                        connection.rollback()
                        return False
                active = connection.execute(
                    """
                    SELECT task_id FROM task_ledger
                    WHERE state IN ('claimed', 'executing', 'final_click_intent', 'confirming')
                    LIMIT 1
                    """
                ).fetchone()
                if active:
                    connection.rollback()
                    raise RuntimeError("another task is already active")
                if existing:
                    connection.execute(
                        """
                        UPDATE task_ledger
                        SET revision = ?, state = 'claimed', attempt = ?,
                            lease_token_encrypted = ?, lease_expires_at = ?,
                            task_json = ?, final_click_intent_at = NULL, updated_at = ?
                        WHERE source_id = ? AND task_id = ? AND idempotency_key = ?
                        """,
                        (
                            task.revision,
                            claim.attempt,
                            encrypted_token,
                            claim.lease.expires_at.isoformat(),
                            task_json,
                            now,
                            source_id,
                            task.task_id,
                            task.idempotency_key,
                        ),
                    )
                else:
                    connection.execute(
                        """
                        INSERT INTO task_ledger (
                            source_id, task_id, idempotency_key, revision, state, attempt,
                            lease_token_encrypted, lease_expires_at, task_json,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, ?, 'claimed', ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            source_id,
                            task.task_id,
                            task.idempotency_key,
                            task.revision,
                            claim.attempt,
                            encrypted_token,
                            claim.lease.expires_at.isoformat(),
                            task_json,
                            now,
                            now,
                        ),
                    )
                if accepted_event is not None:
                    self._insert_outbox(connection, source_id, accepted_event, now)
                connection.commit()
                return True
            except Exception:
                connection.rollback()
                raise

    def get_active_task(self) -> ActiveLedgerTask | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM task_ledger
                WHERE state IN ('claimed', 'executing', 'final_click_intent', 'confirming')
                LIMIT 1
                """
            ).fetchone()
            if not row:
                return None
            token = self.protector.unprotect(row["lease_token_encrypted"]).decode("utf-8")
            claim = ClaimResponse.model_validate(
                {
                    "lease": {
                        "token": token,
                        "expiresAt": row["lease_expires_at"],
                        "renewAfterSeconds": 30,
                    },
                    "task": json.loads(row["task_json"]),
                    "attempt": int(row["attempt"]),
                    "serverTime": row["created_at"],
                    "requestId": "local-recovery",
                }
            )
            return ActiveLedgerTask(
                source_id=row["source_id"],
                claim=claim,
                attempt=int(row["attempt"]),
                state=row["state"],
                final_click_intent_at=row["final_click_intent_at"],
            )

    def get_active_claim(self) -> tuple[str, ClaimResponse, int] | None:
        active = self.get_active_task()
        if active is None:
            return None
        return active.source_id, active.claim, active.attempt

    def update_lease(self, source_id: str, task_id: str, lease) -> None:
        encrypted_token = self.protector.protect(lease.token.encode("utf-8"))
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE task_ledger
                SET lease_token_encrypted = ?, lease_expires_at = ?, updated_at = ?
                WHERE source_id = ? AND task_id = ?
                """,
                (
                    encrypted_token,
                    lease.expires_at.isoformat(),
                    utc_now_iso(),
                    source_id,
                    task_id,
                ),
            )
            connection.commit()

    def set_state(self, source_id: str, task_id: str, state: str) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE task_ledger SET state = ?, updated_at = ?
                WHERE source_id = ? AND task_id = ?
                """,
                (state, utc_now_iso(), source_id, task_id),
            )
            connection.commit()

    def enqueue_event(self, source_id: str, event: TaskEvent) -> None:
        now = utc_now_iso()
        with self._connection() as connection:
            self._insert_outbox(connection, source_id, event, now)
            connection.commit()

    def _insert_outbox(
        self,
        connection: sqlite3.Connection,
        source_id: str,
        event: TaskEvent,
        now: str,
    ) -> None:
        payload = event.model_dump_json(by_alias=True, exclude_none=True).encode("utf-8")
        encrypted = self.protector.protect(payload)
        connection.execute(
            """
            INSERT OR IGNORE INTO outbox_event (
                event_id, source_id, task_id, payload_encrypted,
                next_attempt_at, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (event.event_id, source_id, event.task_id, encrypted, now, now),
        )

    def record_final_click_intent(self, source_id: str, event: TaskEvent) -> None:
        payload = event.model_dump_json(by_alias=True, exclude_none=True).encode("utf-8")
        encrypted = self.protector.protect(payload)
        now = utc_now_iso()
        with self._lock, self._connection() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    """
                    SELECT state, final_click_intent_at FROM task_ledger
                    WHERE source_id = ? AND task_id = ?
                    """,
                    (source_id, event.task_id),
                ).fetchone()
                if not row:
                    raise RuntimeError("task is missing from the local ledger")
                if row["final_click_intent_at"]:
                    raise RuntimeError("final click intent is already recorded")
                if row["state"] not in {"claimed", "executing"}:
                    raise RuntimeError(f"task state {row['state']} cannot enter final click")
                connection.execute(
                    """
                    UPDATE task_ledger
                    SET state = 'final_click_intent', final_click_intent_at = ?, updated_at = ?
                    WHERE source_id = ? AND task_id = ?
                    """,
                    (event.occurred_at.isoformat(), now, source_id, event.task_id),
                )
                connection.execute(
                    """
                    INSERT INTO outbox_event (
                        event_id, source_id, task_id, payload_encrypted,
                        next_attempt_at, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (event.event_id, source_id, event.task_id, encrypted, now, now),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def finish_task(self, source_id: str, task_id: str, state: str, event: TaskEvent) -> None:
        if state not in {"succeeded", "failed", "uncertain"}:
            raise ValueError("invalid terminal local state")
        payload = event.model_dump_json(by_alias=True, exclude_none=True).encode("utf-8")
        encrypted = self.protector.protect(payload)
        now = utc_now_iso()
        with self._lock, self._connection() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """
                    UPDATE task_ledger SET state = ?, updated_at = ?
                    WHERE source_id = ? AND task_id = ?
                    """,
                    (state, now, source_id, task_id),
                )
                connection.execute(
                    """
                    INSERT OR IGNORE INTO outbox_event (
                        event_id, source_id, task_id, payload_encrypted,
                        next_attempt_at, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (event.event_id, source_id, task_id, encrypted, now, now),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def pending_outbox(self, limit: int = 20) -> list[PendingOutboxEvent]:
        now = utc_now_iso()
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM outbox_event
                WHERE delivered_at IS NULL AND next_attempt_at <= ?
                ORDER BY created_at ASC
                LIMIT ?
                """,
                (now, limit),
            ).fetchall()
            return [
                PendingOutboxEvent(
                    event_id=row["event_id"],
                    source_id=row["source_id"],
                    task_id=row["task_id"],
                    event=TaskEvent.model_validate_json(
                        self.protector.unprotect(row["payload_encrypted"])
                    ),
                    attempt_count=int(row["attempt_count"]),
                )
                for row in rows
            ]

    def mark_outbox_delivered(self, event_id: str) -> None:
        with self._connection() as connection:
            connection.execute(
                "UPDATE outbox_event SET delivered_at = ?, last_error = NULL WHERE event_id = ?",
                (utc_now_iso(), event_id),
            )
            connection.commit()

    def mark_outbox_failed(self, event_id: str, error: str, delay_seconds: int) -> None:
        next_attempt = datetime.fromtimestamp(
            datetime.now(timezone.utc).timestamp() + delay_seconds,
            tz=timezone.utc,
        ).isoformat()
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE outbox_event
                SET attempt_count = attempt_count + 1,
                    next_attempt_at = ?,
                    last_error = ?
                WHERE event_id = ?
                """,
                (next_attempt, error[:1000], event_id),
            )
            connection.commit()

    def outbox_backlog(self) -> int:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM outbox_event WHERE delivered_at IS NULL"
            ).fetchone()
            return int(row["count"])

    def update_source_state(
        self,
        source_id: str,
        *,
        health_state: str,
        error_code: str | None = None,
        error_message: str | None = None,
        heartbeat: bool = False,
        claimed: bool = False,
        served: bool = False,
        backoff_until: str | None = None,
    ) -> None:
        now = utc_now_iso()
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO source_runtime_state (
                    source_id, health_state, last_error_code, last_error_message,
                    last_heartbeat_at, last_claim_at, last_served_at, backoff_until
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_id) DO UPDATE SET
                    health_state = excluded.health_state,
                    last_error_code = excluded.last_error_code,
                    last_error_message = excluded.last_error_message,
                    last_heartbeat_at = COALESCE(excluded.last_heartbeat_at, last_heartbeat_at),
                    last_claim_at = COALESCE(excluded.last_claim_at, last_claim_at),
                    last_served_at = COALESCE(excluded.last_served_at, last_served_at),
                    backoff_until = excluded.backoff_until
                """,
                (
                    source_id,
                    health_state,
                    error_code,
                    error_message[:1000] if error_message else None,
                    now if heartbeat else None,
                    now if claimed else None,
                    now if served else None,
                    backoff_until,
                ),
            )
            connection.commit()

    def source_states(self) -> list[dict]:
        with self._connection() as connection:
            return [dict(row) for row in connection.execute(
                "SELECT * FROM source_runtime_state ORDER BY source_id"
            ).fetchall()]

    def recent_tasks(self, limit: int = 50) -> list[dict]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT source_id, task_id, idempotency_key, revision, state, attempt,
                       lease_expires_at, final_click_intent_at, created_at, updated_at
                FROM task_ledger
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (max(1, min(limit, 200)),),
            ).fetchall()
            return [dict(row) for row in rows]

    def task_record(
        self,
        source_id: str,
        task_id: str,
        idempotency_key: str,
    ) -> dict | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT source_id, task_id, idempotency_key, state, attempt,
                       final_click_intent_at, lease_expires_at, updated_at
                FROM task_ledger
                WHERE source_id = ? AND task_id = ? AND idempotency_key = ?
                """,
                (source_id, task_id, idempotency_key),
            ).fetchone()
            return dict(row) if row else None

    def oldest_outbox_age_seconds(self) -> float | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT MIN(created_at) AS oldest
                FROM outbox_event
                WHERE delivered_at IS NULL
                """
            ).fetchone()
        if not row or not row["oldest"]:
            return None
        oldest = datetime.fromisoformat(row["oldest"].replace("Z", "+00:00"))
        return max(0.0, (datetime.now(timezone.utc) - oldest).total_seconds())
