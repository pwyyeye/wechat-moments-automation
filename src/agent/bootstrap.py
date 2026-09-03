from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .config import AgentConfig, SourceConfig, save_config
from .credential_store import CredentialStore
from .sources.standard_http_v1 import (
    device_credential_ref,
    device_enrollment_marker_ref,
)


class BootstrapAuth(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    type: Literal["bearer", "api_key_header"]
    credential: str = Field(min_length=1, max_length=4096)
    header_name: str | None = Field(default=None, alias="headerName")


class BootstrapSource(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    id: str
    name: str
    type: Literal["standard-http-v1", "standard-http-v2"] | None = None
    base_url: str = Field(alias="baseUrl")
    enabled: bool = True
    weight: int = 1
    account_key: str = Field(default="wechat-main", alias="accountKey")
    auth: BootstrapAuth
    allowed_hosts: list[str] = Field(default_factory=list, alias="allowedHosts")
    allow_private_network: bool = Field(default=False, alias="allowPrivateNetwork")

    def source_config(self) -> SourceConfig:
        return SourceConfig.model_validate(
            {
                "id": self.id,
                "name": self.name,
                "type": self.type or (
                    "standard-http-v2"
                    if self.base_url.rstrip("/").endswith("/v2")
                    else "standard-http-v1"
                ),
                "baseUrl": self.base_url,
                "enabled": self.enabled,
                "weight": self.weight,
                "accountKey": self.account_key,
                "auth": {
                    "type": self.auth.type,
                    "credentialRef": f"dpapi://{self.id}",
                    **(
                        {"headerName": self.auth.header_name}
                        if self.auth.header_name
                        else {}
                    ),
                },
                "mediaSecurity": {
                    "allowedHosts": self.allowed_hosts,
                    "allowPrivateNetwork": self.allow_private_network,
                },
            }
        )


class AgentBootstrap(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    schema_version: Literal[1] = Field(alias="schemaVersion")
    sources: list[BootstrapSource] = Field(min_length=1)


def import_bootstrap(
    path: Path,
    *,
    config: AgentConfig,
    config_path: Path,
    credential_store: CredentialStore,
) -> bool:
    """Import a short-lived plaintext bundle into DPAPI-backed storage."""
    if not path.exists():
        return False

    bundle = AgentBootstrap.model_validate(json.loads(path.read_text(encoding="utf-8")))
    source_configs = [source.source_config() for source in bundle.sources]
    replacement_ids = {source.id for source in source_configs}
    existing_sources = {source.id: source for source in config.sources}

    for bootstrap_source, source_config in zip(bundle.sources, source_configs):
        existing = existing_sources.get(source_config.id)
        if existing and str(existing.base_url) != str(source_config.base_url):
            credential_store.delete(device_credential_ref(source_config.id))
            credential_store.delete(device_enrollment_marker_ref(source_config.id))
        credential_store.set(
            source_config.auth.credential_ref,
            bootstrap_source.auth.credential,
        )

    config.sources = [
        source for source in config.sources if source.id not in replacement_ids
    ] + source_configs
    save_config(config, config_path)
    path.unlink()
    return True
