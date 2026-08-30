from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from .config import SourceConfig


@dataclass
class _FairState:
    virtual_finish: float = 0.0
    last_served_at: datetime | None = None


class WeightedFairScheduler:
    """Weighted fair queueing across independent content sources."""

    def __init__(self) -> None:
        self._states: dict[str, _FairState] = {}

    def candidates(self, sources: list[SourceConfig]) -> list[SourceConfig]:
        for source in sources:
            self._states.setdefault(source.id, _FairState())
        return sorted(sources, key=self._sort_key)

    def mark_served(self, source: SourceConfig) -> None:
        state = self._states.setdefault(source.id, _FairState())
        state.virtual_finish += 1.0 / source.weight
        state.last_served_at = datetime.now(timezone.utc)

        # Normalization keeps long-running floating point values bounded.
        if state.virtual_finish > 10_000:
            floor = min(item.virtual_finish for item in self._states.values())
            for item in self._states.values():
                item.virtual_finish -= floor

    def remove_missing(self, source_ids: set[str]) -> None:
        for source_id in set(self._states) - source_ids:
            self._states.pop(source_id, None)

    def _sort_key(self, source: SourceConfig):
        state = self._states[source.id]
        last_served = state.last_served_at or datetime.min.replace(tzinfo=timezone.utc)
        return state.virtual_finish, last_served, source.id
