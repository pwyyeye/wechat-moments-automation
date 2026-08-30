from __future__ import annotations

import logging

from .ledger import AgentLedger
from .source_manager import SourceManager
from .sources.base import SourceError

logger = logging.getLogger(__name__)


class OutboxDispatcher:
    """Deliver durable events without ever re-running the desktop publisher."""

    def __init__(self, ledger: AgentLedger, sources: SourceManager) -> None:
        self.ledger = ledger
        self.sources = sources

    def flush(self, limit: int = 20) -> int:
        delivered = 0
        for pending in self.ledger.pending_outbox(limit):
            try:
                self.sources.send_event(pending.source_id, pending.event)
            except (SourceError, KeyError) as error:
                delay = min(300, 2 ** min(pending.attempt_count + 2, 8))
                self.ledger.mark_outbox_failed(pending.event_id, str(error), delay)
                logger.warning(
                    "outbox delivery failed sourceId=%s taskId=%s eventId=%s",
                    pending.source_id,
                    pending.task_id,
                    pending.event_id,
                )
                continue
            self.ledger.mark_outbox_delivered(pending.event_id)
            delivered += 1
        return delivered
