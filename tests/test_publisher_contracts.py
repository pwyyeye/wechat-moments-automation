import json
from pathlib import Path

import jsonschema
import pytest

CONTRACT_ROOT = Path(__file__).parents[1] / "contracts" / "publisher-agent" / "v1"
CONTRACT_V2_ROOT = Path(__file__).parents[1] / "contracts" / "publisher-agent" / "v2"


def load_json(relative_path: str):
    return json.loads((CONTRACT_ROOT / relative_path).read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    ("schema_name", "fixture_name"),
    [
        ("task.schema.json", "valid-task.json"),
        ("event.schema.json", "valid-succeeded-event.json"),
        ("event.schema.json", "valid-uncertain-event.json"),
    ],
)
def test_valid_contract_fixtures(schema_name, fixture_name):
    validator = jsonschema.Draft202012Validator(
        load_json(schema_name),
        format_checker=jsonschema.FormatChecker(),
    )

    validator.validate(load_json(f"fixtures/{fixture_name}"))


def test_windows_profile_rejects_task_without_media():
    validator = jsonschema.Draft202012Validator(
        load_json("task.schema.json"),
        format_checker=jsonschema.FormatChecker(),
    )

    with pytest.raises(jsonschema.ValidationError) as error:
        validator.validate(load_json("fixtures/invalid-task-no-media.json"))

    assert list(error.value.absolute_path) == ["content", "media"]


def test_unknown_optional_task_field_is_ignored():
    validator = jsonschema.Draft202012Validator(
        load_json("task.schema.json"),
        format_checker=jsonschema.FormatChecker(),
    )
    task = load_json("fixtures/valid-task.json")
    task["futureOptionalField"] = {"enabled": True}
    validator.validate(task)


def test_contract_mirror_matches_auto_content_when_both_repositories_are_present():
    auto_content_contract = (
        Path(__file__).parents[2]
        / "auto-content"
        / "contracts"
        / "publisher-agent"
        / "v1"
    )
    if not auto_content_contract.exists():
        pytest.skip("auto-content repository is not present beside the agent repository")

    relative_files = [
        "task.schema.json",
        "event.schema.json",
        "problem.schema.json",
        "fixtures/valid-task.json",
        "fixtures/valid-succeeded-event.json",
        "fixtures/valid-uncertain-event.json",
        "fixtures/invalid-task-no-media.json",
    ]
    for relative_file in relative_files:
        assert (CONTRACT_ROOT / relative_file).read_bytes() == (
            auto_content_contract / relative_file
        ).read_bytes()


@pytest.mark.parametrize(
    ("schema_name", "fixture_name"),
    [
        ("task.schema.json", "valid-task.json"),
        ("heartbeat.schema.json", "valid-heartbeat.json"),
        ("claim.schema.json", "valid-claim.json"),
        ("event.schema.json", "valid-draft-event.json"),
    ],
)
def test_valid_v2_contract_fixtures(schema_name, fixture_name):
    schema = json.loads((CONTRACT_V2_ROOT / schema_name).read_text(encoding="utf-8"))
    fixture = json.loads(
        (CONTRACT_V2_ROOT / "fixtures" / fixture_name).read_text(encoding="utf-8")
    )
    jsonschema.Draft202012Validator(
        schema,
        format_checker=jsonschema.FormatChecker(),
    ).validate(fixture)


def test_v2_contract_mirror_matches_auto_content_when_present():
    auto_content_contract = (
        Path(__file__).parents[2]
        / "auto-content"
        / "contracts"
        / "publisher-agent"
        / "v2"
    )
    if not auto_content_contract.exists():
        pytest.skip("auto-content repository is not present beside the agent repository")
    relative_files = [
        "README.md",
        "claim.schema.json",
        "event.schema.json",
        "heartbeat.schema.json",
        "task.schema.json",
        "fixtures/valid-claim.json",
        "fixtures/valid-draft-event.json",
        "fixtures/valid-heartbeat.json",
        "fixtures/valid-task.json",
    ]
    for relative_file in relative_files:
        assert (CONTRACT_V2_ROOT / relative_file).read_bytes() == (
            auto_content_contract / relative_file
        ).read_bytes()
