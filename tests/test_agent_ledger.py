import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.agent.credential_store import IdentityPayloadProtector
from src.agent.ledger import AgentLedger
from src.agent.models import EventDetails, TaskEvent, ClaimResponse


ROOT = Path(__file__).resolve().parents[1]


def make_claim(*, attempt=1, token="lease-token-0000000001"):
    task = json.loads(
        (ROOT / "contracts/publisher-agent/v1/fixtures/valid-task.json").read_text(
            encoding="utf-8"
        )
    )
    return ClaimResponse.model_validate(
        {
            "lease": {
                "token": token,
                "expiresAt": (datetime.now(timezone.utc) + timedelta(minutes=3)).isoformat(),
                "renewAfterSeconds": 30,
            },
            "task": task,
            "attempt": attempt,
            "serverTime": datetime.now(timezone.utc).isoformat(),
            "requestId": "request-1",
        }
    )


def make_event(claim, event_type="accepted"):
    return TaskEvent(
        eventId=f"evt-{event_type}-{claim.attempt}",
        taskId=claim.task.task_id,
        idempotencyKey=claim.task.idempotency_key,
        leaseToken=claim.lease.token,
        agentId="agent-test",
        instanceId="instance-test",
        type=event_type,
        attempt=claim.attempt,
        occurredAt=datetime.now(timezone.utc),
        details=EventDetails(stage="claim", message="test"),
    )


def test_claim_and_accepted_event_are_committed_together(tmp_path):
    ledger = AgentLedger(tmp_path / "agent.db", IdentityPayloadProtector())
    claim = make_claim()

    assert ledger.record_claim("source-a", claim, make_event(claim)) is True
    active = ledger.get_active_task()
    assert active is not None
    assert active.claim.lease.token == claim.lease.token
    assert active.attempt == 1
    assert ledger.outbox_backlog() == 1


def test_only_one_task_can_be_active(tmp_path):
    ledger = AgentLedger(tmp_path / "agent.db", IdentityPayloadProtector())
    assert ledger.record_claim("source-a", make_claim())
    other = make_claim(token="lease-token-0000000002")
    other.task.task_id = "other-task"
    other.task.idempotency_key = "other-key"

    with pytest.raises(RuntimeError, match="another task"):
        ledger.record_claim("source-b", other)


def test_pre_click_retry_must_have_a_higher_server_attempt(tmp_path):
    ledger = AgentLedger(tmp_path / "agent.db", IdentityPayloadProtector())
    first = make_claim()
    assert ledger.record_claim("source-a", first)
    ledger.set_state("source-a", first.task.task_id, "failed")

    assert ledger.record_claim("source-a", first) is False
    retry = make_claim(attempt=2, token="lease-token-0000000002")
    assert ledger.record_claim("source-a", retry) is True
    assert ledger.get_active_task().attempt == 2
