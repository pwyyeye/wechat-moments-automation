from __future__ import annotations

import hashlib
import ipaddress
import os
import shutil
import socket
from pathlib import Path
from typing import Callable
from urllib.parse import urljoin, urlparse

import httpx

from .config import SourceConfig
from .credential_store import CredentialStore
from .models import MediaItem, PublisherTask


class MediaDownloadError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


def resolve_host(hostname: str) -> list[str]:
    return list(
        {
            item[4][0]
            for item in socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
        }
    )


def is_private_address(value: str) -> bool:
    address = ipaddress.ip_address(value)
    return bool(
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    )


class MediaCache:
    def __init__(
        self,
        root: Path,
        max_cache_mib: int,
        *,
        client: httpx.Client | None = None,
        address_resolver: Callable[[str], list[str]] = resolve_host,
        credential_store: CredentialStore | None = None,
    ):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.max_cache_bytes = max_cache_mib * 1024 * 1024
        self.client = client or httpx.Client(
            timeout=httpx.Timeout(60.0, connect=10.0),
            follow_redirects=False,
        )
        self.address_resolver = address_resolver
        self.credential_store = credential_store

    def download_task(self, source: SourceConfig, task: PublisherTask) -> list[str]:
        if sum(item.size_bytes for item in task.content.media) > 60 * 1024 * 1024:
            raise MediaDownloadError(
                "TASK_CAPABILITY_UNSUPPORTED",
                "Task media exceeds the 60 MiB Windows profile limit.",
                retryable=False,
            )
        task_root = self.root / source.id / task.task_id
        task_root.mkdir(parents=True, exist_ok=True)
        paths = [self._download_one(source, item, task_root) for item in task.content.media]
        self.prune(exclude=task_root)
        return [str(path) for path in paths]

    def _download_one(
        self,
        source: SourceConfig,
        item: MediaItem,
        task_root: Path,
    ) -> Path:
        extension = ".jpg" if item.mime_type == "image/jpeg" else ".png"
        destination = task_root / f"{item.sha256}{extension}"
        if destination.exists() and self._sha256(destination) == item.sha256:
            os.utime(destination, None)
            return destination

        current_url = str(item.download_url)
        response = None
        for _ in range(4):
            self._validate_url(source, current_url)
            try:
                request = self.client.build_request(
                    "GET",
                    current_url,
                    headers=self._source_auth_headers(source, current_url),
                )
                response = self.client.send(request, stream=True)
            except httpx.HTTPError as error:
                raise MediaDownloadError(
                    "MEDIA_DOWNLOAD_FAILED",
                    f"Media download failed: {error.__class__.__name__}",
                    retryable=True,
                ) from error
            if response.status_code in {301, 302, 303, 307, 308}:
                location = response.headers.get("Location")
                if not location:
                    response.close()
                    raise MediaDownloadError(
                        "MEDIA_DOWNLOAD_FAILED",
                        "Media redirect is missing the Location header.",
                        retryable=True,
                    )
                current_url = urljoin(current_url, location)
                response.close()
                continue
            break
        else:
            raise MediaDownloadError(
                "MEDIA_DOWNLOAD_FAILED",
                "Media download exceeded the redirect limit.",
                retryable=False,
            )

        if response is None or not response.is_success:
            status = response.status_code if response is not None else "unknown"
            if response is not None:
                response.close()
            raise MediaDownloadError(
                "MEDIA_DOWNLOAD_FAILED",
                f"Media server returned HTTP {status}.",
                retryable=status in {408, 425, 429, 500, 502, 503, 504},
            )
        content_type = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
        if content_type and content_type != item.mime_type:
            response.close()
            raise MediaDownloadError(
                "MEDIA_MIME_MISMATCH",
                f"Expected {item.mime_type}, received {content_type}.",
                retryable=False,
            )
        content_length = response.headers.get("Content-Length")
        if content_length and content_length.isdigit() and int(content_length) != item.size_bytes:
            response.close()
            raise MediaDownloadError(
                "MEDIA_SIZE_MISMATCH",
                f"Expected {item.size_bytes} bytes, received Content-Length {content_length}.",
                retryable=False,
            )
        content = bytearray()
        try:
            for chunk in response.iter_bytes(64 * 1024):
                content.extend(chunk)
                if len(content) > item.size_bytes:
                    raise MediaDownloadError(
                        "MEDIA_SIZE_MISMATCH",
                        "Media response exceeded the declared task size.",
                        retryable=False,
                    )
        finally:
            response.close()
        if len(content) != item.size_bytes:
            raise MediaDownloadError(
                "MEDIA_SIZE_MISMATCH",
                f"Expected {item.size_bytes} bytes, received {len(content)}.",
                retryable=False,
            )
        digest = hashlib.sha256(content).hexdigest()
        if digest != item.sha256:
            raise MediaDownloadError(
                "MEDIA_HASH_MISMATCH",
                "Downloaded media SHA-256 does not match the task contract.",
                retryable=False,
            )
        temporary = destination.with_suffix(f"{destination.suffix}.tmp")
        temporary.write_bytes(content)
        temporary.replace(destination)
        return destination

    def _validate_url(self, source: SourceConfig, value: str) -> None:
        parsed = urlparse(value)
        if not parsed.hostname or parsed.username or parsed.password:
            raise MediaDownloadError(
                "MEDIA_DOWNLOAD_URL_UNSAFE",
                "Media URL must have a hostname and must not contain user info.",
                retryable=False,
            )
        if parsed.scheme != "https" and not source.media_security.allow_private_network:
            raise MediaDownloadError(
                "MEDIA_DOWNLOAD_URL_UNSAFE",
                "Media URL must use HTTPS.",
                retryable=False,
            )
        allowed_hosts = {
            host.rstrip(".").lower() for host in source.media_security.allowed_hosts
        }
        allowed_hosts.add(urlparse(str(source.base_url)).hostname.lower())
        hostname = parsed.hostname.rstrip(".").lower()
        if hostname not in allowed_hosts:
            raise MediaDownloadError(
                "MEDIA_HOST_NOT_ALLOWED",
                f"Media host {hostname} is not allowlisted for source {source.id}.",
                retryable=False,
            )
        try:
            addresses = self.address_resolver(hostname)
        except OSError as error:
            raise MediaDownloadError(
                "MEDIA_DNS_FAILED",
                f"Unable to resolve media host {hostname}.",
                retryable=True,
            ) from error
        if not addresses:
            raise MediaDownloadError(
                "MEDIA_DNS_FAILED",
                f"Media host {hostname} resolved to no addresses.",
                retryable=True,
            )
        if not source.media_security.allow_private_network and any(
            is_private_address(address) for address in addresses
        ):
            raise MediaDownloadError(
                "MEDIA_PRIVATE_ADDRESS_BLOCKED",
                f"Media host {hostname} resolves to a private or reserved address.",
                retryable=False,
            )

    def _source_auth_headers(self, source: SourceConfig, value: str) -> dict[str, str]:
        if self.credential_store is None:
            return {}
        source_host = urlparse(str(source.base_url)).hostname
        media_host = urlparse(value).hostname
        # Never forward a source credential to an allowlisted redirect host.
        if not source_host or not media_host or source_host.lower() != media_host.lower():
            return {}
        try:
            secret = self.credential_store.get(source.auth.credential_ref)
        except Exception as error:
            raise MediaDownloadError(
                "MEDIA_DOWNLOAD_AUTH_FAILED",
                "The source credential is unavailable for authenticated media download.",
                retryable=False,
            ) from error
        if source.auth.type == "bearer":
            return {"Authorization": f"Bearer {secret}"}
        return {source.auth.header_name or "X-Api-Key": secret}

    def cleanup_task(self, source_id: str, task_id: str) -> None:
        task_root = (self.root / source_id / task_id).resolve()
        cache_root = self.root.resolve()
        if cache_root not in task_root.parents:
            raise ValueError("task cache path escaped the cache root")
        if task_root.exists():
            shutil.rmtree(task_root)

    def prune(self, *, exclude: Path | None = None) -> None:
        files = [path for path in self.root.rglob("*") if path.is_file()]
        total = sum(path.stat().st_size for path in files)
        if total <= self.max_cache_bytes:
            return
        excluded = exclude.resolve() if exclude else None
        for path in sorted(files, key=lambda item: item.stat().st_mtime):
            if excluded and excluded in path.resolve().parents:
                continue
            size = path.stat().st_size
            path.unlink(missing_ok=True)
            total -= size
            if total <= self.max_cache_bytes:
                break

    def size_bytes(self) -> int:
        return sum(
            path.stat().st_size for path in self.root.rglob("*") if path.is_file()
        )

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def close(self) -> None:
        self.client.close()
