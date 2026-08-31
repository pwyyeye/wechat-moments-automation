from __future__ import annotations

import hashlib
import logging
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from uuid import uuid4

from .executor import PublishExecutor
from .ledger import ActiveLedgerTask, AgentLedger
from .media_cache import MediaCache, MediaDownloadError
from .models import (
    Confirmation,
    EventDetails,
    EventError,
    EventResult,
    PublisherTask,
    TaskEvent,
)
from .outbox import OutboxDispatcher
from .source_manager import SourceManager
from .sources.base import SourceError

logger = logging.getLogger(__name__)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class LeaseRenewer:
    def __init__(
        self,
        sources: SourceManager,
        ledger: AgentLedger,
        source_id: str,
        active: ActiveLedgerTask,
    ) -> None:
        self.sources = sources
        self.ledger = ledger
        self.source_id = source_id
        self.task_id = active.claim.task.task_id
        self.lease = active.claim.lease
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, name="lease-renewer", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        self.thread.join(timeout=5)

    def _run(self) -> None:
        while not self.stop_event.wait(max(5, self.lease.renew_after_seconds)):
            try:
                self.lease = self.sources.renew_lease(
                    self.source_id,
                    self.task_id,
                    self.lease,
                )
                self.ledger.update_lease(self.source_id, self.task_id, self.lease)
            except SourceError as error:
                logger.warning(
                    "lease renew failed sourceId=%s taskId=%s code=%s",
                    self.source_id,
                    self.task_id,
                    error.code,
                )


class PublisherWorker:
    """One-at-a-time durable worker for all configured content sources."""

    def __init__(
        self,
        ledger: AgentLedger,
        sources: SourceManager,
        outbox: OutboxDispatcher,
        media_cache: MediaCache,
        executor: PublishExecutor,
    ) -> None:
        self.ledger = ledger
        self.sources = sources
        self.outbox = outbox
        self.media_cache = media_cache
        self.executor = executor
        self._lock = threading.Lock()
        self.last_error_code: str | None = None
        self.last_error_message: str | None = None
        self.last_stage_durations_ms: dict[str, float] = {}

    @property
    def is_active(self) -> bool:
        return self._lock.locked()

    @contextmanager
    def exclusive_desktop_action(self):
        """Pause polling while a confirmed local action manipulates WeChat."""
        self._lock.acquire()
        try:
            yield
        finally:
            self._lock.release()

    def run_once(self) -> bool:
        if not self._lock.acquire(blocking=False):
            return False
        try:
            cycle_started = time.perf_counter()
            self.outbox.flush()
            active = self.ledger.get_active_task()
            if active is not None:
                if active.state in {"final_click_intent", "confirming"}:
                    self._recover_after_final_click(active)
                    self.outbox.flush()
                    return True
                if active.claim.lease.expires_at <= utc_now():
                    try:
                        renewed = self.sources.renew_lease(
                            active.source_id,
                            active.claim.task.task_id,
                            active.claim.lease,
                        )
                    except SourceError:
                        # Safe to abandon locally because no final click intent
                        # exists; the data source owns retry policy after expiry.
                        self.ledger.set_state(
                            active.source_id,
                            active.claim.task.task_id,
                            "failed",
                        )
                        return True
                    self.ledger.update_lease(
                        active.source_id,
                        active.claim.task.task_id,
                        renewed,
                    )
                    active.claim.lease = renewed
                self._execute(active)
                self.outbox.flush()
                return True

            claimed = self.sources.claim_next()
            if claimed is None:
                return False
            source_id, response = claimed
            accepted = self._event(
                source_id,
                response.task,
                response.lease.token,
                response.attempt,
                "accepted",
                "claim",
                "Task persisted in the local ledger.",
            )
            if not self.ledger.record_claim(source_id, response, accepted):
                prior = self.ledger.task_record(
                    source_id,
                    response.task.task_id,
                    response.task.idempotency_key,
                )
                event_type = (
                    "uncertain"
                    if prior and prior.get("final_click_intent_at")
                    else "failed"
                )
                duplicate_event = self._terminal_event(
                    source_id,
                    response.task,
                    response.lease.token,
                    response.attempt,
                    event_type,
                    "report",
                    "Local ledger rejected a duplicate claim; desktop execution was skipped.",
                    "LOCAL_DUPLICATE_CLAIM",
                    retryable=False,
                )
                self.ledger.enqueue_event(source_id, duplicate_event)
                self.outbox.flush()
                return True
            active = self.ledger.get_active_task()
            if active is None:
                raise RuntimeError("claimed task disappeared from the local ledger")
            self.outbox.flush()
            self._execute(active)
            self.outbox.flush()
            return True
        finally:
            if "cycle_started" in locals():
                self._record_duration("workerCycle", cycle_started)
            self._lock.release()

    def _execute(self, active: ActiveLedgerTask) -> None:
        source_id = active.source_id
        claim = active.claim
        task = claim.task
        renewer = LeaseRenewer(self.sources, self.ledger, source_id, active)
        renewer.start()
        try:
            self.ledger.set_state(source_id, task.task_id, "executing")
            self.ledger.enqueue_event(
                source_id,
                self._event(
                    source_id,
                    task,
                    claim.lease.token,
                    claim.attempt,
                    "preflight_started",
                    "preflight",
                    "Desktop and WeChat preflight started.",
                ),
            )
            source_config = self.sources.config_for(source_id)
            download_started = time.perf_counter()
            try:
                media_paths = self.media_cache.download_task(source_config, task)
            except MediaDownloadError as error:
                self._finish_failure(
                    active,
                    error.code,
                    "download",
                    str(error),
                    error.retryable,
                )
                return
            finally:
                self._record_duration("mediaDownload", download_started)

            preflight_started = time.perf_counter()
            snapshot = self.executor.preflight(task)
            self._record_duration("preflight", preflight_started)
            preflight_error = self._preflight_error(snapshot)
            if preflight_error is not None:
                code, message = preflight_error
                self._finish_failure(active, code, "preflight", message, True)
                return

            self.ledger.enqueue_event(
                source_id,
                self._event(
                    source_id,
                    task,
                    claim.lease.token,
                    claim.attempt,
                    "publish_started",
                    "editor",
                    "WeChat editor execution started.",
                ),
            )

            def before_final_click() -> None:
                event = self._event(
                    source_id,
                    task,
                    claim.lease.token,
                    claim.attempt,
                    "final_click_intent",
                    "before_final_click",
                    "Final-click intent committed locally; automatic retry is disabled.",
                )
                self.ledger.record_final_click_intent(source_id, event)

            def after_final_click() -> None:
                self.ledger.set_state(source_id, task.task_id, "confirming")
                self.ledger.enqueue_event(
                    source_id,
                    self._event(
                        source_id,
                        task,
                        claim.lease.token,
                        claim.attempt,
                        "confirmation_started",
                        "confirmation",
                        "Final click completed; post-publish confirmation started.",
                    ),
                )

            publish_started = time.perf_counter()
            try:
                result = self.executor.publish(
                    task,
                    media_paths,
                    before_final_click,
                    after_final_click,
                )
            finally:
                self._record_duration("desktopPublishAndConfirm", publish_started)
            if result.published:
                digest = hashlib.sha256(task.content.text.strip().encode("utf-8")).hexdigest()
                event = self._terminal_event(
                    source_id,
                    task,
                    claim.lease.token,
                    claim.attempt,
                    "succeeded",
                    "confirmation",
                    "Expected post was confirmed in the Moments feed.",
                    confirmation=Confirmation(
                        mode="feed_text_ocr",
                        state="confirmed",
                        matchedTextHash=digest,
                    ),
                )
                self.ledger.finish_task(source_id, task.task_id, "succeeded", event)
            elif result.final_click_intent or self._is_after_final_click(source_id, task):
                self._finish_uncertain(active, result.error_message or "Result is unconfirmed.")
            else:
                self._finish_failure(
                    active,
                    "CONTENT_INPUT_FAILED",
                    "before_final_click",
                    result.error_message or "Desktop publisher failed before final click.",
                    True,
                )
        except Exception as error:
            logger.exception(
                "worker execution failed sourceId=%s taskId=%s",
                source_id,
                task.task_id,
            )
            if self._is_after_final_click(source_id, task):
                self._finish_uncertain(active, str(error))
            else:
                self._finish_failure(
                    active,
                    "EDITOR_OPEN_FAILED",
                    "before_final_click",
                    str(error),
                    True,
                )
        finally:
            renewer.stop()

    def _recover_after_final_click(self, active: ActiveLedgerTask) -> None:
        self._finish_uncertain(
            active,
            "Agent restarted after final-click intent; the task was not clicked again.",
        )

    def _finish_failure(
        self,
        active: ActiveLedgerTask,
        code: str,
        stage: str,
        message: str,
        retryable: bool,
    ) -> None:
        event = self._terminal_event(
            active.source_id,
            active.claim.task,
            active.claim.lease.token,
            active.attempt,
            "failed",
            stage,
            message,
            code,
            retryable=retryable,
        )
        self.ledger.finish_task(
            active.source_id,
            active.claim.task.task_id,
            "failed",
            event,
        )
        self.last_error_code = code
        self.last_error_message = message

    def _finish_uncertain(self, active: ActiveLedgerTask, message: str) -> None:
        event = self._terminal_event(
            active.source_id,
            active.claim.task,
            active.claim.lease.token,
            active.attempt,
            "uncertain",
            "confirmation",
            message,
            "POST_CLICK_UNCONFIRMED",
            retryable=False,
            confirmation=Confirmation(
                mode="feed_text_ocr",
                state="unconfirmed",
            ),
        )
        self.ledger.finish_task(
            active.source_id,
            active.claim.task.task_id,
            "uncertain",
            event,
        )
        self.last_error_code = "POST_CLICK_UNCONFIRMED"
        self.last_error_message = message

    def _event(
        self,
        source_id: str,
        task: PublisherTask,
        lease_token: str,
        attempt: int,
        event_type: str,
        stage: str,
        message: str,
    ) -> TaskEvent:
        adapter = self.sources.source(source_id)
        return TaskEvent(
            eventId=f"evt-{uuid4().hex}",
            taskId=task.task_id,
            idempotencyKey=task.idempotency_key,
            leaseToken=lease_token,
            agentId=self.sources.config.agent.id,
            instanceId=adapter.instance_id,
            type=event_type,
            attempt=attempt,
            occurredAt=utc_now(),
            details=EventDetails(stage=stage, message=message[:1000]),
        )

    def _terminal_event(
        self,
        source_id: str,
        task: PublisherTask,
        lease_token: str,
        attempt: int,
        event_type: str,
        stage: str,
        message: str,
        error_code: str | None = None,
        *,
        retryable: bool = False,
        confirmation: Confirmation | None = None,
    ) -> TaskEvent:
        adapter = self.sources.source(source_id)
        return TaskEvent(
            eventId=f"evt-{uuid4().hex}",
            taskId=task.task_id,
            idempotencyKey=task.idempotency_key,
            leaseToken=lease_token,
            agentId=self.sources.config.agent.id,
            instanceId=adapter.instance_id,
            type=event_type,
            attempt=attempt,
            occurredAt=utc_now(),
            details=EventDetails(stage=stage, message=message[:1000]),
            result=EventResult(
                confirmation=confirmation,
                error=(
                    EventError(
                        code=error_code,
                        stage=stage,
                        retryable=retryable,
                        message=message[:1000],
                    )
                    if error_code
                    else None
                ),
                evidence=[],
            ),
        )

    def _is_after_final_click(self, source_id: str, task: PublisherTask) -> bool:
        record = self.ledger.task_record(source_id, task.task_id, task.idempotency_key)
        return bool(record and record.get("final_click_intent_at"))

    def timings(self) -> dict[str, float]:
        return dict(self.last_stage_durations_ms)

    def _record_duration(self, stage: str, started: float) -> None:
        self.last_stage_durations_ms[stage] = round(
            (time.perf_counter() - started) * 1000,
            1,
        )

    @staticmethod
    def _preflight_error(snapshot) -> tuple[str, str] | None:
        if not snapshot.interactive_session or not snapshot.desktop_unlocked:
            return "DESKTOP_LOCKED", "Windows desktop is not interactive or is locked."
        if not snapshot.running:
            return "WECHAT_NOT_RUNNING", "WeChat is not running."
        if not snapshot.logged_in:
            return "WECHAT_NOT_LOGGED_IN", "WeChat is not logged in."
        if not snapshot.moments_window_ready:
            return "MOMENTS_WINDOW_NOT_READY", "The Moments window is not ready."
        return None
