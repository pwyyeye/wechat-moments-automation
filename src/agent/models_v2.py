from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator

from .models import Lease, MediaItem, TaskSchedule


class ContractModelV2(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")


class PublisherV2Route(ContractModelV2):
    provider_key: str = Field(alias="providerKey", min_length=1, max_length=100)
    operation: Literal["publish", "create_draft"]
    platform: str = Field(min_length=1, max_length=50)
    account_key: str | None = Field(default=None, alias="accountKey", max_length=255)
    account_stable_id: str = Field(alias="accountStableId", min_length=1, max_length=255)
    nickname: str | None = Field(default=None, max_length=255)
    profile_id: str | None = Field(default=None, alias="profileId", max_length=128)
    executor_instance_id: str | None = Field(
        default=None,
        alias="executorInstanceId",
        max_length=128,
    )


class PublisherV2MediaItem(MediaItem):
    width: int | None = Field(default=None, ge=1)
    height: int | None = Field(default=None, ge=1)


class PublisherV2Content(ContractModelV2):
    title: str | None = None
    text: str | None = None
    markdown: str | None = None
    html: str | None = None
    hashtags: list[str] = Field(default_factory=list)
    media: list[PublisherV2MediaItem] = Field(default_factory=list)

    @model_validator(mode="after")
    def content_body_is_present(self):
        if self.text is None and self.markdown is None and self.html is None:
            raise ValueError("one of text, markdown, or html is required")
        return self


class PublisherV2Policy(ContractModelV2):
    max_pre_action_attempts: int = Field(alias="maxPreActionAttempts", ge=0)
    completion_strategy: str = Field(alias="completionStrategy", min_length=1)


class PublisherV2Task(ContractModelV2):
    spec_version: Literal["content-publisher/task-v2"] = Field(alias="specVersion")
    task_id: str = Field(alias="taskId", min_length=1, max_length=128)
    idempotency_key: str = Field(alias="idempotencyKey", min_length=1, max_length=128)
    revision: int = Field(ge=1)
    created_at: datetime = Field(alias="createdAt")
    priority: int = Field(ge=0, le=100)
    route: PublisherV2Route
    content: PublisherV2Content
    options: dict[str, Any]
    schedule: TaskSchedule
    policy: PublisherV2Policy
    extensions: dict[str, Any]


class PublisherV2ClaimResponse(ContractModelV2):
    lease: Lease
    task: PublisherV2Task
    attempt: int = Field(ge=1)
    server_time: datetime = Field(alias="serverTime")
    request_id: str = Field(alias="requestId")


class PublisherV2EventOutput(ContractModelV2):
    sync_id: str | None = Field(default=None, alias="syncId")
    post_id: str | None = Field(default=None, alias="postId")
    post_url: HttpUrl | None = Field(default=None, alias="postUrl")
    draft_only: bool | None = Field(default=None, alias="draftOnly")


class PublisherV2EventError(ContractModelV2):
    code: str = Field(min_length=1, max_length=100)
    stage: str = Field(min_length=1, max_length=100)
    retryable: bool
    message: str = Field(min_length=1, max_length=1000)


class PublisherV2EventResult(ContractModelV2):
    output: PublisherV2EventOutput | None = None
    error: PublisherV2EventError | None = None
    evidence: list[dict[str, Any]] = Field(default_factory=list, max_length=10)


class PublisherV2EventDetails(ContractModelV2):
    stage: str = Field(min_length=1, max_length=100)
    message: str = Field(min_length=1, max_length=1000)


class PublisherV2TaskEvent(ContractModelV2):
    spec_version: Literal["content-publisher/event-v2"] = Field(
        default="content-publisher/event-v2",
        alias="specVersion",
    )
    event_id: str = Field(alias="eventId", min_length=1, max_length=128)
    task_id: str = Field(alias="taskId", min_length=1, max_length=128)
    idempotency_key: str = Field(alias="idempotencyKey", min_length=1, max_length=128)
    lease_token: str = Field(alias="leaseToken", min_length=1, max_length=512)
    agent_id: str = Field(alias="agentId", min_length=1, max_length=128)
    instance_id: str = Field(alias="instanceId", min_length=1, max_length=128)
    executor_instance_id: str = Field(
        alias="executorInstanceId",
        min_length=1,
        max_length=128,
    )
    type: Literal[
        "accepted",
        "preflight_started",
        "action_started",
        "final_action_intent",
        "completion_started",
        "draft_created",
        "platform_processing",
        "published",
        "waiting_user_action",
        "failed",
        "uncertain",
    ]
    attempt: int = Field(ge=1)
    occurred_at: datetime = Field(alias="occurredAt")
    details: PublisherV2EventDetails
    result: PublisherV2EventResult | None = None

    @model_validator(mode="after")
    def validate_terminal_result(self):
        if self.type in {"draft_created", "published", "failed", "uncertain"}:
            if self.result is None:
                raise ValueError("terminal v2 events require result")
        if self.type == "draft_created" and (
            self.result is None
            or self.result.output is None
            or self.result.output.draft_only is not True
        ):
            raise ValueError("draft_created requires result.output.draftOnly=true")
        return self


class PublisherV2Capability(ContractModelV2):
    platform: str
    operations: list[Literal["publish", "create_draft"]]
    content_types: list[Literal["short_text", "image_text", "article", "video"]] = Field(
        alias="contentTypes"
    )


class PublisherV2Executor(ContractModelV2):
    executor_instance_id: str = Field(alias="executorInstanceId", min_length=1)
    provider_key: str = Field(alias="providerKey", min_length=1)
    execution_mode: Literal["windows_ui", "browser_bridge"] = Field(alias="executionMode")
    profile_id: str | None = Field(default=None, alias="profileId")
    connector_version: str | None = Field(default=None, alias="connectorVersion")
    status: Literal["ready", "degraded", "offline"]
    capabilities: list[PublisherV2Capability]
    last_error_code: str | None = Field(default=None, alias="lastErrorCode")
    last_error_message: str | None = Field(default=None, alias="lastErrorMessage")


class PublisherV2Account(ContractModelV2):
    executor_instance_id: str = Field(alias="executorInstanceId", min_length=1)
    platform: str = Field(min_length=1)
    account_stable_id: str = Field(alias="accountStableId", min_length=1)
    nickname: str = Field(min_length=1)
    account_key: str | None = Field(default=None, alias="accountKey")
    profile_id: str | None = Field(default=None, alias="profileId")
    auth_state: Literal["authenticated", "unauthenticated", "expired", "unknown"] = Field(
        alias="authState"
    )
    status: Literal["ready", "degraded", "offline"]
