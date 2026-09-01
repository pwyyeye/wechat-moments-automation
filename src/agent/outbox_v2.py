from __future__ import annotations

import logging

from .ledger_v2 import AgentV2Ledger
from .source_manager import SourceManager
from .sources.base import SourceError

logger = logging.getLogger(__name__)


class V2OutboxDispatcher:
    def __init__(self, ledger: AgentV2Ledger, sources: SourceManager) -> None:
        self.ledger = ledger
        self.sources = sources

    def flush(self, limit: int = 20) -> int:
        delivered = 0
        for item in self.ledger.pending_outbox(limit):
            try:
                self.sources.send_event_v2(item.source_id, item.event)
            except SourceError as error:
                delay = min(300, 2 ** min(item.attempt_count + 1, 8))
                self.ledger.mark_outbox_failed(item.event_id, str(error), delay)
                logger.warning(
                    "v2 outbox delivery failed eventId=%s code=%s",
                    item.event_id,
                    error.code,
                )
                continue
            self.ledger.mark_outbox_delivered(item.event_id)
            delivered += 1
        return delivered
