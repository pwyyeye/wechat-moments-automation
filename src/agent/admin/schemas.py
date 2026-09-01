from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator

from ..config import AuthConfig, MediaSecurityConfig


class SourceUpsertRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
    name: str = Field(min_length=1, max_length=255)
    type: Literal["standard-http-v1", "standard-http-v2"] = "standard-http-v2"
    base_url: HttpUrl = Field(alias="baseUrl")
    enabled: bool = True
    weight: int = Field(default=1, ge=1, le=10)
    account_key: str = Field(alias="accountKey", min_length=1, max_length=128)
    auth: AuthConfig
    media_security: MediaSecurityConfig = Field(
        default_factory=MediaSecurityConfig,
        alias="mediaSecurity",
    )
    credential: str | None = Field(default=None, min_length=1, max_length=4096)


class AgentIdentityUpdate(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    display_name: str = Field(alias="displayName", min_length=1, max_length=255)
    account_key: str = Field(alias="accountKey", min_length=1, max_length=128)


class WechatSyncProfileUpsertRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
    name: str = Field(min_length=1, max_length=255)
    enabled: bool = True
    bridge_port: int = Field(alias="bridgePort", ge=1024, le=65534)
    platforms: list[Literal["zhihu", "juejin"]] = Field(min_length=1)
    chrome_executable: str | None = Field(default=None, alias="chromeExecutable")
    user_data_dir: str | None = Field(default=None, alias="userDataDir")
    profile_directory: str | None = Field(default=None, alias="profileDirectory")
    extension_path: str | None = Field(default=None, alias="extensionPath")
    auto_launch: bool = Field(default=False, alias="autoLaunch")


class LocalScheduleCreateRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    text: str = Field(default="", max_length=5000)
    image_paths: list[str] = Field(alias="imagePaths", min_length=1, max_length=9)
    scheduled_at: datetime = Field(alias="scheduledAt")

    @field_validator("scheduled_at")
    @classmethod
    def scheduled_at_must_include_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("scheduledAt must include a timezone offset")
        return value


class LocalScheduleUpdateRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    text: str = Field(default="", max_length=5000)
    image_paths: list[str] | None = Field(
        default=None,
        alias="imagePaths",
        min_length=1,
        max_length=9,
    )
    scheduled_at: datetime = Field(alias="scheduledAt")

    @field_validator("scheduled_at")
    @classmethod
    def scheduled_at_must_include_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("scheduledAt must include a timezone offset")
        return value
