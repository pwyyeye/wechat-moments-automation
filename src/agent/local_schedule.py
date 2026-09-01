from __future__ import annotations

import hashlib
import json
import logging
import shutil
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator
from uuid import uuid4

from PIL import Image

from .executor import PublishExecutor
from .models import PublisherTask

logger = logging.getLogger(__name__)

ACTIVE_STATES = ("executing", "final_click_intent", "confirming")
EDITABLE_STATES = ("pending", "failed")
MAX_MEDIA_BYTES_PER_ITEM = 20 * 1024 * 1024
MAX_MEDIA_BYTES_PER_TASK = 60 * 1024 * 1024


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_iso(value: datetime | None = None) -> str:
    return (value or utc_now()).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


@dataclass(frozen=True)
class LocalMomentsTask:
    task_id: str
    text: str
    media: tuple[dict, ...]
    scheduled_at: str
    target_account_key: str
    target_wechat_id: str | None
    target_nickname: str | None
    state: str
    attempt: int
    final_click_intent_at: str | None
    error_code: str | None
    error_message: str | None
    created_at: str
    updated_at: str

    @property
    def media_paths(self) -> list[str]:
        return [item["path"] for item in self.media]

    def as_admin_dict(self) -> dict:
        return {
            "kind": "local_schedule",
            "source_id": "本机定时",
            "task_id": self.task_id,
            "state": self.state,
            "attempt": self.attempt,
            "scheduled_at": self.scheduled_at,
            "updated_at": self.updated_at,
            "final_click_intent_at": self.final_click_intent_at,
            "text": self.text,
            "text_preview": self.text[:80],
            "image_count": len(self.media),
            "media_paths": self.media_paths,
            "target_account_key": self.target_account_key,
            "target_wechat_id": self.target_wechat_id,
            "target_nickname": self.target_nickname,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "protocol_version": "local-1.0",
        }


class LocalScheduleStore:
    """Durable one-shot local schedules with immutable managed media copies."""

    def __init__(self, database_path: Path, media_root: Path):
        self.database_path = database_path
        self.media_root = media_root
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.media_root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._initialize()
        self._recover_interrupted_tasks()

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode = WAL")
        try:
            yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS local_moments_schedule (
                    task_id TEXT PRIMARY KEY,
                    text TEXT NOT NULL,
                    media_json TEXT NOT NULL,
                    scheduled_at TEXT NOT NULL,
                    target_account_key TEXT NOT NULL,
                    target_wechat_id TEXT NULL,
                    target_nickname TEXT NULL,
                    state TEXT NOT NULL,
                    attempt INTEGER NOT NULL DEFAULT 0,
                    final_click_intent_at TEXT NULL,
                    error_code TEXT NULL,
                    error_message TEXT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS local_moments_schedule_due
                    ON local_moments_schedule (state, scheduled_at);

                CREATE UNIQUE INDEX IF NOT EXISTS local_moments_schedule_one_active
                    ON local_moments_schedule ((CASE
                        WHEN state IN ('executing', 'final_click_intent', 'confirming')
                        THEN 1 ELSE NULL END));
                """
            )
            connection.commit()

    def _recover_interrupted_tasks(self) -> None:
        now = utc_iso()
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE local_moments_schedule
                SET state = 'pending', error_code = NULL, error_message = NULL,
                    updated_at = ?
                WHERE state = 'executing' AND final_click_intent_at IS NULL
                """,
                (now,),
            )
            connection.execute(
                """
                UPDATE local_moments_schedule
                SET state = 'uncertain', error_code = 'POST_CLICK_UNCONFIRMED',
                    error_message = 'Agent restarted after final-click intent; task was not repeated.',
                    updated_at = ?
                WHERE state IN ('final_click_intent', 'confirming')
                """,
                (now,),
            )
            connection.commit()

    def create(
        self,
        *,
        text: str,
        image_paths: list[str],
        scheduled_at: datetime,
        target_account_key: str,
        target_wechat_id: str | None,
        target_nickname: str | None,
    ) -> LocalMomentsTask:
        task_id = f"local-{uuid4().hex}"
        media = self._copy_media(task_id, image_paths)
        now = utc_iso()
        try:
            with self._lock, self._connection() as connection:
                connection.execute(
                    """
                    INSERT INTO local_moments_schedule (
                        task_id, text, media_json, scheduled_at, target_account_key,
                        target_wechat_id, target_nickname, state, attempt,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?)
                    """,
                    (
                        task_id,
                        text,
                        json.dumps(media, ensure_ascii=False),
                        utc_iso(scheduled_at),
                        target_account_key,
                        target_wechat_id,
                        target_nickname,
                        now,
                        now,
                    ),
                )
                connection.commit()
        except Exception:
            shutil.rmtree(self.media_root / task_id, ignore_errors=True)
            raise
        return self.get(task_id)

    def update(
        self,
        task_id: str,
        *,
        text: str,
        scheduled_at: datetime,
        image_paths: list[str] | None = None,
    ) -> LocalMomentsTask:
        current = self.get(task_id)
        if current.state not in EDITABLE_STATES or current.final_click_intent_at:
            raise RuntimeError("只有待执行或点击前失败的本机任务可以编辑")
        replacement = self._copy_media(task_id, image_paths) if image_paths is not None else None
        with self._lock, self._connection() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT state, final_click_intent_at FROM local_moments_schedule WHERE task_id = ?",
                    (task_id,),
                ).fetchone()
                if row is None:
                    raise KeyError(f"local schedule {task_id} does not exist")
                if row["state"] not in EDITABLE_STATES or row["final_click_intent_at"]:
                    raise RuntimeError("只有待执行或点击前失败的本机任务可以编辑")
                assignments = [
                    "text = ?",
                    "scheduled_at = ?",
                    "state = 'pending'",
                    "error_code = NULL",
                    "error_message = NULL",
                    "updated_at = ?",
                ]
                values: list[object] = [text, utc_iso(scheduled_at), utc_iso()]
                if replacement is not None:
                    assignments.append("media_json = ?")
                    values.append(json.dumps(replacement, ensure_ascii=False))
                values.append(task_id)
                connection.execute(
                    f"UPDATE local_moments_schedule SET {', '.join(assignments)} WHERE task_id = ?",
                    values,
                )
                connection.commit()
            except Exception:
                connection.rollback()
                if replacement is not None:
                    shutil.rmtree(Path(replacement[0]["path"]).parent, ignore_errors=True)
                raise
        return self.get(task_id)

    def cancel(self, task_id: str) -> LocalMomentsTask:
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT state, final_click_intent_at FROM local_moments_schedule WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise KeyError(f"local schedule {task_id} does not exist")
            if row["state"] not in EDITABLE_STATES or row["final_click_intent_at"]:
                connection.rollback()
                raise RuntimeError("只有待执行或点击前失败的本机任务可以取消")
            connection.execute(
                "UPDATE local_moments_schedule SET state = 'cancelled', updated_at = ? WHERE task_id = ?",
                (utc_iso(), task_id),
            )
            connection.commit()
        return self.get(task_id)

    def get(self, task_id: str) -> LocalMomentsTask:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM local_moments_schedule WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"local schedule {task_id} does not exist")
        return self._row_to_task(row)

    def list(self, limit: int = 50) -> list[LocalMomentsTask]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM local_moments_schedule ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._row_to_task(row) for row in rows]

    def has_due(self, now: datetime | None = None) -> bool:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM local_moments_schedule
                WHERE state = 'pending' AND scheduled_at <= ? LIMIT 1
                """,
                (utc_iso(now),),
            ).fetchone()
        return row is not None

    def active(self) -> LocalMomentsTask | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM local_moments_schedule
                WHERE state IN ('executing', 'final_click_intent', 'confirming') LIMIT 1
                """
            ).fetchone()
        return self._row_to_task(row) if row else None

    def claim_due(self, now: datetime | None = None) -> LocalMomentsTask | None:
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            active = connection.execute(
                """
                SELECT 1 FROM local_moments_schedule
                WHERE state IN ('executing', 'final_click_intent', 'confirming') LIMIT 1
                """
            ).fetchone()
            if active:
                connection.rollback()
                return None
            row = connection.execute(
                """
                SELECT task_id FROM local_moments_schedule
                WHERE state = 'pending' AND scheduled_at <= ?
                ORDER BY scheduled_at, created_at LIMIT 1
                """,
                (utc_iso(now),),
            ).fetchone()
            if row is None:
                connection.rollback()
                return None
            connection.execute(
                """
                UPDATE local_moments_schedule
                SET state = 'executing', attempt = attempt + 1, updated_at = ?
                WHERE task_id = ?
                """,
                (utc_iso(), row["task_id"]),
            )
            connection.commit()
        return self.get(row["task_id"])

    def record_final_click_intent(self, task_id: str) -> None:
        now = utc_iso()
        self._set_state(
            task_id,
            "final_click_intent",
            final_click_intent_at=now,
            updated_at=now,
        )

    def mark_confirming(self, task_id: str) -> None:
        self._set_state(task_id, "confirming", updated_at=utc_iso())

    def finish(
        self,
        task_id: str,
        state: str,
        *,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> None:
        if state not in {"succeeded", "failed", "uncertain"}:
            raise ValueError(f"invalid terminal state: {state}")
        self._set_state(
            task_id,
            state,
            error_code=error_code,
            error_message=(error_message or "")[:1000] or None,
            updated_at=utc_iso(),
        )

    def _set_state(self, task_id: str, state: str, **fields) -> None:
        assignments = ["state = ?", *[f"{key} = ?" for key in fields]]
        values = [state, *fields.values(), task_id]
        with self._lock, self._connection() as connection:
            cursor = connection.execute(
                f"UPDATE local_moments_schedule SET {', '.join(assignments)} WHERE task_id = ?",
                values,
            )
            if cursor.rowcount != 1:
                raise KeyError(f"local schedule {task_id} does not exist")
            connection.commit()

    def _copy_media(self, task_id: str, image_paths: list[str]) -> list[dict]:
        if not 1 <= len(image_paths) <= 9:
            raise ValueError("请选择 1 到 9 张 JPG 或 PNG 图片")
        revision_root = self.media_root / task_id / uuid4().hex
        revision_root.mkdir(parents=True, exist_ok=False)
        copied: list[dict] = []
        total = 0
        try:
            for index, raw_path in enumerate(image_paths, start=1):
                source = Path(raw_path).expanduser().resolve(strict=True)
                size = source.stat().st_size
                if size <= 0 or size > MAX_MEDIA_BYTES_PER_ITEM:
                    raise ValueError(f"图片 {source.name} 必须小于等于 20 MiB")
                total += size
                if total > MAX_MEDIA_BYTES_PER_TASK:
                    raise ValueError("单个任务的图片总大小不能超过 60 MiB")
                with Image.open(source) as image:
                    image.verify()
                    image_format = (image.format or "").upper()
                if image_format not in {"JPEG", "PNG"}:
                    raise ValueError(f"图片 {source.name} 不是有效的 JPG 或 PNG 文件")
                suffix = ".jpg" if image_format == "JPEG" else ".png"
                destination = revision_root / f"{index:02d}{suffix}"
                shutil.copy2(source, destination)
                copied.append(
                    {
                        "path": str(destination),
                        "fileName": source.name,
                        "mimeType": "image/jpeg" if image_format == "JPEG" else "image/png",
                        "sizeBytes": size,
                        "sha256": self._sha256(destination),
                    }
                )
            return copied
        except Exception:
            shutil.rmtree(revision_root, ignore_errors=True)
            raise

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _row_to_task(row: sqlite3.Row) -> LocalMomentsTask:
        return LocalMomentsTask(
            task_id=row["task_id"],
            text=row["text"],
            media=tuple(json.loads(row["media_json"])),
            scheduled_at=row["scheduled_at"],
            target_account_key=row["target_account_key"],
            target_wechat_id=row["target_wechat_id"],
            target_nickname=row["target_nickname"],
            state=row["state"],
            attempt=int(row["attempt"]),
            final_click_intent_at=row["final_click_intent_at"],
            error_code=row["error_code"],
            error_message=row["error_message"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


class LocalScheduleWorker:
    """Executes due local schedules through the shared one-at-a-time desktop path."""

    def __init__(
        self,
        store: LocalScheduleStore,
        executor: PublishExecutor,
        desktop_action: Callable,
    ) -> None:
        self.store = store
        self.executor = executor
        self.desktop_action = desktop_action
        self._lock = threading.Lock()
        self.last_error_code: str | None = None
        self.last_error_message: str | None = None

    @property
    def is_active(self) -> bool:
        return self._lock.locked()

    def run_once(self) -> bool:
        if not self.store.has_due() or not self._lock.acquire(blocking=False):
            return False
        try:
            try:
                coordinator = self.desktop_action(timeout=0)
                with coordinator:
                    task = self.store.claim_due()
                    if task is None:
                        return False
                    self._execute(task)
                    return True
            except RuntimeError as error:
                if "Worker" in str(error):
                    return False
                raise
        finally:
            self._lock.release()

    def _execute(self, task: LocalMomentsTask) -> None:
        try:
            snapshot = self.executor.preflight(self._publisher_task(task))
            preflight_error = self._preflight_error(snapshot)
            if preflight_error:
                self._fail(task, *preflight_error)
                return
            if not self._account_matches(task, snapshot):
                expected = task.target_wechat_id or task.target_nickname or task.target_account_key
                actual = snapshot.wechat_id or snapshot.wechat_nickname or "未识别"
                self._fail(
                    task,
                    "ACCOUNT_MISMATCH",
                    f"当前微信账号 {actual} 与任务指定账号 {expected} 不一致，未执行发布。",
                )
                return

            def before_final_click() -> None:
                self.store.record_final_click_intent(task.task_id)

            def after_final_click() -> None:
                self.store.mark_confirming(task.task_id)

            result = self.executor.publish(
                self._publisher_task(task),
                task.media_paths,
                before_final_click,
                after_final_click,
            )
            if result.published:
                self.store.finish(task.task_id, "succeeded")
                self.last_error_code = None
                self.last_error_message = None
            elif result.final_click_intent or self.store.get(task.task_id).final_click_intent_at:
                self._uncertain(task, result.error_message or "发布点击后未能确认朋友圈结果。")
            else:
                self._fail(
                    task,
                    "CONTENT_INPUT_FAILED",
                    result.error_message or "最终点击前的桌面操作失败。",
                )
        except Exception as error:
            logger.exception("local schedule execution failed taskId=%s", task.task_id)
            if self.store.get(task.task_id).final_click_intent_at:
                self._uncertain(task, str(error))
            else:
                self._fail(task, "EDITOR_OPEN_FAILED", str(error))

    def _fail(self, task: LocalMomentsTask, code: str, message: str) -> None:
        self.store.finish(task.task_id, "failed", error_code=code, error_message=message)
        self.last_error_code = code
        self.last_error_message = message

    def _uncertain(self, task: LocalMomentsTask, message: str) -> None:
        self.store.finish(
            task.task_id,
            "uncertain",
            error_code="POST_CLICK_UNCONFIRMED",
            error_message=message,
        )
        self.last_error_code = "POST_CLICK_UNCONFIRMED"
        self.last_error_message = message

    @staticmethod
    def _account_matches(task: LocalMomentsTask, snapshot) -> bool:
        if task.target_wechat_id:
            return snapshot.wechat_id == task.target_wechat_id
        return bool(task.target_nickname and snapshot.wechat_nickname == task.target_nickname)

    @staticmethod
    def _preflight_error(snapshot) -> tuple[str, str] | None:
        if not snapshot.interactive_session or not snapshot.desktop_unlocked:
            return "DESKTOP_LOCKED", "Windows 桌面已锁定或不可交互。"
        if not snapshot.running:
            return "WECHAT_NOT_RUNNING", "微信未运行。"
        if not snapshot.logged_in:
            return "WECHAT_NOT_LOGGED_IN", "微信未登录。"
        if not snapshot.moments_window_ready:
            return "MOMENTS_WINDOW_NOT_READY", "朋友圈窗口未就绪。"
        return None

    @staticmethod
    def _publisher_task(task: LocalMomentsTask) -> PublisherTask:
        created = parse_datetime(task.created_at)
        return PublisherTask.model_validate(
            {
                "specVersion": "wechat-moments-publisher/task-v1",
                "taskId": task.task_id,
                "idempotencyKey": task.task_id,
                "revision": 1,
                "createdAt": created,
                "priority": 100,
                "schedule": {
                    "notBefore": task.scheduled_at,
                    "expiresAt": None,
                    "timezone": "local",
                    "misfirePolicy": "publish_asap",
                },
                "target": {
                    "platform": "wechat_moments",
                    "accountKey": task.target_account_key,
                    "visibility": {"type": "public"},
                },
                "content": {
                    "text": task.text,
                    "media": [
                        {
                            "mediaId": f"{task.task_id}-{index}",
                            "type": "image",
                            "mimeType": item["mimeType"],
                            "fileName": item["fileName"],
                            "sizeBytes": item["sizeBytes"],
                            "sha256": item["sha256"],
                            "downloadUrl": f"https://local.invalid/{task.task_id}/{index}",
                        }
                        for index, item in enumerate(task.media, start=1)
                    ],
                },
                "policy": {
                    "maxPreClickAttempts": 0,
                    "requirePostPublishConfirmation": True,
                },
                "extensions": {"origin": "local_schedule"},
            }
        )
