from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator


class ContractModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")


class MediaItem(ContractModel):
    media_id: str = Field(alias="mediaId", min_length=1, max_length=128)
    type: Literal["image"]
    mime_type: Literal["image/jpeg", "image/png"] = Field(alias="mimeType")
    file_name: str = Field(alias="fileName", min_length=1, max_length=255)
    size_bytes: int = Field(alias="sizeBytes", gt=0, le=20 * 1024 * 1024)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    download_url: HttpUrl = Field(alias="downloadUrl")


class TaskSchedule(ContractModel):
    not_before: datetime = Field(alias="notBefore")
    expires_at: datetime | None = Field(alias="expiresAt")
    timezone: str = Field(min_length=1, max_length=100)
    misfire_policy: Literal["skip", "publish_asap", "manual"] = Field(
        alias="misfirePolicy"
    )


class TaskTarget(ContractModel):
    platform: Literal["wechat_moments"]
    account_key: str = Field(alias="accountKey", min_length=1, max_length=128)
    visibility: dict[str, Literal["public"]]

    @field_validator("visibility")
    @classmethod
    def validate_visibility(cls, value):
        if value != {"type": "public"}:
            raise ValueError("only public visibility is supported in v1")
        return value


class TaskContent(ContractModel):
    text: str = Field(max_length=5000)
    media: list[MediaItem] = Field(min_length=1, max_length=9)


class TaskPolicy(ContractModel):
    max_pre_click_attempts: int = Field(alias="maxPreClickAttempts", ge=0, le=2)
    require_post_publish_confirmation: Literal[True] = Field(
        alias="requirePostPublishConfirmation"
    )


class PublisherTask(ContractModel):
    spec_version: Literal["wechat-moments-publisher/task-v1"] = Field(
        alias="specVersion"
    )
    task_id: str = Field(alias="taskId", min_length=1, max_length=128)
    idempotency_key: str = Field(alias="idempotencyKey", min_length=1, max_length=128)
    revision: int = Field(ge=1)
    created_at: datetime = Field(alias="createdAt")
    priority: int = Field(ge=0, le=100)
    schedule: TaskSchedule
    target: TaskTarget
    content: TaskContent
    policy: TaskPolicy
    extensions: dict[str, Any]


class Lease(ContractModel):
    token: str = Field(min_length=16, max_length=512)
    expires_at: datetime = Field(alias="expiresAt")
    renew_after_seconds: int = Field(alias="renewAfterSeconds", ge=5)


class ClaimResponse(ContractModel):
    lease: Lease
    task: PublisherTask
    attempt: int = Field(ge=1)
    server_time: datetime = Field(alias="serverTime")
    request_id: str = Field(alias="requestId")


class Confirmation(ContractModel):
    mode: Literal["feed_text_ocr"]
    state: Literal["confirmed", "unconfirmed", "not_attempted"]
    matched_text_hash: str | None = Field(
        default=None,
        alias="matchedTextHash",
        pattern=r"^[a-f0-9]{64}$",
    )


class EventError(ContractModel):
    code: str = Field(pattern=r"^[A-Z][A-Z0-9_]*$", max_length=100)
    stage: str = Field(max_length=100)
    retryable: bool
    message: str = Field(max_length=1000)


class Evidence(ContractModel):
    type: Literal["ocr_digest", "screenshot"]
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    storage_ref: str | None = Field(default=None, alias="storageRef", max_length=500)


class EventResult(ContractModel):
    confirmation: Confirmation | None = None
    error: EventError | None = None
    evidence: list[Evidence] = Field(default_factory=list, max_length=10)


class EventDetails(ContractModel):
    stage: Literal[
        "claim",
        "validation",
        "download",
        "preflight",
        "editor",
        "before_final_click",
        "confirmation",
        "report",
    ]
    message: str = Field(max_length=1000)


class TaskEvent(ContractModel):
    spec_version: Literal["wechat-moments-publisher/event-v1"] = Field(
        default="wechat-moments-publisher/event-v1",
        alias="specVersion",
    )
    event_id: str = Field(alias="eventId", min_length=1, max_length=128)
    task_id: str = Field(alias="taskId", min_length=1, max_length=128)
    idempotency_key: str = Field(alias="idempotencyKey", min_length=1, max_length=128)
    lease_token: str = Field(alias="leaseToken", min_length=16, max_length=512)
    agent_id: str = Field(alias="agentId", min_length=1, max_length=128)
    instance_id: str = Field(alias="instanceId", min_length=1, max_length=128)
    type: Literal[
        "accepted",
        "preflight_started",
        "publish_started",
        "final_click_intent",
        "confirmation_started",
        "succeeded",
        "failed",
        "uncertain",
    ]
    attempt: int = Field(ge=1)
    occurred_at: datetime = Field(alias="occurredAt")
    details: EventDetails
    result: EventResult | None = None

    @model_validator(mode="after")
    def terminal_events_require_result(self):
        if self.type in {"succeeded", "failed", "uncertain"} and self.result is None:
            raise ValueError("terminal events require result")
        return self


class AgentCapabilities(ContractModel):
    platforms: list[str] = Field(default_factory=lambda: ["wechat_moments"])
    visibility_types: list[str] = Field(
        default_factory=lambda: ["public"], alias="visibilityTypes"
    )
    media_types: list[str] = Field(
        default_factory=lambda: ["image/jpeg", "image/png"], alias="mediaTypes"
    )
    min_media_count: int = Field(default=1, alias="minMediaCount")
    max_media_count: int = Field(default=9, alias="maxMediaCount")
    max_media_bytes_per_item: int = Field(
        default=20 * 1024 * 1024, alias="maxMediaBytesPerItem"
    )
    max_media_bytes_per_task: int = Field(
        default=60 * 1024 * 1024, alias="maxMediaBytesPerTask"
    )
    max_text_code_points: int = Field(default=5000, alias="maxTextCodePoints")
    confirmation_modes: list[str] = Field(
        default_factory=lambda: ["feed_text_ocr"], alias="confirmationModes"
    )


class AgentSnapshot(ContractModel):
    running: bool
    logged_in: bool = Field(alias="loggedIn")
    moments_window_ready: bool = Field(alias="momentsWindowReady")
    wechat_version: str = Field(alias="wechatVersion")
    interactive_session: bool = Field(alias="interactiveSession")
    desktop_unlocked: bool = Field(alias="desktopUnlocked")


class LocalClaim:
    def __init__(self, source_id: str, response: ClaimResponse):
        self.source_id = source_id
        self.response = response

    @property
    def task(self) -> PublisherTask:
        return self.response.task

    @property
    def lease(self) -> Lease:
        return self.response.lease
