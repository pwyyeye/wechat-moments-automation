from contextlib import contextmanager
from datetime import timedelta

from PIL import Image

from src.agent.executor import ExecutorResult
from src.agent.local_schedule import LocalScheduleStore, LocalScheduleWorker, utc_now
from src.agent.models import AgentSnapshot


def make_image(path, color="red"):
    Image.new("RGB", (24, 24), color=color).save(path, format="PNG")
    return str(path)


def snapshot(*, nickname="番石榴", wechat_id="higuava001"):
    return AgentSnapshot(
        running=True,
        loggedIn=True,
        momentsWindowReady=True,
        wechatVersion="4.1.13.12",
        wechatNickname=nickname,
        wechatId=wechat_id,
        interactiveSession=True,
        desktopUnlocked=True,
    )


def create_due_task(store, image_path):
    return store.create(
        text="本机定时发布测试",
        image_paths=[image_path],
        scheduled_at=utc_now() - timedelta(seconds=1),
        target_account_key="wechat-main",
        target_wechat_id="higuava001",
        target_nickname="番石榴",
    )


def test_local_schedule_copies_media_and_atomically_claims_once(tmp_path):
    original = tmp_path / "original.png"
    make_image(original)
    store = LocalScheduleStore(tmp_path / "agent.db", tmp_path / "managed")

    task = create_due_task(store, str(original))
    original.unlink()

    assert task.media_paths[0] != str(original)
    assert task.media[0]["sha256"]
    assert task.media[0]["sizeBytes"] > 0
    assert store.has_due() is True
    claimed = store.claim_due()
    assert claimed is not None
    assert claimed.state == "executing"
    assert claimed.attempt == 1
    assert store.claim_due() is None


def test_local_schedule_restart_never_repeats_after_final_click_intent(tmp_path):
    image_path = make_image(tmp_path / "image.png")
    database = tmp_path / "agent.db"
    media_root = tmp_path / "managed"
    store = LocalScheduleStore(database, media_root)
    task = create_due_task(store, image_path)
    store.claim_due()
    store.record_final_click_intent(task.task_id)

    recovered = LocalScheduleStore(database, media_root).get(task.task_id)

    assert recovered.state == "uncertain"
    assert recovered.error_code == "POST_CLICK_UNCONFIRMED"
    assert LocalScheduleStore(database, media_root).has_due() is False


def test_local_schedule_failed_task_can_be_edited_and_cancelled(tmp_path):
    original = make_image(tmp_path / "first.png")
    replacement = make_image(tmp_path / "second.png", color="blue")
    store = LocalScheduleStore(tmp_path / "agent.db", tmp_path / "managed")
    task = create_due_task(store, original)
    store.claim_due()
    store.finish(task.task_id, "failed", error_code="DESKTOP_LOCKED", error_message="locked")

    edited = store.update(
        task.task_id,
        text="修改后的文案",
        image_paths=[replacement],
        scheduled_at=utc_now() + timedelta(minutes=5),
    )
    cancelled = store.cancel(task.task_id)

    assert edited.state == "pending"
    assert edited.text == "修改后的文案"
    assert edited.media[0]["fileName"] == "second.png"
    assert cancelled.state == "cancelled"


class FakePublishExecutor:
    def __init__(self, account_snapshot, *, published=True):
        self.account_snapshot = account_snapshot
        self.published = published
        self.publish_calls = 0

    def snapshot(self):
        return self.account_snapshot

    def preflight(self, task=None):
        return self.account_snapshot

    def publish(self, task, media_paths, before_final_click, after_final_click):
        self.publish_calls += 1
        before_final_click()
        after_final_click()
        return ExecutorResult(
            published=self.published,
            final_click_intent=True,
            error_message="" if self.published else "not confirmed",
        )

    def close(self):
        pass


@contextmanager
def desktop_action(*, timeout=None):
    yield


def test_local_schedule_worker_publishes_for_the_frozen_wechat_account(tmp_path):
    image_path = make_image(tmp_path / "image.png")
    store = LocalScheduleStore(tmp_path / "agent.db", tmp_path / "managed")
    task = create_due_task(store, image_path)
    executor = FakePublishExecutor(snapshot())
    worker = LocalScheduleWorker(store, executor, desktop_action)

    assert worker.run_once() is True

    finished = store.get(task.task_id)
    assert finished.state == "succeeded"
    assert finished.final_click_intent_at is not None
    assert executor.publish_calls == 1


def test_local_schedule_worker_stops_before_publish_on_account_mismatch(tmp_path):
    image_path = make_image(tmp_path / "image.png")
    store = LocalScheduleStore(tmp_path / "agent.db", tmp_path / "managed")
    task = create_due_task(store, image_path)
    executor = FakePublishExecutor(snapshot(nickname="另一个账号", wechat_id="other001"))
    worker = LocalScheduleWorker(store, executor, desktop_action)

    assert worker.run_once() is True

    failed = store.get(task.task_id)
    assert failed.state == "failed"
    assert failed.error_code == "ACCOUNT_MISMATCH"
    assert failed.final_click_intent_at is None
    assert executor.publish_calls == 0
