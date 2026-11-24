from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from astrbot_sdk.errors import (
    InvalidPluginMetadata,
    UnsupportedMetadataVersion,
)
from astrbot_sdk.runtime.metadata import (
    load_metadata,
    parse_metadata,
)


def sdk_metadata() -> dict:
    return {
        "schema_version": 2,
        "name": "astrbot_plugin_test",
        "desc": "Test plugin",
        "author": "AstrBot",
        "version": "1.0.0",
        "runtime": {
            "api": "sdk",
            "entrypoint": "main:TestPlugin",
            "sdk_version": ">=0.1,<0.2",
        },
        "capabilities": {
            "required": [{"id": "message.receive"}],
            "optional": [
                {
                    "id": "web.route",
                    "scope": {"paths": ["/callback"]},
                }
            ],
        },
    }


@pytest.mark.parametrize("has_runtime", [False, True])
def test_metadata_requires_schema_version(has_runtime: bool) -> None:
    data = sdk_metadata()
    del data["schema_version"]
    if not has_runtime:
        del data["runtime"]
        del data["capabilities"]
    with pytest.raises(InvalidPluginMetadata, match="schema_version"):
        parse_metadata(data)


def test_unsupported_schema_is_rejected() -> None:
    with pytest.raises(UnsupportedMetadataVersion):
        parse_metadata({"schema_version": 3})


@pytest.mark.parametrize("schema_version", [True, 2.0, "2"])
def test_schema_version_must_be_integer_two(schema_version: object) -> None:
    with pytest.raises(UnsupportedMetadataVersion):
        parse_metadata({"schema_version": schema_version})


def test_parse_metadata_v2() -> None:
    metadata = parse_metadata(sdk_metadata())

    assert metadata.plugin_id == "astrbot/astrbot_plugin_test"
    assert metadata.runtime.module == "main"
    assert metadata.runtime.plugin_class == "TestPlugin"
    assert metadata.capabilities.required_ids == {"message.receive"}
    assert metadata.capabilities.optional_ids == {"web.route"}
    assert metadata.capabilities.optional[0].scope == {"paths": ["/callback"]}


def test_duplicate_capability_is_rejected() -> None:
    data = sdk_metadata()
    data["capabilities"]["optional"].append({"id": "message.receive"})

    with pytest.raises(InvalidPluginMetadata, match="duplicate"):
        parse_metadata(data)


def test_invalid_entrypoint_is_rejected() -> None:
    data = sdk_metadata()
    data["runtime"]["entrypoint"] = "../main:TestPlugin"

    with pytest.raises(InvalidPluginMetadata, match="module"):
        parse_metadata(data)


def test_invalid_capability_id_is_rejected() -> None:
    data = sdk_metadata()
    data["capabilities"]["required"] = [{"id": "Message Receive"}]

    with pytest.raises(InvalidPluginMetadata, match="capability id"):
        parse_metadata(data)


def test_runtime_language_defaults_to_python() -> None:
    metadata = parse_metadata(sdk_metadata())
    assert metadata.runtime.language == "python"


@pytest.mark.parametrize("language", ["python", "java", "go", "rust"])
def test_runtime_language_declared_values_parse(language: str) -> None:
    data = sdk_metadata()
    data["runtime"]["language"] = language
    metadata = parse_metadata(data)
    assert metadata.runtime.language == language


def test_runtime_language_rejects_unknown_value() -> None:
    data = sdk_metadata()
    data["runtime"]["language"] = "node"
    with pytest.raises(InvalidPluginMetadata, match="runtime.language"):
        parse_metadata(data)


def test_load_metadata_accepts_metadata_yml(tmp_path: Path) -> None:
    (tmp_path / "metadata.yml").write_text(
        yaml.safe_dump(sdk_metadata()),
        encoding="utf-8",
    )

    assert load_metadata(tmp_path).name == "astrbot_plugin_test"
