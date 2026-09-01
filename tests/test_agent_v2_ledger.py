from datetime import datetime, timedelta, timezone

from src.agent.credential_store import IdentityPayloadProtector
from src.agent.ledger_v2 import AgentV2Ledger
from src.agent.models_v2 import (
    PublisherV2ClaimResponse,
    PublisherV2EventDetails,
    PublisherV2TaskEvent,
)


def claim():
    now = datetime.now(timezone.utc)
    return PublisherV2ClaimResponse.model_validate(
        {
            "lease": {
                "token": "lease-token-v2-0001",
                "expiresAt": (now + timedelta(minutes=3)).isoformat(),
                "renewAfterSeconds": 30,
            },
            "task": {
                "specVersion": "content-publisher/task-v2",
                "taskId": "task-v2-ledger",
                "idempotencyKey": "idempotency-v2-ledger",
                "revision": 1,
                "createdAt": now.isoformat(),
                "priority": 50,
                "route": {
                    "providerKey": "wechatsync",
                    "operation": "create_draft",
                    "platform": "zhihu",
                    "accountStableId": "user-1",
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
            "requestId": "request-1",
        }
    )


def event(target, event_type):
    return PublisherV2TaskEvent(
        eventId=f"event-{event_type}",
        taskId=target.task.task_id,
        idempotencyKey=target.task.idempotency_key,
        leaseToken=target.lease.token,
        agentId="agent-1",
        instanceId="instance-1",
        executorInstanceId=target.task.route.executor_instance_id,
        type=event_type,
        attempt=1,
        occurredAt=datetime.now(timezone.utc),
        details=PublisherV2EventDetails(stage="test", message="test"),
    )


def test_v2_action_intent_survives_restart_and_blocks_duplicate_claim(tmp_path):
    path = tmp_path / "agent.db"
    first = AgentV2Ledger(path, IdentityPayloadProtector())
    target = claim()

    assert first.record_claim("source-v2", target, event(target, "accepted")) is True
    first.record_action_intent("source-v2", event(target, "final_action_intent"))

    restarted = AgentV2Ledger(path, IdentityPayloadProtector())
    active = restarted.get_active_task()
    assert active is not None
    assert active.state == "final_action_intent"
    assert active.action_intent_at is not None
    assert restarted.record_claim("source-v2", target, event(target, "accepted")) is False
