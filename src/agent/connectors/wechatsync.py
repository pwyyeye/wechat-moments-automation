from __future__ import annotations

import base64
import hashlib
import logging
import mimetypes
import os
import subprocess
import threading
from pathlib import Path
from typing import Any

from ..config import WechatSyncProfileConfig
from ..credential_store import CredentialStore
from ..models_v2 import (
    PublisherV2Account,
    PublisherV2Capability,
    PublisherV2Executor,
    PublisherV2Task,
)
from .wechatsync_bridge import WechatSyncBridge

logger = logging.getLogger(__name__)


class ConnectorError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class WechatSyncConnector:
    VERSION = "2.0.9-compatible"

    def __init__(
        self,
        profile: WechatSyncProfileConfig,
        credential_store: CredentialStore,
    ) -> None:
        self.profile = profile
        self.credential_store = credential_store
        self.executor_instance_id = f"wechatsync:{profile.id}"
        self._lock = threading.RLock()
        self._accounts: list[PublisherV2Account] = []
        self._last_error_code: str | None = None
        self._last_error_message: str | None = None
        self.bridge = WechatSyncBridge(
            profile.bridge_host,
            profile.bridge_port,
            credential_store.get(profile.token_ref),
        )

    def start(self) -> None:
        if not self.profile.enabled:
            return
        try:
            self.bridge.start()
            if self.profile.auto_launch:
                self.launch_chrome()
        except Exception as error:
            self._set_error("CONNECTOR_UNAVAILABLE", str(error))

    def stop(self) -> None:
        self.bridge.stop()

    def launch_chrome(self) -> None:
        executable = self._chrome_executable()
        arguments = [str(executable), "--new-window"]
        if self.profile.user_data_dir:
            arguments.append(f"--user-data-dir={Path(self.profile.user_data_dir)}")
        if self.profile.profile_directory:
            arguments.append(f"--profile-directory={self.profile.profile_directory}")
        if self.profile.extension_path:
            extension_path = Path(self.profile.extension_path).resolve()
            if not extension_path.is_dir():
                raise ConnectorError(
                    "CONNECTOR_UNAVAILABLE",
                    f"WechatSync extension path does not exist: {extension_path}",
                    retryable=False,
                )
            arguments.append(f"--load-extension={extension_path}")
        subprocess.Popen(arguments, close_fds=True)

    def refresh_accounts(self, *, force: bool = False) -> list[PublisherV2Account]:
        if not self.profile.enabled or not self.bridge.connected:
            with self._lock:
                self._accounts = []
            return []
        accounts: list[PublisherV2Account] = []
        try:
            for platform in self.profile.platforms:
                result = self.bridge.request(
                    "checkAuth",
                    {"platform": platform, "forceRefresh": force},
                    timeout=30,
                )
                if not isinstance(result, dict) or not result.get("isAuthenticated"):
                    continue
                nickname = str(result.get("username") or platform)
                stable_id = str(
                    result.get("userId")
                    or self._fallback_stable_id(platform, nickname)
                )
                accounts.append(
                    PublisherV2Account(
                        executorInstanceId=self.executor_instance_id,
                        platform=platform,
                        accountStableId=stable_id,
                        nickname=nickname,
                        accountKey=f"{self.profile.id}:{platform}",
                        profileId=self.profile.id,
                        authState="authenticated",
                        status="ready",
                    )
                )
        except Exception as error:
            self._set_error("CONNECTOR_UNAVAILABLE", str(error))
            with self._lock:
                self._accounts = []
            return []
        with self._lock:
            self._accounts = accounts
            self._last_error_code = None
            self._last_error_message = None
        return list(accounts)

    def executor(self) -> PublisherV2Executor:
        connected = self.profile.enabled and self.bridge.connected
        with self._lock:
            return PublisherV2Executor(
                executorInstanceId=self.executor_instance_id,
                providerKey="wechatsync",
                executionMode="browser_bridge",
                profileId=self.profile.id,
                connectorVersion=self.VERSION,
                status="ready" if connected else "offline",
                capabilities=[
                    PublisherV2Capability(
                        platform=platform,
                        operations=["create_draft"],
                        contentTypes=["article"],
                    )
                    for platform in self.profile.platforms
                ],
                lastErrorCode=self._last_error_code,
                lastErrorMessage=self._last_error_message,
            )

    def accounts(self) -> list[PublisherV2Account]:
        with self._lock:
            return list(self._accounts)

    def create_draft(self, task: PublisherV2Task, media_paths: list[str]) -> dict[str, Any]:
        self._assert_route(task)
        markdown = task.content.markdown or task.content.text or ""
        html = task.content.html or ""
        markdown, html, cover = self._embed_media(task, media_paths, markdown, html)
        try:
            response = self.bridge.request(
                "syncArticle",
                {
                    "platforms": [task.route.platform],
                    "article": {
                        "title": task.content.title or "未命名内容",
                        "markdown": markdown,
                        "content": html,
                        "cover": cover,
                    },
                },
                timeout=360,
            )
        except TimeoutError as error:
            raise ConnectorError("CONNECTOR_TIMEOUT", str(error), retryable=False) from error
        except Exception as error:
            raise ConnectorError("CONNECTOR_UNAVAILABLE", str(error), retryable=True) from error
        if isinstance(response, list):
            results = response
            sync_id = None
        elif isinstance(response, dict):
            results = response.get("results", [])
            sync_id = response.get("syncId")
        else:
            raise ConnectorError(
                "CONNECTOR_PROTOCOL_ERROR",
                "WechatSync returned an unsupported response.",
                retryable=False,
            )
        result = next(
            (item for item in results if item.get("platform") == task.route.platform),
            None,
        )
        if not result or not result.get("success"):
            message = (result or {}).get("error") or "WechatSync draft creation failed."
            raise ConnectorError("PLATFORM_DRAFT_FAILED", message, retryable=False)
        if result.get("draftOnly") is not True:
            raise ConnectorError(
                "CONNECTOR_PROTOCOL_ERROR",
                "WechatSync did not confirm a draft-only result.",
                retryable=False,
            )
        return {
            "syncId": sync_id,
            "postId": result.get("postId"),
            "postUrl": result.get("postUrl"),
            "draftOnly": True,
        }

    def status(self) -> dict[str, Any]:
        executor = self.executor()
        return {
            "id": self.profile.id,
            "name": self.profile.name,
            "enabled": self.profile.enabled,
            "bridgeUrl": f"ws://127.0.0.1:{self.profile.bridge_port}",
            "executorInstanceId": self.executor_instance_id,
            "profileId": self.profile.id,
            "connected": self.bridge.connected,
            "status": executor.status,
            "platforms": list(self.profile.platforms),
            "chromeExecutable": self.profile.chrome_executable,
            "userDataDir": self.profile.user_data_dir,
            "profileDirectory": self.profile.profile_directory,
            "extensionPath": self.profile.extension_path,
            "autoLaunch": self.profile.auto_launch,
            "accounts": [item.model_dump(by_alias=True) for item in self.accounts()],
            "lastErrorCode": executor.last_error_code,
            "lastErrorMessage": executor.last_error_message,
        }

    def _assert_route(self, task: PublisherV2Task) -> None:
        route = task.route
        if route.provider_key != "wechatsync" or route.operation != "create_draft":
            raise ConnectorError(
                "CAPABILITY_MISMATCH",
                f"WechatSync cannot execute {route.provider_key}/{route.operation}.",
                retryable=False,
            )
        if route.executor_instance_id and route.executor_instance_id != self.executor_instance_id:
            raise ConnectorError("CONNECTOR_UNAVAILABLE", "Task targets another executor.", retryable=False)
        if route.profile_id and route.profile_id != self.profile.id:
            raise ConnectorError("ACCOUNT_MISMATCH", "Task targets another Chrome profile.", retryable=False)
        account = next(
            (
                item
                for item in self.accounts()
                if item.platform == route.platform
                and item.account_stable_id == route.account_stable_id
            ),
            None,
        )
        if account is None:
            raise ConnectorError(
                "ACCOUNT_MISMATCH",
                f"Current {route.platform} account does not match the task target.",
                retryable=False,
            )

    @staticmethod
    def _embed_media(
        task: PublisherV2Task,
        media_paths: list[str],
        markdown: str,
        html: str,
    ) -> tuple[str, str, str | None]:
        cover = None
        unreferenced: list[tuple[str, str]] = []
        for item, raw_path in zip(task.content.media, media_paths):
            path = Path(raw_path)
            mime_type = item.mime_type or mimetypes.guess_type(path.name)[0] or "image/png"
            data_uri = f"data:{mime_type};base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"
            cover = cover or data_uri
            candidates = [str(item.download_url), item.media_id, item.file_name]
            replaced = False
            for candidate in candidates:
                if candidate and candidate in markdown:
                    markdown = markdown.replace(candidate, data_uri)
                    replaced = True
                if candidate and candidate in html:
                    html = html.replace(candidate, data_uri)
                    replaced = True
            if not replaced:
                unreferenced.append((item.file_name, data_uri))
        if unreferenced:
            suffix = "\n\n".join(f"![{name}]({uri})" for name, uri in unreferenced)
            markdown = f"{markdown.rstrip()}\n\n{suffix}".strip()
        return markdown, html, cover

    def _fallback_stable_id(self, platform: str, nickname: str) -> str:
        value = f"{self.profile.id}\0{platform}\0{nickname}".encode("utf-8")
        return f"profile-user-{hashlib.sha256(value).hexdigest()[:24]}"

    def _set_error(self, code: str, message: str) -> None:
        with self._lock:
            self._last_error_code = code
            self._last_error_message = message[:1000]

    def _chrome_executable(self) -> Path:
        candidates = [
            self.profile.chrome_executable,
            os.path.join(os.environ.get("PROGRAMFILES", ""), "Google", "Chrome", "Application", "chrome.exe"),
            os.path.join(os.environ.get("PROGRAMFILES(X86)", ""), "Google", "Chrome", "Application", "chrome.exe"),
            os.path.join(os.environ.get("LOCALAPPDATA", ""), "Google", "Chrome", "Application", "chrome.exe"),
        ]
        for candidate in candidates:
            if candidate and Path(candidate).is_file():
                return Path(candidate)
        raise ConnectorError("CONNECTOR_UNAVAILABLE", "Google Chrome was not found.", retryable=False)
