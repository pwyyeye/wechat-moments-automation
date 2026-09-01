from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from uuid import uuid4

from .connectors import ConnectorError, ConnectorRegistry
from .ledger_v2 import ActiveV2LedgerTask, AgentV2Ledger
from .media_cache import MediaCache, MediaDownloadError
from .models import PublisherTask
from .models_v2 import (
    PublisherV2EventDetails,
    PublisherV2EventError,
    PublisherV2EventOutput,
    PublisherV2EventResult,
    PublisherV2Task,
    PublisherV2TaskEvent,
)
from .outbox_v2 import V2OutboxDispatcher
from .source_manager import SourceManager
from .sources.base import SourceError

logger = logging.getLogger(__name__)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class V2LeaseRenewer:
    def __init__(
        self,
        sources: SourceManager,
        ledger: AgentV2Ledger,
        active: ActiveV2LedgerTask,
    ) -> None:
        self.sources = sources
        self.ledger = ledger
        self.active = active
        self.lease = active.claim.lease
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, name="v2-lease-renewer", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        self.thread.join(timeout=5)

    def _run(self) -> None:
        task = self.active.claim.task
        while not self.stop_event.wait(max(5, self.lease.renew_after_seconds)):
            try:
                self.lease = self.sources.renew_lease_v2(
                    self.active.source_id,
                    task.task_id,
                    self.lease,
                    task.route.executor_instance_id or "",
                )
                self.ledger.update_lease(self.active.source_id, task.task_id, self.lease)
            except SourceError as error:
                logger.warning("v2 lease renew failed taskId=%s code=%s", task.task_id, error.code)


class PublisherV2Worker:
    """Executes generic V2 tasks one at a time across all local connectors."""

    def __init__(
        self,
        ledger: AgentV2Ledger,
        sources: SourceManager,
        outbox: V2OutboxDispatcher,
        media_cache: MediaCache,
        connectors: ConnectorRegistry,
        desktop_executor,
    ) -> None:
        self.ledger = ledger
        self.sources = sources
        self.outbox = outbox
        self.media_cache = media_cache
        self.connectors = connectors
        self.desktop_executor = desktop_executor
        self._lock = threading.Lock()
        self.last_error_code: str | None = None
        self.last_error_message: str | None = None

    @property
    def is_active(self) -> bool:
        return self._lock.locked()

    def run_once(self) -> bool:
        if not self._lock.acquire(blocking=False):
            return False
        try:
            self.outbox.flush()
            active = self.ledger.get_active_task()
            if active is not None:
                if active.action_intent_at:
                    self._finish_uncertain(
                        active,
                        "Agent restarted after action intent; the action was not repeated.",
                    )
                    self.outbox.flush()
                    return True
                if active.claim.lease.expires_at <= utc_now():
                    try:
                        renewed = self.sources.renew_lease_v2(
                            active.source_id,
                            active.claim.task.task_id,
                            active.claim.lease,
                            active.claim.task.route.executor_instance_id or "",
                        )
                    except SourceError:
                        self.ledger.set_state(active.source_id, active.claim.task.task_id, "failed")
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

            snapshot = self.desktop_executor.snapshot()
            executors, accounts = self.connectors.runtime(snapshot)
            claimed = self.sources.claim_next_v2(executors, accounts)
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
                "Task persisted in the local V2 ledger.",
            )
            if not self.ledger.record_claim(source_id, response, accepted):
                return True
            active = self.ledger.get_active_task()
            if active is None:
                raise RuntimeError("claimed V2 task disappeared from the ledger")
            self.outbox.flush()
            self._execute(active)
            self.outbox.flush()
            return True
        finally:
            self._lock.release()

    def _execute(self, active: ActiveV2LedgerTask) -> None:
        task = active.claim.task
        renewer = V2LeaseRenewer(self.sources, self.ledger, active)
        renewer.start()
        try:
            self.ledger.set_state(active.source_id, task.task_id, "executing")
            self.ledger.enqueue_event(
                active.source_id,
                self._event(
                    active.source_id,
                    task,
                    active.claim.lease.token,
                    active.attempt,
                    "preflight_started",
                    "preflight",
                    "Executor and account route validation started.",
                ),
            )
            self._refresh_and_validate_route(task)
            try:
                media_paths = self.media_cache.download_task(
                    self.sources.config_for(active.source_id),
                    task,
                )
            except MediaDownloadError as error:
                self._finish_failure(active, error.code, "download", str(error), error.retryable)
                return
            self.ledger.enqueue_event(
                active.source_id,
                self._event(
                    active.source_id,
                    task,
                    active.claim.lease.token,
                    active.attempt,
                    "action_started",
                    task.route.operation,
                    f"{task.route.provider_key} action started.",
                ),
            )
            if task.route.provider_key == "wechatsync":
                self._execute_draft(active, media_paths)
            elif task.route.provider_key == "windows_moments":
                self._execute_windows_publish(active, media_paths)
            else:
                self._finish_failure(
                    active,
                    "CAPABILITY_MISMATCH",
                    "preflight",
                    f"No local connector for {task.route.provider_key}.",
                    False,
                )
        except ConnectorError as error:
            if self._has_action_intent(active):
                self._finish_uncertain(active, f"{error.code}: {error}")
            else:
                self._finish_failure(active, error.code, "preflight", str(error), error.retryable)
        except Exception as error:
            logger.exception("V2 task execution failed taskId=%s", task.task_id)
            if self._has_action_intent(active):
                self._finish_uncertain(active, str(error))
            else:
                self._finish_failure(active, "CONNECTOR_UNAVAILABLE", "preflight", str(error), True)
        finally:
            renewer.stop()

    def _execute_draft(self, active: ActiveV2LedgerTask, media_paths: list[str]) -> None:
        task = active.claim.task
        connector = self.connectors.connector_for_executor(task.route.executor_instance_id or "")
        intent = self._event(
            active.source_id,
            task,
            active.claim.lease.token,
            active.attempt,
            "final_action_intent",
            "create_draft",
            "Draft creation intent committed locally; automatic retry is disabled.",
        )
        self.ledger.record_action_intent(active.source_id, intent)
        output = connector.create_draft(task, media_paths)
        self.ledger.set_state(active.source_id, task.task_id, "completing")
        self.ledger.enqueue_event(
            active.source_id,
            self._event(
                active.source_id,
                task,
                active.claim.lease.token,
                active.attempt,
                "completion_started",
                "create_draft",
                "WechatSync returned a platform result.",
            ),
        )
        event = self._terminal_event(
            active,
            "draft_created",
            "create_draft",
            f"{task.route.platform} draft created.",
            output=PublisherV2EventOutput.model_validate(output),
        )
        self.ledger.finish_task(active.source_id, task.task_id, "succeeded", event)

    def _execute_windows_publish(self, active: ActiveV2LedgerTask, media_paths: list[str]) -> None:
        task = active.claim.task
        desktop_task = self._desktop_task(task)
        snapshot = self.desktop_executor.preflight(desktop_task)
        preflight_error = self._desktop_preflight_error(snapshot)
        if preflight_error:
            code, message = preflight_error
            self._finish_failure(active, code, "preflight", message, True)
            return

        def before_final_click() -> None:
            self.ledger.record_action_intent(
                active.source_id,
                self._event(
                    active.source_id,
                    task,
                    active.claim.lease.token,
                    active.attempt,
                    "final_action_intent",
                    "before_final_click",
                    "Final-click intent committed locally; automatic retry is disabled.",
                ),
            )

        def after_final_click() -> None:
            self.ledger.set_state(active.source_id, task.task_id, "completing")
            self.ledger.enqueue_event(
                active.source_id,
                self._event(
                    active.source_id,
                    task,
                    active.claim.lease.token,
                    active.attempt,
                    "completion_started",
                    "confirmation",
                    "Final click completed; confirmation started.",
                ),
            )

        result = self.desktop_executor.publish(
            desktop_task,
            media_paths,
            before_final_click,
            after_final_click,
        )
        if result.published:
            event = self._terminal_event(
                active,
                "published",
                "confirmation",
                "Expected Moments post was confirmed.",
                output=PublisherV2EventOutput(draftOnly=False),
            )
            self.ledger.finish_task(active.source_id, task.task_id, "succeeded", event)
        elif self._has_action_intent(active):
            self._finish_uncertain(active, result.error_message or "Publish result is unconfirmed.")
        else:
            self._finish_failure(
                active,
                "CONTENT_INPUT_FAILED",
                "before_final_click",
                result.error_message or "Desktop publisher failed before final click.",
                True,
            )

    def _refresh_and_validate_route(self, task: PublisherV2Task) -> None:
        if task.route.provider_key == "wechatsync":
            connector = self.connectors.connector_for_executor(task.route.executor_instance_id or "")
            connector.refresh_accounts(force=True)
        executors, accounts = self.connectors.runtime(self.desktop_executor.snapshot())
        executor = next(
            (
                item
                for item in executors
                if item.executor_instance_id == task.route.executor_instance_id
            ),
            None,
        )
        if executor is None or executor.status != "ready":
            raise ConnectorError("CONNECTOR_UNAVAILABLE", "Target executor is not ready.", retryable=True)
        if executor.provider_key != task.route.provider_key:
            raise ConnectorError("CAPABILITY_MISMATCH", "Provider key does not match.", retryable=False)
        if task.route.profile_id and executor.profile_id != task.route.profile_id:
            raise ConnectorError("ACCOUNT_MISMATCH", "Chrome profile does not match.", retryable=False)
        capability = next(
            (
                item
                for item in executor.capabilities
                if item.platform == task.route.platform and task.route.operation in item.operations
            ),
            None,
        )
        if capability is None:
            raise ConnectorError("CAPABILITY_MISMATCH", "Operation is not supported.", retryable=False)
        account = next(
            (
                item
                for item in accounts
                if item.executor_instance_id == executor.executor_instance_id
                and item.platform == task.route.platform
                and item.account_stable_id == task.route.account_stable_id
                and item.auth_state == "authenticated"
                and item.status == "ready"
            ),
            None,
        )
        if account is None:
            raise ConnectorError("ACCOUNT_MISMATCH", "Authenticated account changed.", retryable=False)

    def _event(
        self,
        source_id: str,
        task: PublisherV2Task,
        lease_token: str,
        attempt: int,
        event_type: str,
        stage: str,
        message: str,
        *,
        result: PublisherV2EventResult | None = None,
    ) -> PublisherV2TaskEvent:
        return PublisherV2TaskEvent(
            eventId=f"evt-{uuid4().hex}",
            taskId=task.task_id,
            idempotencyKey=task.idempotency_key,
            leaseToken=lease_token,
            agentId=self.sources.config.agent.id,
            instanceId=self.sources.source(source_id).instance_id,
            executorInstanceId=task.route.executor_instance_id or "",
            type=event_type,
            attempt=attempt,
            occurredAt=utc_now(),
            details=PublisherV2EventDetails(stage=stage, message=message[:1000]),
            result=result,
        )

    def _terminal_event(
        self,
        active: ActiveV2LedgerTask,
        event_type: str,
        stage: str,
        message: str,
        *,
        output: PublisherV2EventOutput | None = None,
        error: PublisherV2EventError | None = None,
    ) -> PublisherV2TaskEvent:
        return self._event(
            active.source_id,
            active.claim.task,
            active.claim.lease.token,
            active.attempt,
            event_type,
            stage,
            message,
            result=PublisherV2EventResult(output=output, error=error, evidence=[]),
        )

    def _finish_failure(
        self,
        active: ActiveV2LedgerTask,
        code: str,
        stage: str,
        message: str,
        retryable: bool,
    ) -> None:
        error = PublisherV2EventError(
            code=code,
            stage=stage,
            retryable=retryable,
            message=message[:1000],
        )
        event = self._terminal_event(active, "failed", stage, message, error=error)
        self.ledger.finish_task(active.source_id, active.claim.task.task_id, "failed", event)
        self.last_error_code = code
        self.last_error_message = message

    def _finish_uncertain(self, active: ActiveV2LedgerTask, message: str) -> None:
        error = PublisherV2EventError(
            code="POST_ACTION_UNCONFIRMED",
            stage="completion",
            retryable=False,
            message=message[:1000],
        )
        event = self._terminal_event(
            active,
            "uncertain",
            "completion",
            message,
            error=error,
        )
        self.ledger.finish_task(active.source_id, active.claim.task.task_id, "uncertain", event)
        self.last_error_code = error.code
        self.last_error_message = message

    def _has_action_intent(self, active: ActiveV2LedgerTask) -> bool:
        record = self.ledger.task_record(
            active.source_id,
            active.claim.task.task_id,
            active.claim.task.idempotency_key,
        )
        return bool(record and record.get("action_intent_at"))

    @staticmethod
    def _desktop_task(task: PublisherV2Task) -> PublisherTask:
        return PublisherTask.model_validate(
            {
                "specVersion": "wechat-moments-publisher/task-v1",
                "taskId": task.task_id,
                "idempotencyKey": task.idempotency_key,
                "revision": task.revision,
                "createdAt": task.created_at,
                "priority": task.priority,
                "schedule": task.schedule.model_dump(by_alias=True),
                "target": {
                    "platform": "wechat_moments",
                    "accountKey": task.route.account_key or task.route.account_stable_id,
                    "visibility": {"type": "public"},
                },
                "content": {
                    "text": task.content.text or task.content.markdown or "",
                    "media": [item.model_dump(by_alias=True) for item in task.content.media],
                },
                "policy": {
                    "maxPreClickAttempts": min(2, task.policy.max_pre_action_attempts),
                    "requirePostPublishConfirmation": True,
                },
                "extensions": task.extensions,
            }
        )

    @staticmethod
    def _desktop_preflight_error(snapshot) -> tuple[str, str] | None:
        if not snapshot.interactive_session or not snapshot.desktop_unlocked:
            return "DESKTOP_LOCKED", "Windows desktop is not interactive or is locked."
        if not snapshot.running:
            return "WECHAT_NOT_RUNNING", "WeChat is not running."
        if not snapshot.logged_in:
            return "WECHAT_NOT_LOGGED_IN", "WeChat is not logged in."
        if not snapshot.moments_window_ready:
            return "MOMENTS_WINDOW_NOT_READY", "The Moments window is not ready."
        return None
