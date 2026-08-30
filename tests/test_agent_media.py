import hashlib
import json
from datetime import datetime, timezone

import httpx
import pytest

from src.agent.config import SourceConfig
from src.agent.media_cache import MediaCache, MediaDownloadError
from src.agent.models import PublisherTask


def source(*, allowed_hosts=None, allow_private=False):
    return SourceConfig.model_validate(
        {
            "id": "source-a",
            "name": "Source A",
            "baseUrl": "https://source.example.test/openapi/publisher-agent/v1",
            "accountKey": "wechat-main",
            "auth": {
                "type": "api_key_header",
                "headerName": "x-api-key",
                "credentialRef": "dpapi://source-a",
            },
            "mediaSecurity": {
                "allowedHosts": allowed_hosts or ["media.example.test"],
                "allowPrivateNetwork": allow_private,
            },
        }
    )


def task(content=b"image", **media_overrides):
    media = {
        "mediaId": "media-1",
        "type": "image",
        "mimeType": "image/jpeg",
        "fileName": "test.jpg",
        "sizeBytes": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
        "downloadUrl": "https://media.example.test/test.jpg",
        **media_overrides,
    }
    return PublisherTask.model_validate(
        {
            "specVersion": "wechat-moments-publisher/task-v1",
            "taskId": "task-1",
            "idempotencyKey": "publish-1",
            "revision": 1,
            "createdAt": datetime.now(timezone.utc).isoformat(),
            "priority": 50,
            "schedule": {
                "notBefore": datetime.now(timezone.utc).isoformat(),
                "expiresAt": None,
                "timezone": "Asia/Shanghai",
                "misfirePolicy": "manual",
            },
            "target": {
                "platform": "wechat_moments",
                "accountKey": "wechat-main",
                "visibility": {"type": "public"},
            },
            "content": {"text": "test", "media": [media]},
            "policy": {
                "maxPreClickAttempts": 2,
                "requirePostPublishConfirmation": True,
            },
            "extensions": {},
        }
    )


def cache(tmp_path, handler, *, resolver=lambda host: ["93.184.216.34"]):
    return MediaCache(
        tmp_path,
        64,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        address_resolver=resolver,
    )


def test_media_download_validates_and_caches_sha256(tmp_path):
    content = b"valid-image"
    media_cache = cache(
        tmp_path,
        lambda request: httpx.Response(
            200,
            headers={"Content-Type": "image/jpeg", "Content-Length": str(len(content))},
            content=content,
        ),
    )
    paths = media_cache.download_task(source(), task(content))
    assert len(paths) == 1
    assert open(paths[0], "rb").read() == content


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"mimeType": "image/png"}, "MEDIA_MIME_MISMATCH"),
        ({"sha256": "a" * 64}, "MEDIA_HASH_MISMATCH"),
        ({"sizeBytes": 3}, "MEDIA_SIZE_MISMATCH"),
    ],
)
def test_media_metadata_mismatch_is_rejected(tmp_path, overrides, code):
    content = b"valid-image"
    media_cache = cache(
        tmp_path,
        lambda request: httpx.Response(
            200,
            headers={"Content-Type": "image/jpeg"},
            content=content,
        ),
    )
    with pytest.raises(MediaDownloadError) as error:
        media_cache.download_task(source(), task(content, **overrides))
    assert error.value.code == code


def test_media_host_and_private_address_are_blocked(tmp_path):
    media_cache = cache(
        tmp_path,
        lambda request: httpx.Response(200, content=b"image"),
        resolver=lambda host: ["127.0.0.1"],
    )
    with pytest.raises(MediaDownloadError) as error:
        media_cache.download_task(source(), task())
    assert error.value.code == "MEDIA_PRIVATE_ADDRESS_BLOCKED"

    with pytest.raises(MediaDownloadError) as error:
        media_cache.download_task(
            source(allowed_hosts=["other.example.test"]),
            task(),
        )
    assert error.value.code == "MEDIA_HOST_NOT_ALLOWED"
