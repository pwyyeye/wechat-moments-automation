from __future__ import annotations

import logging
import os
import secrets
import threading
from datetime import timezone
from pathlib import Path

import uvicorn

from .admin.schemas import (
    LocalScheduleCreateRequest,
    LocalScheduleUpdateRequest,
    SourceUpsertRequest,
)
from .admin.server import create_admin_app
from .config import SourceConfig, WechatSyncProfileConfig, load_config, save_config
from .connectors import ConnectorRegistry
from .credential_store import DpapiCredentialStore, DpapiPayloadProtector
from .executor import DesktopPublishExecutor, PublishExecutor
from .ledger import AgentLedger
from .ledger_v2 import AgentV2Ledger
from .local_schedule import LocalScheduleStore, LocalScheduleWorker, utc_now
from .media_cache import MediaCache
from .outbox import OutboxDispatcher
from .outbox_v2 import V2OutboxDispatcher
from .source_manager import SourceManager
from .version import AGENT_VERSION
from .worker import PublisherWorker
from .worker_v2 import PublisherV2Worker

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
        self._preflight_lock = threading.Lock()
        self._shutdown_lock = threading.Lock()
        self.stop_event = threading.Event()
        self.executor = executor or DesktopPublishExecutor()
        self.credential_store = credential_store or DpapiCredentialStore(
            self.data_root / "credentials"
        )
        self._import_bootstrap_if_present()
        self._ensure_connector_credentials()
        protector = payload_protector or DpapiPayloadProtector()
        self.ledger = AgentLedger(self.data_root / "data" / "agent.db", protector)
        self.v2_ledger = AgentV2Ledger(self.data_root / "data" / "agent.db", protector)
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
        self.v2_outbox = V2OutboxDispatcher(self.v2_ledger, self.source_manager)
        self.connector_registry = ConnectorRegistry(self.config, self.credential_store)
        self.worker = PublisherWorker(
            self.ledger,
            self.source_manager,
            self.outbox,
            self.media_cache,
            self.executor,
        )
        self.v2_worker = PublisherV2Worker(
            self.v2_ledger,
            self.source_manager,
            self.v2_outbox,
            self.media_cache,
            self.connector_registry,
            self.executor,
        )
        self.local_schedule_store = LocalScheduleStore(
            self.data_root / "data" / "agent.db",
            self.data_root / "data" / "local-media",
        )
        self.local_schedule_worker = LocalScheduleWorker(
            self.local_schedule_store,
            self.executor,
            self.worker.exclusive_desktop_action,
        )
        self.admin_app = create_admin_app(self)
        self._worker_thread: threading.Thread | None = None
        self._heartbeat_thread: threading.Thread | None = None
        self._shutdown_watchdog_thread: threading.Thread | None = None
        self._server: uvicorn.Server | None = None
        self._server_stopped = threading.Event()
        self._shutdown_grace_seconds = 8.0
        self._force_exit = os._exit
        self._stopped = False

    def _import_bootstrap_if_present(self) -> None:
        from .bootstrap import import_bootstrap

        bootstrap_path = self.data_root / "bootstrap.json"
        try:
            imported = import_bootstrap(
                bootstrap_path,
                config=self.config,
                config_path=self.config_path,
                credential_store=self.credential_store,
            )
        except Exception as error:
            # A damaged deployment bundle must not take down the loopback admin
            # page; keep it for diagnosis and expose the source as unconfigured.
            # Do not log the validation exception: Pydantic may include the
            # bootstrap input, including its plaintext credential.
            logger.error(
                "source bootstrap import failed errorType=%s",
                error.__class__.__name__,
            )
            return
        if imported:
            logger.info("source bootstrap imported and plaintext file removed")

    def start_background(self) -> None:
        if self._worker_thread and self._worker_thread.is_alive():
            return
        self.stop_event.clear()
        self.connector_registry.start()
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

    def run_forever(self) -> None:
        self.start_background()
        self._server_stopped.clear()
        url = (
            f"http://{self.config.runtime.local_admin_host}:"
            f"{self.config.runtime.local_admin_port}"
        )
        logger.info("starting local admin url=%s", url)
        try:
            config = uvicorn.Config(
                self.admin_app,
                host=self.config.runtime.local_admin_host,
                port=self.config.runtime.local_admin_port,
                log_level="info",
                access_log=False,
                # Scheduled Tasks do not provide reliable stdio handles to a
                # windowed executable. Keep all service logs in agent.log.
                log_config=None,
            )
            self._server = uvicorn.Server(config)
            self._server.run()
        except Exception:
            logger.exception("local admin failed url=%s", url)
            raise
        finally:
            self._server = None
            try:
                self.stop()
            finally:
                self._server_stopped.set()

    def request_shutdown(self) -> None:
        """Stop accepting work and ask the local HTTP server to exit."""
        if (
            self.ledger.get_active_task() is not None
            or self.v2_ledger.get_active_task() is not None
            or self.local_schedule_store.active() is not None
        ):
            raise RuntimeError("当前有发布任务正在执行，任务结束后再安全退出")
        with self._shutdown_lock:
            if self.stop_event.is_set():
                return
            logger.info("safe shutdown requested from local admin")
            self.stop_event.set()
            if self._server is not None:
                self._server.should_exit = True
                self._start_shutdown_watchdog()

    def _start_shutdown_watchdog(self) -> None:
        """Guarantee exit if an OCR/UIA request prevents Uvicorn from draining."""
        if self._shutdown_watchdog_thread is not None:
            return

        def watchdog() -> None:
            if self._server_stopped.wait(self._shutdown_grace_seconds):
                return
            logger.warning(
                "graceful shutdown exceeded %.1fs; forcing process exit",
                self._shutdown_grace_seconds,
            )
            self._force_exit(0)

        self._shutdown_watchdog_thread = threading.Thread(
            target=watchdog,
            name="shutdown-watchdog",
            daemon=True,
        )
        self._shutdown_watchdog_thread.start()

    def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        self.stop_event.set()
        for thread in (self._worker_thread, self._heartbeat_thread):
            if thread and thread is not threading.current_thread():
                thread.join(timeout=10)
        self.outbox.flush()
        self.v2_outbox.flush()
        self.media_cache.close()
        self.connector_registry.close()
        self.source_manager.close()
        self.executor.close()

    def status(self) -> dict:
        snapshot = self.executor.snapshot()
        from .wechat_identity import get_wechat_identity_status

        wechat_status = snapshot.model_dump(by_alias=True, mode="json")
        wechat_status["identityRecognition"] = get_wechat_identity_status()
        active = self.ledger.get_active_task()
        v2_active = self.v2_ledger.get_active_task()
        local_active = self.local_schedule_store.active()
        sources = self.source_manager.status()
        outbox_backlog = self.ledger.outbox_backlog() + self.v2_ledger.outbox_backlog()
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
                "version": AGENT_VERSION,
            },
            "wechat": wechat_status,
            "worker": {
                "active": (
                    self.worker.is_active
                    or self.v2_worker.is_active
                    or self.local_schedule_worker.is_active
                ),
                "task": (
                    {
                        "sourceId": active.source_id,
                        "taskId": active.claim.task.task_id,
                        "state": active.state,
                    }
                    if active
                    else (
                        {
                            "sourceId": v2_active.source_id,
                            "taskId": v2_active.claim.task.task_id,
                            "state": v2_active.state,
                            "executorInstanceId": v2_active.claim.task.route.executor_instance_id,
                            "protocolVersion": "2.0",
                        }
                        if v2_active
                        else (
                            {
                                "sourceId": "local-schedule",
                                "taskId": local_active.task_id,
                                "state": local_active.state,
                                "protocolVersion": "local-1.0",
                            }
                            if local_active
                            else None
                        )
                    )
                ),
                "lastErrorCode": (
                    self.local_schedule_worker.last_error_code
                    or self.v2_worker.last_error_code
                    or self.worker.last_error_code
                ),
                "lastErrorMessage": (
                    self.local_schedule_worker.last_error_message
                    or self.v2_worker.last_error_message
                    or self.worker.last_error_message
                ),
                "lastStageDurationsMs": self.worker.timings(),
            },
            "sources": sources,
            "connectors": self.connector_registry.status(),
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
        if not self._preflight_lock.acquire(blocking=False):
            raise RuntimeError("环境预检正在执行，请等待当前检查结束")
        try:
            if self.v2_worker.is_active or self.local_schedule_worker.is_active:
                raise RuntimeError("发布任务正在执行，请稍后再预检")
            with self.worker.exclusive_desktop_action(timeout=2.0):
                return self.executor.preflight().model_dump(by_alias=True, mode="json")
        finally:
            self._preflight_lock.release()

    def recognize_wechat_identity(self) -> dict:
        from .environment import is_desktop_unlocked, is_interactive_session
        from .wechat_identity import get_wechat_identity, get_wechat_identity_status

        if not is_interactive_session() or not is_desktop_unlocked():
            raise RuntimeError("Windows 桌面已锁定或不可交互，无法识别微信账号")
        # Worker.is_active also covers short no-task polling cycles. Waiting for
        # its lock avoids a false conflict while still excluding real publishes.
        with self.worker.exclusive_desktop_action():
            if (
                self.ledger.get_active_task() is not None
                or self.v2_ledger.get_active_task() is not None
                or self.local_schedule_store.active() is not None
            ):
                raise RuntimeError("发布任务执行期间不能切换微信窗口，请稍后重试")
            identity = get_wechat_identity(force=True)
        return {
            "recognized": identity is not None,
            "nickname": identity.nickname if identity else None,
            "wechatId": identity.wechat_id if identity else None,
            "diagnostic": get_wechat_identity_status(),
        }

    def update_identity(self, display_name: str, account_key: str) -> None:
        with self._config_lock:
            self.config.agent.display_name = display_name
            self.config.agent.account_key = account_key
            save_config(self.config, self.config_path)
            self.source_manager.reload(self.config)
            self.connector_registry.reload(self.config)

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
            payload["type"] = body.type
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
            v2_active = self.v2_ledger.get_active_task()
            if (active and active.source_id == source_id) or (
                v2_active and v2_active.source_id == source_id
            ):
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

    def upsert_wechat_sync_profile(self, body, *, create_only: bool) -> None:
        with self._config_lock:
            existing_index = next(
                (
                    index
                    for index, profile in enumerate(self.config.wechat_sync_profiles)
                    if profile.id == body.id
                ),
                None,
            )
            if create_only and existing_index is not None:
                raise ValueError(f"WechatSync profile {body.id} already exists")
            if not create_only and existing_index is None:
                raise ValueError(f"WechatSync profile {body.id} does not exist")
            token_ref = f"dpapi://wechatsync-{body.id}"
            try:
                self.credential_store.get(token_ref)
            except Exception:
                self.credential_store.set(token_ref, secrets.token_urlsafe(32))
            profile = WechatSyncProfileConfig.model_validate(
                {
                    **body.model_dump(by_alias=True, mode="json"),
                    "bridgeHost": "127.0.0.1",
                    "tokenRef": token_ref,
                }
            )
            if existing_index is None:
                self.config.wechat_sync_profiles.append(profile)
            else:
                old = self.config.wechat_sync_profiles[existing_index]
                profile.token_ref = old.token_ref
                self.config.wechat_sync_profiles[existing_index] = profile
            save_config(self.config, self.config_path)
            self.connector_registry.reload(self.config)

    def delete_wechat_sync_profile(self, profile_id: str) -> None:
        with self._config_lock:
            existing = next(
                (
                    profile
                    for profile in self.config.wechat_sync_profiles
                    if profile.id == profile_id
                ),
                None,
            )
            if existing is None:
                raise KeyError(f"WechatSync profile {profile_id} does not exist")
            active = self.v2_ledger.get_active_task()
            if active and active.claim.task.route.profile_id == profile_id:
                raise RuntimeError("cannot delete a profile with an active task")
            self.config.wechat_sync_profiles = [
                profile
                for profile in self.config.wechat_sync_profiles
                if profile.id != profile_id
            ]
            save_config(self.config, self.config_path)
            self.connector_registry.reload(self.config)
            self.credential_store.delete(existing.token_ref)

    def wechat_sync_token(self, profile_id: str) -> str:
        profile = next(
            (
                item
                for item in self.config.wechat_sync_profiles
                if item.id == profile_id
            ),
            None,
        )
        if profile is None:
            raise KeyError(f"WechatSync profile {profile_id} does not exist")
        return self.credential_store.get(profile.token_ref)

    def create_local_schedule(self, body: LocalScheduleCreateRequest):
        self._validate_schedule_time(body.scheduled_at)
        snapshot = self.executor.snapshot()
        if not snapshot.running or not snapshot.logged_in:
            raise RuntimeError("请先启动并登录微信，再创建本机定时任务")
        if not snapshot.wechat_id and not snapshot.wechat_nickname:
            raise RuntimeError("尚未识别当前微信账号，请先点击“重新识别”")
        return self.local_schedule_store.create(
            text=body.text,
            image_paths=body.image_paths,
            scheduled_at=body.scheduled_at,
            target_account_key=self.config.agent.account_key,
            target_wechat_id=snapshot.wechat_id,
            target_nickname=snapshot.wechat_nickname,
        )

    def update_local_schedule(self, task_id: str, body: LocalScheduleUpdateRequest):
        self._validate_schedule_time(body.scheduled_at)
        return self.local_schedule_store.update(
            task_id,
            text=body.text,
            scheduled_at=body.scheduled_at,
            image_paths=body.image_paths,
        )

    def cancel_local_schedule(self, task_id: str):
        return self.local_schedule_store.cancel(task_id)

    @staticmethod
    def _validate_schedule_time(scheduled_at) -> None:
        delta = scheduled_at.astimezone(timezone.utc) - utc_now()
        if delta.total_seconds() < 5:
            raise ValueError("定时时间至少需要晚于当前时间 5 秒")
        if delta.total_seconds() > 365 * 24 * 60 * 60:
            raise ValueError("定时时间不能超过未来 365 天")

    def _worker_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                if self.ledger.get_active_task() is not None:
                    worked = self.worker.run_once()
                elif self.v2_ledger.get_active_task() is not None:
                    worked = self.v2_worker.run_once()
                else:
                    worked = self.local_schedule_worker.run_once()
                    if not worked:
                        worked = self.worker.run_once()
                    if not worked:
                        worked = self.v2_worker.run_once()
            except Exception:
                logger.exception("worker loop failed")
                worked = False
            delay = 0.25 if worked else self.config.runtime.poll_seconds
            self.stop_event.wait(delay)

    def _heartbeat_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                snapshot = self.executor.snapshot()
                self.connector_registry.refresh_browser_accounts()
                executors, accounts = self.connector_registry.runtime(snapshot)
                self.source_manager.heartbeat_all(snapshot)
                self.source_manager.heartbeat_v2(snapshot, executors, accounts)
            except Exception:
                logger.exception("heartbeat loop failed")
            self.stop_event.wait(self.config.runtime.heartbeat_seconds)

    def _ensure_connector_credentials(self) -> None:
        for profile in self.config.wechat_sync_profiles:
            try:
                self.credential_store.get(profile.token_ref)
            except Exception:
                self.credential_store.set(profile.token_ref, secrets.token_urlsafe(32))

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
