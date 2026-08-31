
import pytest

from src.agent.config import (
    DEFAULT_SOURCE_ID,
    DEFAULT_SOURCE_URL,
    AgentConfig,
    SourceConfig,
    load_config,
    save_config,
)


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


def test_default_location_auto_registers_configurable_content_source(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    configured_url = "https://publisher.example.test/openapi/publisher-agent/v1"
    monkeypatch.setenv("WECHAT_PUBLISHER_DEFAULT_SOURCE_URL", configured_url)

    config, path = load_config()

    assert path == tmp_path / "WechatPublisherAgent" / "config.yaml"
    assert len(config.sources) == 1
    assert config.sources[0].id == DEFAULT_SOURCE_ID
    assert str(config.sources[0].base_url) == configured_url
    assert config.sources[0].media_security.allowed_hosts == ["publisher.example.test"]


def test_default_location_migrates_an_existing_empty_source_list(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    path = tmp_path / "WechatPublisherAgent" / "config.yaml"
    save_config(AgentConfig(), path)

    config, _ = load_config()

    assert [source.id for source in config.sources] == [DEFAULT_SOURCE_ID]
    assert str(config.sources[0].base_url) == DEFAULT_SOURCE_URL
