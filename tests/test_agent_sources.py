from pathlib import Path

from src.agent.config import AgentConfig, SourceConfig
from src.agent.credential_store import IdentityPayloadProtector, InMemoryCredentialStore
from src.agent.ledger import AgentLedger
from src.agent.scheduler import WeightedFairScheduler
from src.agent.source_manager import SourceManager
from src.agent.sources.base import SourceError, SourceMeta


def source(source_id, weight=1):
    return SourceConfig.model_validate(
        {
            "id": source_id,
            "name": source_id,
            "baseUrl": "https://content.example.test/openapi/publisher-agent/v1",
            "weight": weight,
            "accountKey": "wechat-main",
            "auth": {
                "type": "api_key_header",
                "headerName": "x-api-key",
                "credentialRef": f"dpapi://{source_id}",
            },
        }
    )


def test_weighted_scheduler_serves_three_to_one_without_starvation():
    high = source("a", 3)
    low = source("b", 1)
    scheduler = WeightedFairScheduler()
    selected = []
    for _ in range(40):
        candidate = scheduler.candidates([high, low])[0]
        selected.append(candidate.id)
        scheduler.mark_served(candidate)

    assert selected.count("a") == 30
    assert selected.count("b") == 10
    assert max(
        len(run) for run in "".join(selected).split("b") if run
    ) <= 3


class FakeSource:
    instances = {}

    def __init__(self, config, agent, credential_store, instance_id=None):
        self.config = config
        self.source_id = config.id
        self.instance_id = instance_id
        self.fail = config.id == "bad"
        self.closed = False
        self.instances[config.id] = self

    def test_connection(self):
        if self.fail:
            raise SourceError("SOURCE_UNREACHABLE", "offline", retryable=True)
        return SourceMeta("wechat-moments-publisher-source", ["1.0"], self.source_id, "now")

    def heartbeat(self, snapshot):
        if self.fail:
            raise SourceError("SOURCE_UNREACHABLE", "offline", retryable=True)

    def claim(self):
        if self.fail:
            raise SourceError("SOURCE_UNREACHABLE", "offline", retryable=True)
        return None

    def renew_lease(self, task_id, lease):
        return lease

    def send_event(self, event):
        return None

    def close(self):
        self.closed = True


def test_source_failure_isolated_and_shared_instance_id(tmp_path):
    config = AgentConfig(sources=[source("bad"), source("good")])
    ledger = AgentLedger(tmp_path / "agent.db", IdentityPayloadProtector())
    credentials = InMemoryCredentialStore()
    credentials.set("dpapi://bad", "bad-secret")
    credentials.set("dpapi://good", "good-secret")
    manager = SourceManager(
        config,
        credentials,
        ledger,
        source_factory=FakeSource,
    )

    assert manager.claim_next() is None
    states = {item["id"]: item for item in manager.status()}
    assert states["bad"]["healthState"] == "degraded"
    assert states["good"]["healthState"] == "healthy"
    assert states["bad"]["requestCount"] == 1
    assert states["bad"]["errorCount"] == 1
    assert states["bad"]["lastOperation"] == "claim"
    assert states["good"]["requestCount"] == 1
    assert states["good"]["lastLatencyMs"] is not None
    assert FakeSource.instances["bad"].instance_id == FakeSource.instances["good"].instance_id
