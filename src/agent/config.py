from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Literal
from uuid import uuid4

import yaml
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator


def default_data_root() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "WechatPublisherAgent"
    return Path.home() / ".wechat-publisher-agent"


class AuthConfig(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    type: Literal["bearer", "api_key_header"]
    credential_ref: str = Field(alias="credentialRef")
    header_name: str | None = Field(default=None, alias="headerName")

    @field_validator("header_name")
    @classmethod
    def validate_header_name(cls, value, info):
        auth_type = info.data.get("type")
        if auth_type == "api_key_header" and not value:
            raise ValueError("api_key_header requires headerName")
        if value and not value.replace("-", "").isalnum():
            raise ValueError("headerName contains unsupported characters")
        return value

    @field_validator("credential_ref")
    @classmethod
    def credential_reference_uses_dpapi(cls, value):
        if not value.startswith("dpapi://"):
            raise ValueError("credentialRef must use dpapi://")
        return value


class MediaSecurityConfig(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    allowed_hosts: list[str] = Field(default_factory=list, alias="allowedHosts")
    allow_private_network: bool = Field(default=False, alias="allowPrivateNetwork")

    @field_validator("allowed_hosts")
    @classmethod
    def validate_allowed_hosts(cls, value):
        for host in value:
            if (
                not host
                or "://" in host
                or "/" in host
                or "@" in host
                or host.startswith(".")
            ):
                raise ValueError("allowedHosts entries must be exact hostnames")
        return value


class SourceConfig(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
    name: str = Field(min_length=1, max_length=255)
    type: Literal["standard-http-v1"] = "standard-http-v1"
    base_url: HttpUrl = Field(alias="baseUrl")
    enabled: bool = True
    weight: int = Field(default=1, ge=1, le=10)
    account_key: str = Field(alias="accountKey", min_length=1, max_length=128)
    auth: AuthConfig
    media_security: MediaSecurityConfig = Field(
        default_factory=MediaSecurityConfig,
        alias="mediaSecurity",
    )

    @model_validator(mode="after")
    def require_secure_transport(self):
        hostname = (self.base_url.host or "").lower()
        if self.base_url.scheme != "https" and hostname not in {
            "127.0.0.1",
            "localhost",
            "::1",
        }:
            raise ValueError("baseUrl must use HTTPS except for loopback testing")
        return self


class AgentIdentityConfig(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    id: str = Field(default_factory=lambda: f"agent-{uuid4().hex[:12]}")
    display_name: str = Field(default=os.environ.get("COMPUTERNAME", "Windows Publisher"), alias="displayName")
    account_key: str = Field(default="wechat-main", alias="accountKey")


class RuntimeConfig(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    heartbeat_seconds: int = Field(default=15, alias="heartbeatSeconds", ge=5, le=60)
    default_lease_seconds: int = Field(default=180, alias="defaultLeaseSeconds", ge=30, le=600)
    poll_seconds: int = Field(default=2, alias="pollSeconds", ge=1, le=30)
    media_cache_max_mib: int = Field(default=1024, alias="mediaCacheMaxMiB", ge=64)
    local_admin_host: Literal["127.0.0.1"] = Field(default="127.0.0.1", alias="localAdminHost")
    local_admin_port: int = Field(default=17821, alias="localAdminPort", ge=1024, le=65535)


class AgentConfig(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    schema_version: Literal[1] = Field(default=1, alias="schemaVersion")
    agent: AgentIdentityConfig = Field(default_factory=AgentIdentityConfig)
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)
    sources: list[SourceConfig] = Field(default_factory=list)

    @field_validator("sources")
    @classmethod
    def source_ids_are_unique(cls, value):
        source_ids = [source.id for source in value]
        if len(source_ids) != len(set(source_ids)):
            raise ValueError("source ids must be unique")
        return value


def load_config(path: Path | str | None = None) -> tuple[AgentConfig, Path]:
    config_path = Path(path) if path else default_data_root() / "config.yaml"
    if config_path.exists():
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        return AgentConfig.model_validate(raw), config_path

    config = AgentConfig()
    save_config(config, config_path)
    return config, config_path


def save_config(config: AgentConfig, path: Path | str) -> None:
    config_path = Path(path)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    serialized = yaml.safe_dump(
        config.model_dump(by_alias=True, mode="json"),
        allow_unicode=True,
        sort_keys=False,
    )
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        newline="\n",
        dir=config_path.parent,
        delete=False,
    ) as temporary:
        temporary.write(serialized)
        temporary_path = Path(temporary.name)
    temporary_path.replace(config_path)
