from __future__ import annotations

import base64
import os
from pathlib import Path
from typing import Protocol


class CredentialStore(Protocol):
    def get(self, reference: str) -> str: ...

    def set(self, reference: str, secret: str) -> None: ...

    def delete(self, reference: str) -> None: ...


class PayloadProtector(Protocol):
    def protect(self, value: bytes) -> bytes: ...

    def unprotect(self, value: bytes) -> bytes: ...


class DpapiPayloadProtector:
    def protect(self, value: bytes) -> bytes:
        import win32crypt

        return win32crypt.CryptProtectData(
            value,
            "WechatPublisherAgent",
            None,
            None,
            None,
            0,
        )

    def unprotect(self, value: bytes) -> bytes:
        import win32crypt

        _, plaintext = win32crypt.CryptUnprotectData(value, None, None, None, 0)
        return plaintext


class IdentityPayloadProtector:
    """Test-only protector; production always uses DPAPI."""

    def protect(self, value: bytes) -> bytes:
        return value

    def unprotect(self, value: bytes) -> bytes:
        return value


class DpapiCredentialStore:
    """Store per-source secrets encrypted for the current Windows user."""

    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, reference: str) -> Path:
        if not reference.startswith("dpapi://"):
            raise ValueError("credentialRef must start with dpapi://")
        name = reference.removeprefix("dpapi://")
        if not name or any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-" for character in name):
            raise ValueError("credentialRef contains unsupported characters")
        return self.root / f"{name}.secret"

    def get(self, reference: str) -> str:
        import win32crypt

        encrypted = base64.b64decode(self._path(reference).read_bytes())
        _, plaintext = win32crypt.CryptUnprotectData(encrypted, None, None, None, 0)
        return plaintext.decode("utf-8")

    def set(self, reference: str, secret: str) -> None:
        import win32crypt

        if not secret:
            raise ValueError("secret cannot be empty")
        encrypted = win32crypt.CryptProtectData(
            secret.encode("utf-8"),
            "WechatPublisherAgent",
            None,
            None,
            None,
            0,
        )
        path = self._path(reference)
        temporary = path.with_suffix(f".{os.getpid()}.tmp")
        temporary.write_bytes(base64.b64encode(encrypted))
        temporary.replace(path)

    def delete(self, reference: str) -> None:
        path = self._path(reference)
        if path.exists():
            path.unlink()


class InMemoryCredentialStore:
    def __init__(self):
        self.values: dict[str, str] = {}

    def get(self, reference: str) -> str:
        return self.values[reference]

    def set(self, reference: str, secret: str) -> None:
        self.values[reference] = secret

    def delete(self, reference: str) -> None:
        self.values.pop(reference, None)
