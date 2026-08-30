from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, HttpUrl

from ..config import AuthConfig, MediaSecurityConfig


class SourceUpsertRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
    name: str = Field(min_length=1, max_length=255)
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
