from __future__ import annotations

import logging
import threading

from ..config import AgentConfig
from ..credential_store import CredentialStore
from ..models import AgentSnapshot
from ..models_v2 import PublisherV2Account, PublisherV2Capability, PublisherV2Executor
from .wechatsync import WechatSyncConnector

logger = logging.getLogger(__name__)


def windows_desktop_ready(snapshot: AgentSnapshot) -> bool:
    """The Moments window is opened on demand during task preflight."""
    return bool(
        snapshot.interactive_session
        and snapshot.desktop_unlocked
        and snapshot.running
        and snapshot.logged_in
    )


class ConnectorRegistry:
    """Own all executors exposed by one Windows Agent Host."""

    WINDOWS_EXECUTOR_ID = "windows_moments:desktop"

    def __init__(self, config: AgentConfig, credential_store: CredentialStore) -> None:
        self.config = config
        self.credential_store = credential_store
        self._lock = threading.RLock()
        self._connectors: dict[str, WechatSyncConnector] = {}
        self._started = False
        self.reload(config)

    def reload(self, config: AgentConfig) -> None:
        with self._lock:
            self.config = config
            configured_ids = {profile.id for profile in config.wechat_sync_profiles}
            for profile_id in set(self._connectors) - configured_ids:
                self._connectors.pop(profile_id).stop()
            for profile in config.wechat_sync_profiles:
                current = self._connectors.get(profile.id)
                if current is not None and current.profile == profile:
                    continue
                if current is not None:
                    current.stop()
                connector = WechatSyncConnector(profile, self.credential_store)
                self._connectors[profile.id] = connector
                if self._started:
                    connector.start()

    def start(self) -> None:
        with self._lock:
            self._started = True
            connectors = list(self._connectors.values())
        for connector in connectors:
            connector.start()

    def close(self) -> None:
        with self._lock:
            self._started = False
            connectors = list(self._connectors.values())
            self._connectors.clear()
        for connector in connectors:
            connector.stop()

    def refresh_browser_accounts(self, *, force: bool = False) -> None:
        with self._lock:
            connectors = list(self._connectors.values())
        for connector in connectors:
            connector.refresh_accounts(force=force)

    def runtime(
        self,
        snapshot: AgentSnapshot,
    ) -> tuple[list[PublisherV2Executor], list[PublisherV2Account]]:
        windows_ready = windows_desktop_ready(snapshot)
        windows_executor = PublisherV2Executor(
            executorInstanceId=self.WINDOWS_EXECUTOR_ID,
            providerKey="windows_moments",
            executionMode="windows_ui",
            status="ready" if windows_ready else "degraded",
            capabilities=[
                PublisherV2Capability(
                    platform="wechat_moments",
                    operations=["publish"],
                    contentTypes=["image_text"],
                )
            ],
            lastErrorCode=None if windows_ready else "CONNECTOR_UNAVAILABLE",
            lastErrorMessage=None if windows_ready else "WeChat desktop is not available.",
        )
        accounts: list[PublisherV2Account] = []
        if snapshot.logged_in:
            accounts.append(
                PublisherV2Account(
                    executorInstanceId=self.WINDOWS_EXECUTOR_ID,
                    platform="wechat_moments",
                    accountStableId=snapshot.wechat_id or self.config.agent.account_key,
                    nickname=snapshot.wechat_nickname or self.config.agent.account_key,
                    accountKey=self.config.agent.account_key,
                    authState="authenticated",
                    status="ready" if windows_ready else "degraded",
                )
            )
        with self._lock:
            connectors = list(self._connectors.values())
        executors = [windows_executor, *(connector.executor() for connector in connectors)]
        for connector in connectors:
            accounts.extend(connector.accounts())
        return executors, accounts

    def connector_for_executor(self, executor_instance_id: str) -> WechatSyncConnector:
        with self._lock:
            for connector in self._connectors.values():
                if connector.executor_instance_id == executor_instance_id:
                    return connector
        raise KeyError(f"unknown executor {executor_instance_id}")

    def status(self) -> list[dict]:
        with self._lock:
            return [connector.status() for connector in self._connectors.values()]

    def test(self, profile_id: str) -> dict:
        with self._lock:
            try:
                connector = self._connectors[profile_id]
            except KeyError as error:
                raise KeyError(f"unknown WechatSync profile {profile_id}") from error
        connector.refresh_accounts(force=True)
        return connector.status()

    def launch(self, profile_id: str) -> dict:
        with self._lock:
            try:
                connector = self._connectors[profile_id]
            except KeyError as error:
                raise KeyError(f"unknown WechatSync profile {profile_id}") from error
        connector.launch_chrome()
        return connector.status()
