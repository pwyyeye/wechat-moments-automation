import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.agent.credential_store import IdentityPayloadProtector
from src.agent.executor import ExecutorResult
from src.agent.ledger import AgentLedger
from src.agent.models import AgentSnapshot, ClaimResponse, EventDetails, TaskEvent
from src.agent.outbox import OutboxDispatcher
from src.agent.sources.base import SourceError
from src.agent.worker import PublisherWorker


ROOT = Path(__file__).resolve().parents[1]


def make_claim():
    task = json.loads(
        (ROOT / "contracts/publisher-agent/v1/fixtures/valid-task.json").read_text(
            encoding="utf-8"
        )
    )
    return ClaimResponse.model_validate(
        {
            "lease": {
                "token": "lease-token-0000000001",
                "expiresAt": (datetime.now(timezone.utc) + timedelta(minutes=3)).isoformat(),
                "renewAfterSeconds": 300,
            },
            "task": task,
            "attempt": 1,
            "serverTime": datetime.now(timezone.utc).isoformat(),
            "requestId": "request-1",
        }
    )


class FakeAdapter:
    instance_id = "instance-test"


class FakeSources:
    def __init__(self, claim, send_failures=0):
        self.claim = claim
        self.send_failures = send_failures
        self.sent = []
        self.config = type("Config", (), {"agent": type("Agent", (), {"id": "agent-test"})()})()
        self.source_config = type("Source", (), {"id": "source-a"})()

    def claim_next(self):
        if self.claim is None:
            return None
        result = ("source-a", self.claim)
        self.claim = None
        return result

    def source(self, source_id):
        return FakeAdapter()

    def config_for(self, source_id):
        return self.source_config

    def renew_lease(self, source_id, task_id, lease):
        return lease

    def send_event(self, source_id, event):
        if self.send_failures:
            self.send_failures -= 1
            raise SourceError("SOURCE_UNREACHABLE", "network down", retryable=True)
        self.sent.append(event)


class FakeMediaCache:
    def download_task(self, source, task):
        return ["C:/cache/test.jpg"]


class FakeExecutor:
    def __init__(self, *, fail_after_click=False):
        self.calls = 0
        self.fail_after_click = fail_after_click

    def snapshot(self):
        return self.preflight(None)

    def preflight(self, task):
        return AgentSnapshot(
            running=True,
            loggedIn=True,
            momentsWindowReady=True,
            wechatVersion="4.1.13.12",
            interactiveSession=True,
            desktopUnlocked=True,
        )

    def publish(self, task, media_paths, before_final_click, after_final_click):
        self.calls += 1
        before_final_click()
        after_final_click()
        if self.fail_after_click:
            raise RuntimeError("confirmation crashed")
        return ExecutorResult(published=True, final_click_intent=True)

    def close(self):
        pass


def make_worker(tmp_path, sources, executor):
    ledger = AgentLedger(tmp_path / "agent.db", IdentityPayloadProtector())
    outbox = OutboxDispatcher(ledger, sources)
    worker = PublisherWorker(ledger, sources, outbox, FakeMediaCache(), executor)
    return ledger, worker


def test_success_path_emits_ordered_durable_events(tmp_path):
    sources = FakeSources(make_claim())
    executor = FakeExecutor()
    ledger, worker = make_worker(tmp_path, sources, executor)

    assert worker.run_once() is True
    assert executor.calls == 1
    assert ledger.recent_tasks()[0]["state"] == "succeeded"
    assert [event.type for event in sources.sent] == [
        "accepted",
        "preflight_started",
        "publish_started",
        "final_click_intent",
        "confirmation_started",
        "succeeded",
    ]


def test_exception_after_click_becomes_uncertain_and_never_retries(tmp_path):
    sources = FakeSources(make_claim())
    executor = FakeExecutor(fail_after_click=True)
    ledger, worker = make_worker(tmp_path, sources, executor)

    worker.run_once()
    worker.run_once()
    assert executor.calls == 1
    assert ledger.recent_tasks()[0]["state"] == "uncertain"
    assert sources.sent[-1].type == "uncertain"


def test_outbox_retry_does_not_repeat_desktop_execution(tmp_path):
    sources = FakeSources(make_claim(), send_failures=6)
    executor = FakeExecutor()
    ledger, worker = make_worker(tmp_path, sources, executor)

    worker.run_once()
    assert executor.calls == 1
    assert ledger.outbox_backlog() > 0
    worker.run_once()
    assert executor.calls == 1


def test_restart_after_final_intent_reports_uncertain_without_click(tmp_path):
    claim = make_claim()
    sources = FakeSources(None)
    executor = FakeExecutor()
    ledger, worker = make_worker(tmp_path, sources, executor)
    assert ledger.record_claim("source-a", claim)
    intent = TaskEvent(
        eventId="evt-intent",
        taskId=claim.task.task_id,
        idempotencyKey=claim.task.idempotency_key,
        leaseToken=claim.lease.token,
        agentId="agent-test",
        instanceId="instance-test",
        type="final_click_intent",
        attempt=1,
        occurredAt=datetime.now(timezone.utc),
        details=EventDetails(stage="before_final_click", message="persisted"),
    )
    ledger.record_final_click_intent("source-a", intent)

    worker.run_once()
    assert executor.calls == 0
    assert ledger.recent_tasks()[0]["state"] == "uncertain"
