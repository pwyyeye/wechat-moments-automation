from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from websockets.sync.server import Server, ServerConnection, serve

logger = logging.getLogger(__name__)


@dataclass
class PendingRequest:
    completed: threading.Event = field(default_factory=threading.Event)
    result: Any = None
    error: str | None = None


class WechatSyncBridge:
    """Loopback-only implementation of the WechatSync extension bridge."""

    def __init__(self, host: str, port: int, token: str) -> None:
        if host != "127.0.0.1":
            raise ValueError("WechatSync bridge must bind to 127.0.0.1")
        self.host = host
        self.port = port
        self.token = token
        self._lock = threading.RLock()
        self._ready = threading.Event()
        self._server: Server | None = None
        self._connection: ServerConnection | None = None
        self._thread: threading.Thread | None = None
        self._pending: dict[str, PendingRequest] = {}
        self.last_error: str | None = None

    @property
    def connected(self) -> bool:
        with self._lock:
            return self._connection is not None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._ready.clear()
        self._thread = threading.Thread(
            target=self._run,
            name=f"wechatsync-bridge-{self.port}",
            daemon=True,
        )
        self._thread.start()
        if not self._ready.wait(5):
            raise RuntimeError(f"WechatSync bridge {self.port} did not start")
        if self.last_error:
            raise RuntimeError(self.last_error)

    def stop(self) -> None:
        with self._lock:
            server = self._server
            connection = self._connection
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass
        if server is not None:
            server.shutdown()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=5)
        self._fail_pending("WechatSync bridge stopped")

    def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float = 60,
    ) -> Any:
        request_id = f"agent-{uuid4().hex}"
        pending = PendingRequest()
        with self._lock:
            connection = self._connection
            if connection is None:
                raise RuntimeError("WechatSync Chrome extension is not connected")
            self._pending[request_id] = pending
            message = {
                "id": request_id,
                "method": method,
                "token": self.token,
                "params": params or {},
            }
            try:
                connection.send(json.dumps(message, ensure_ascii=False))
            except Exception:
                self._pending.pop(request_id, None)
                raise
        if not pending.completed.wait(timeout):
            with self._lock:
                self._pending.pop(request_id, None)
            raise TimeoutError(f"WechatSync request timed out: {method}")
        if pending.error:
            raise RuntimeError(pending.error)
        return pending.result

    def _run(self) -> None:
        try:
            with serve(
                self._handle_connection,
                self.host,
                self.port,
                max_size=64 * 1024 * 1024,
                open_timeout=10,
            ) as server:
                with self._lock:
                    self._server = server
                    self.last_error = None
                self._ready.set()
                server.serve_forever()
        except Exception as error:
            self.last_error = f"WechatSync bridge {self.host}:{self.port} failed: {error}"
            logger.exception("WechatSync bridge failed port=%s", self.port)
            self._ready.set()
        finally:
            with self._lock:
                self._server = None
                self._connection = None
            self._fail_pending("WechatSync extension disconnected")

    def _handle_connection(self, connection: ServerConnection) -> None:
        remote = connection.remote_address
        remote_host = remote[0] if isinstance(remote, tuple) and remote else None
        if remote_host not in {"127.0.0.1", "::1"}:
            connection.close(code=1008, reason="loopback clients only")
            return
        with self._lock:
            previous = self._connection
            self._connection = connection
        if previous is not None and previous is not connection:
            try:
                previous.close(code=1012, reason="new extension connection")
            except Exception:
                pass
        logger.info("WechatSync extension connected port=%s", self.port)
        try:
            for raw in connection:
                self._handle_message(raw)
        except Exception:
            logger.info("WechatSync extension connection closed port=%s", self.port)
        finally:
            was_active = False
            with self._lock:
                if self._connection is connection:
                    self._connection = None
                    was_active = True
            if was_active:
                self._fail_pending("WechatSync extension disconnected")

    def _handle_message(self, raw: str | bytes) -> None:
        try:
            payload = json.loads(raw)
            request_id = payload.get("id")
        except (TypeError, ValueError):
            logger.warning("ignored malformed WechatSync bridge response")
            return
        if not request_id:
            return
        with self._lock:
            pending = self._pending.pop(request_id, None)
        if pending is None:
            return
        error = payload.get("error")
        if error:
            pending.error = (
                error.get("message", str(error)) if isinstance(error, dict) else str(error)
            )
        else:
            pending.result = payload.get("result")
        pending.completed.set()

    def _fail_pending(self, message: str) -> None:
        with self._lock:
            pending = list(self._pending.values())
            self._pending.clear()
        for item in pending:
            item.error = message
            item.completed.set()
