from __future__ import annotations

import logging
import threading
import webbrowser
from pathlib import Path

import uvicorn

from .admin.schemas import SourceUpsertRequest
from .admin.server import create_admin_app
from .config import AgentConfig, SourceConfig, load_config, save_config
from .credential_store import DpapiCredentialStore, DpapiPayloadProtector
from .executor import DesktopPublishExecutor, PublishExecutor
from .ledger import AgentLedger
from .media_cache import MediaCache
from .outbox import OutboxDispatcher
from .source_manager import SourceManager
from .worker import PublisherWorker

logger = logging.getLogger(__name__)


class PublisherAgentApp:
    """Composition root for the multi-source Windows publishing agent."""

    def __init__(
        self,
        config_path: Path | str | None = None,
        *,
        executor: PublishExecutor | None = None,
        credential_store=None,
        payload_protector=None,
        source_factory=None,
    ) -> None:
        self.config, self.config_path = load_config(config_path)
        self.data_root = self.config_path.parent
        self.data_root.mkdir(parents=True, exist_ok=True)
        self._configure_logging()
        self._config_lock = threading.RLock()
        self.stop_event = threading.Event()
        self.executor = executor or DesktopPublishExecutor()
        self.credential_store = credential_store or DpapiCredentialStore(
            self.data_root / "credentials"
        )
        protector = payload_protector or DpapiPayloadProtector()
        self.ledger = AgentLedger(self.data_root / "data" / "agent.db", protector)
        source_kwargs = {}
        if source_factory is not None:
            source_kwargs["source_factory"] = source_factory
        self.source_manager = SourceManager(
            self.config,
            self.credential_store,
            self.ledger,
            **source_kwargs,
        )
        self.media_cache = MediaCache(
            self.data_root / "cache" / "media",
            self.config.runtime.media_cache_max_mib,
            credential_store=self.credential_store,
        )
        self.outbox = OutboxDispatcher(self.ledger, self.source_manager)
        self.worker = PublisherWorker(
            self.ledger,
            self.source_manager,
            self.outbox,
            self.media_cache,
            self.executor,
        )
        self.admin_app = create_admin_app(self)
        self._worker_thread: threading.Thread | None = None
        self._heartbeat_thread: threading.Thread | None = None
        self._stopped = False

    def start_background(self) -> None:
        if self._worker_thread and self._worker_thread.is_alive():
            return
        self.stop_event.clear()
        self._worker_thread = threading.Thread(
            target=self._worker_loop,
            name="publisher-worker",
            daemon=True,
        )
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop,
            name="publisher-heartbeat",
            daemon=True,
        )
        self._worker_thread.start()
        self._heartbeat_thread.start()

    def run_forever(self, *, open_browser: bool = True) -> None:
        self.start_background()
        url = (
            f"http://{self.config.runtime.local_admin_host}:"
            f"{self.config.runtime.local_admin_port}"
        )
        if open_browser:
            threading.Timer(1.0, lambda: webbrowser.open(url)).start()
        logger.info("starting local admin url=%s", url)
        try:
            uvicorn.run(
                self.admin_app,
                host=self.config.runtime.local_admin_host,
                port=self.config.runtime.local_admin_port,
                log_level="info",
                access_log=False,
                # Scheduled Tasks do not provide reliable stdio handles to a
                # windowed executable. Keep all service logs in agent.log.
                log_config=None,
            )
        except Exception:
            logger.exception("local admin failed url=%s", url)
            raise
        finally:
            self.stop()

    def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        self.stop_event.set()
        for thread in (self._worker_thread, self._heartbeat_thread):
            if thread and thread is not threading.current_thread():
                thread.join(timeout=10)
        self.outbox.flush()
        self.media_cache.close()
        self.source_manager.close()
        self.executor.close()

    def status(self) -> dict:
        snapshot = self.executor.snapshot()
        active = self.ledger.get_active_task()
        sources = self.source_manager.status()
        outbox_backlog = self.ledger.outbox_backlog()
        oldest_outbox = self.ledger.oldest_outbox_age_seconds()
        alerts = []
        for source in sources:
            if source["healthState"] in {"auth_error", "incompatible"}:
                alerts.append(
                    {
                        "severity": "critical",
                        "code": source["lastErrorCode"] or "SOURCE_UNAVAILABLE",
                        "sourceId": source["id"],
                    }
                )
        if outbox_backlog and (oldest_outbox or 0) >= 300:
            alerts.append(
                {
                    "severity": "warning",
                    "code": "OUTBOX_BACKLOG_AGED",
                    "count": outbox_backlog,
                }
            )
        return {
            "agent": {
                "id": self.config.agent.id,
                "displayName": self.config.agent.display_name,
                "accountKey": self.config.agent.account_key,
                "version": "0.3.0",
            },
            "wechat": snapshot.model_dump(by_alias=True, mode="json"),
            "worker": {
                "active": self.worker.is_active,
                "task": (
                    {
                        "sourceId": active.source_id,
                        "taskId": active.claim.task.task_id,
                        "state": active.state,
                    }
                    if active
                    else None
                ),
                "lastErrorCode": self.worker.last_error_code,
                "lastErrorMessage": self.worker.last_error_message,
                "lastStageDurationsMs": self.worker.timings(),
            },
            "sources": sources,
            "outbox": {
                "backlog": outbox_backlog,
                "oldestAgeSeconds": oldest_outbox,
            },
            "mediaCache": {
                "bytes": self.media_cache.size_bytes(),
                "limitBytes": self.media_cache.max_cache_bytes,
            },
            "alerts": alerts,
        }

    def preflight(self) -> dict:
        return self.executor.preflight().model_dump(by_alias=True, mode="json")

    def update_identity(self, display_name: str, account_key: str) -> None:
        with self._config_lock:
            self.config.agent.display_name = display_name
            self.config.agent.account_key = account_key
            save_config(self.config, self.config_path)
            self.source_manager.reload(self.config)

    def upsert_source(
        self,
        body: SourceUpsertRequest,
        *,
        create_only: bool,
    ) -> None:
        with self._config_lock:
            existing_index = next(
                (i for i, source in enumerate(self.config.sources) if source.id == body.id),
                None,
            )
            if create_only and existing_index is not None:
                raise ValueError(f"source {body.id} already exists")
            if not create_only and existing_index is None:
                raise ValueError(f"source {body.id} does not exist")
            credential_ref = f"dpapi://{body.id}"
            if body.credential:
                self.credential_store.set(credential_ref, body.credential)
            elif existing_index is None:
                raise ValueError("a credential is required for a new source")

            payload = body.model_dump(by_alias=True, mode="json", exclude={"credential"})
            payload["type"] = "standard-http-v1"
            payload["auth"]["credentialRef"] = credential_ref
            source = SourceConfig.model_validate(payload)
            if existing_index is None:
                self.config.sources.append(source)
            else:
                self.config.sources[existing_index] = source
            save_config(self.config, self.config_path)
            self.source_manager.reload(self.config)

    def delete_source(self, source_id: str) -> None:
        with self._config_lock:
            active = self.ledger.get_active_task()
            if active and active.source_id == source_id:
                raise RuntimeError("cannot delete a source with an active task")
            existing = next(
                (source for source in self.config.sources if source.id == source_id),
                None,
            )
            if existing is None:
                raise KeyError(f"source {source_id} does not exist")
            self.config.sources = [
                source for source in self.config.sources if source.id != source_id
            ]
            save_config(self.config, self.config_path)
            self.source_manager.reload(self.config)
            self.credential_store.delete(existing.auth.credential_ref)

    def _worker_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                worked = self.worker.run_once()
            except Exception:
                logger.exception("worker loop failed")
                worked = False
            delay = 0.25 if worked else self.config.runtime.poll_seconds
            self.stop_event.wait(delay)

    def _heartbeat_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                self.source_manager.heartbeat_all(self.executor.snapshot())
            except Exception:
                logger.exception("heartbeat loop failed")
            self.stop_event.wait(self.config.runtime.heartbeat_seconds)

    def _configure_logging(self) -> None:
        log_root = self.data_root / "logs"
        log_root.mkdir(parents=True, exist_ok=True)
        root = logging.getLogger()
        if not any(getattr(handler, "_publisher_agent_handler", False) for handler in root.handlers):
            from logging.handlers import RotatingFileHandler

            handler = RotatingFileHandler(
                log_root / "agent.log",
                maxBytes=10 * 1024 * 1024,
                backupCount=7,
                encoding="utf-8",
            )
            handler._publisher_agent_handler = True
            handler.setFormatter(
                logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
            )
            root.addHandler(handler)
            root.setLevel(logging.INFO)
