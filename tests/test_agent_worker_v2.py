from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from src.agent.config import AgentConfig
from src.agent.credential_store import IdentityPayloadProtector
from src.agent.ledger_v2 import AgentV2Ledger
from src.agent.models import AgentSnapshot
from src.agent.models_v2 import (
    PublisherV2Account,
    PublisherV2Capability,
    PublisherV2ClaimResponse,
    PublisherV2EventDetails,
    PublisherV2Executor,
    PublisherV2TaskEvent,
)
from src.agent.outbox_v2 import V2OutboxDispatcher
from src.agent.worker_v2 import PublisherV2Worker


def make_claim():
    now = datetime.now(timezone.utc)
    return PublisherV2ClaimResponse.model_validate(
        {
            "lease": {
                "token": "lease-token-v2-worker",
                "expiresAt": (now + timedelta(minutes=3)).isoformat(),
                "renewAfterSeconds": 30,
            },
            "task": {
                "specVersion": "content-publisher/task-v2",
                "taskId": "task-v2-worker",
                "idempotencyKey": "idempotency-v2-worker",
                "revision": 1,
                "createdAt": now.isoformat(),
                "priority": 50,
                "route": {
                    "providerKey": "wechatsync",
                    "operation": "create_draft",
                    "platform": "zhihu",
                    "accountStableId": "zhihu-user-1",
                    "profileId": "profile-a",
                    "executorInstanceId": "wechatsync:profile-a",
                },
                "content": {"title": "文章", "markdown": "正文", "media": []},
                "options": {"draftOnly": True},
                "schedule": {
                    "notBefore": now.isoformat(),
                    "expiresAt": None,
                    "timezone": "Asia/Shanghai",
                    "misfirePolicy": "manual",
                },
                "policy": {"maxPreActionAttempts": 2, "completionStrategy": "sync"},
                "extensions": {},
            },
            "attempt": 1,
            "serverTime": now.isoformat(),
            "requestId": "request-worker",
        }
    )


class FakeSources:
    def __init__(self, claim):
        self.config = AgentConfig()
        self.claim = claim
        self.events = []

    def claim_next_v2(self, executors, accounts):
        if self.claim is None:
            return None
        claim, self.claim = self.claim, None
        return "source-v2", claim

    def send_event_v2(self, source_id, event):
        self.events.append((source_id, event))

    def source(self, source_id):
        return SimpleNamespace(instance_id="instance-worker")

    def config_for(self, source_id):
        return SimpleNamespace(id=source_id)

    def renew_lease_v2(self, source_id, task_id, lease, executor_instance_id):
        return lease


class FakeDesktopExecutor:
    def snapshot(self):
        return AgentSnapshot(
            wechatVersion="4.1.13.12",
            agentVersion="0.5.0",
            running=True,
            loggedIn=True,
            accountKey="wechat-main",
            interactiveSession=True,
            desktopUnlocked=True,
            momentsWindowReady=True,
            publishCapable=True,
        )


class FakeDraftConnector:
    def __init__(self):
        self.calls = 0

    def refresh_accounts(self, force=False):
        return []

    def create_draft(self, task, media_paths):
        self.calls += 1
        return {
            "syncId": "sync-worker",
            "postId": "draft-worker",
            "postUrl": "https://example.test/drafts/worker",
            "draftOnly": True,
        }


class FakeConnectors:
    def __init__(self, connector):
        self.connector = connector

    def runtime(self, snapshot):
        capability = PublisherV2Capability(
            platform="zhihu",
            operations=["create_draft"],
            contentTypes=["article"],
        )
        executor = PublisherV2Executor(
            executorInstanceId="wechatsync:profile-a",
            providerKey="wechatsync",
            executionMode="browser_bridge",
            profileId="profile-a",
            connectorVersion="0.5.0",
            status="ready",
            capabilities=[capability],
        )
        account = PublisherV2Account(
            executorInstanceId="wechatsync:profile-a",
            platform="zhihu",
            accountStableId="zhihu-user-1",
            nickname="知乎用户",
            profileId="profile-a",
            authState="authenticated",
            status="ready",
        )
        return [executor], [account]

    def connector_for_executor(self, executor_instance_id):
        assert executor_instance_id == "wechatsync:profile-a"
        return self.connector


class FakeMediaCache:
    def download_task(self, source, task):
        return []


def make_worker(tmp_path, sources, connector):
    ledger = AgentV2Ledger(tmp_path / "agent.db", IdentityPayloadProtector())
    worker = PublisherV2Worker(
        ledger,
        sources,
        V2OutboxDispatcher(ledger, sources),
        FakeMediaCache(),
        FakeConnectors(connector),
        FakeDesktopExecutor(),
    )
    return ledger, worker


def test_worker_creates_one_draft_and_reports_ordered_terminal_events(tmp_path):
    connector = FakeDraftConnector()
    sources = FakeSources(make_claim())
    ledger, worker = make_worker(tmp_path, sources, connector)

    assert worker.run_once() is True

    assert connector.calls == 1
    assert ledger.get_active_task() is None
    record = ledger.task_record("source-v2", "task-v2-worker", "idempotency-v2-worker")
    assert record["state"] == "succeeded"
    assert record["action_intent_at"] is not None
    assert [event.type for _, event in sources.events] == [
        "accepted",
        "preflight_started",
        "action_started",
        "final_action_intent",
        "completion_started",
        "draft_created",
    ]

    assert worker.run_once() is False
    assert connector.calls == 1


def test_restart_after_action_intent_marks_uncertain_without_repeating_action(tmp_path):
    claim = make_claim()
    sources = FakeSources(None)
    connector = FakeDraftConnector()
    ledger, worker = make_worker(tmp_path, sources, connector)
    accepted = PublisherV2TaskEvent(
        eventId="event-accepted-worker",
        taskId=claim.task.task_id,
        idempotencyKey=claim.task.idempotency_key,
        leaseToken=claim.lease.token,
        agentId="agent-worker",
        instanceId="instance-worker",
        executorInstanceId=claim.task.route.executor_instance_id,
        type="accepted",
        attempt=1,
        occurredAt=datetime.now(timezone.utc),
        details=PublisherV2EventDetails(stage="claim", message="accepted"),
    )
    assert ledger.record_claim("source-v2", claim, accepted) is True
    intent = accepted.model_copy(
        update={
            "event_id": "event-intent-worker",
            "type": "final_action_intent",
            "details": PublisherV2EventDetails(stage="create_draft", message="intent"),
        }
    )
    ledger.record_action_intent("source-v2", intent)

    restarted = PublisherV2Worker(
        ledger,
        sources,
        V2OutboxDispatcher(ledger, sources),
        FakeMediaCache(),
        FakeConnectors(connector),
        FakeDesktopExecutor(),
    )
    assert restarted.run_once() is True

    assert connector.calls == 0
    record = ledger.task_record("source-v2", "task-v2-worker", "idempotency-v2-worker")
    assert record["state"] == "uncertain"
    assert [event.type for _, event in sources.events][-1] == "uncertain"
