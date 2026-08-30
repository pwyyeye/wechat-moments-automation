from pathlib import Path

import pytest

from src.agent.config import AgentConfig, SourceConfig, load_config, save_config


def source_payload(base_url="https://content.example.test/openapi/publisher-agent/v1"):
    return {
        "id": "source-a",
        "name": "Source A",
        "baseUrl": base_url,
        "accountKey": "wechat-main",
        "auth": {
            "type": "api_key_header",
            "headerName": "x-api-key",
            "credentialRef": "dpapi://source-a",
        },
    }


def test_config_round_trip_contains_reference_but_not_secret(tmp_path):
    path = tmp_path / "config.yaml"
    config = AgentConfig(sources=[SourceConfig.model_validate(source_payload())])
    save_config(config, path)
    restored, restored_path = load_config(path)

    assert restored_path == path
    assert restored.sources[0].auth.credential_ref == "dpapi://source-a"
    assert "secret" not in path.read_text(encoding="utf-8").lower()


def test_remote_plain_http_and_non_dpapi_credentials_are_rejected():
    with pytest.raises(ValueError, match="HTTPS"):
        SourceConfig.model_validate(
            source_payload("http://content.example.test/openapi/publisher-agent/v1")
        )
    payload = source_payload()
    payload["auth"]["credentialRef"] = "plain-secret"
    with pytest.raises(ValueError, match="dpapi"):
        SourceConfig.model_validate(payload)


def test_loopback_http_is_allowed_for_local_contract_testing():
    config = SourceConfig.model_validate(
        source_payload("http://127.0.0.1:3000/openapi/publisher-agent/v1")
    )
    assert config.base_url.scheme == "http"
