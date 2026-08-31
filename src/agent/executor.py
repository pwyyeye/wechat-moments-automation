from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Callable, Protocol

from .environment import prepare_moments_window, probe_environment
from .models import AgentSnapshot, PublisherTask


@dataclass(frozen=True)
class ExecutorResult:
    published: bool
    final_click_intent: bool
    error_message: str = ""


class PublishExecutor(Protocol):
    def snapshot(self) -> AgentSnapshot: ...

    def preflight(self, task: PublisherTask | None = None) -> AgentSnapshot: ...

    def publish(
        self,
        task: PublisherTask,
        media_paths: list[str],
        before_final_click: Callable[[], None],
        after_final_click: Callable[[], None],
    ) -> ExecutorResult: ...

    def close(self) -> None: ...


class DesktopPublishExecutor:
    """Adapter from protocol jobs to the existing WeChat desktop publisher."""

    def __init__(self, publisher_factory=None) -> None:
        self.publisher_factory = publisher_factory
        self._publisher = None
        self._lock = threading.RLock()

    def snapshot(self) -> AgentSnapshot:
        return probe_environment()

    def preflight(self, task: PublisherTask | None = None) -> AgentSnapshot:
        snapshot = self.snapshot()
        if not snapshot.interactive_session or not snapshot.desktop_unlocked:
            return snapshot
        with self._lock:
            moments_ready = snapshot.logged_in and prepare_moments_window()
            refreshed = self.snapshot()
            return refreshed.model_copy(
                update={
                    "logged_in": snapshot.logged_in,
                    "moments_window_ready": moments_ready,
                }
            )

    def publish(
        self,
        task: PublisherTask,
        media_paths: list[str],
        before_final_click: Callable[[], None],
        after_final_click: Callable[[], None],
    ) -> ExecutorResult:
        from src.core.publisher import PublishTask

        with self._lock:
            result = self._get_publisher().publish(
                PublishTask(
                    text=task.content.text,
                    images=media_paths,
                    confirm_publish=True,
                    before_final_click=before_final_click,
                    after_final_click=after_final_click,
                )
            )
        return ExecutorResult(
            published=result.published,
            final_click_intent=result.final_click_intent,
            error_message=result.error_message,
        )

    def close(self) -> None:
        with self._lock:
            if self._publisher is not None:
                self._publisher.shutdown()
                self._publisher = None

    def _get_publisher(self):
        if self._publisher is None:
            if self.publisher_factory is None:
                from src.core.publisher import EventDrivenPublisher

                self.publisher_factory = EventDrivenPublisher
            publisher = self.publisher_factory()
            if not publisher.initialize():
                raise RuntimeError("publisher initialization failed")
            self._publisher = publisher
        return self._publisher
