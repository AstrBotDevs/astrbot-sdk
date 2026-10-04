from __future__ import annotations

import keyword
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Any

import yaml
from packaging.specifiers import InvalidSpecifier, SpecifierSet

from ..errors import (
    InvalidPluginMetadata,
    LegacyPluginMetadata,
    UnsupportedMetadataVersion,
)

METADATA_FILENAMES = ("metadata.yaml", "metadata.yml")
_REQUIRED_STRING_FIELDS = ("name", "desc", "version", "author")
_CAPABILITY_ID_PATTERN = re.compile(r"^[a-z][a-z0-9]*(?:\.[a-z][a-z0-9]*)+$")


class PluginAPIFamily(StrEnum):
    LEGACY = "legacy"
    SDK = "sdk"


@dataclass(frozen=True, slots=True)
class RuntimeMetadata:
    api: PluginAPIFamily
    entrypoint: str
    module: str
    plugin_class: str
    sdk_version: str


@dataclass(frozen=True, slots=True)
class CapabilityRequirement:
    id: str
    scope: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "scope", MappingProxyType(dict(self.scope)))


@dataclass(frozen=True, slots=True)
class CapabilityDeclarations:
    required: tuple[CapabilityRequirement, ...] = ()
    optional: tuple[CapabilityRequirement, ...] = ()

    @property
    def all_ids(self) -> frozenset[str]:
        return frozenset(item.id for item in (*self.required, *self.optional))

    @property
    def required_ids(self) -> frozenset[str]:
        return frozenset(item.id for item in self.required)

    @property
    def optional_ids(self) -> frozenset[str]:
        return frozenset(item.id for item in self.optional)


@dataclass(frozen=True, slots=True)
class PluginMetadata:
    schema_version: int
    name: str
    desc: str
    version: str
    author: str
    runtime: RuntimeMetadata
    capabilities: CapabilityDeclarations
    display_name: str | None = None
    short_desc: str | None = None
    repo: str | None = None
    astrbot_version: str | None = None
    support_platforms: tuple[str, ...] = ()
    pages: tuple[Mapping[str, Any], ...] = ()
    i18n: Mapping[str, Any] = field(default_factory=dict)
    extra: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "i18n", MappingProxyType(dict(self.i18n)))
        object.__setattr__(self, "extra", MappingProxyType(dict(self.extra)))

    @property
    def plugin_id(self) -> str:
        name = self.name.lower().replace("/", "_")
        author = self.author.lower().replace("/", "_")
        return f"{author}/{name}"


def find_metadata_path(plugin_root: str | Path) -> Path:
    root = Path(plugin_root)
    for filename in METADATA_FILENAMES:
        candidate = root / filename
        if candidate.is_file():
            return candidate
    raise InvalidPluginMetadata(
        f"plugin metadata not found in {root}; expected metadata.yaml"
    )


def read_metadata(plugin_root: str | Path) -> Mapping[str, Any]:
    path = find_metadata_path(plugin_root)
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise InvalidPluginMetadata(f"cannot read {path.name}: {exc}") from exc
    if not isinstance(data, Mapping):
        raise InvalidPluginMetadata(f"{path.name} must contain a YAML mapping")
    return data


def detect_api_family(data: Mapping[str, Any]) -> PluginAPIFamily:
    schema_version = data.get("schema_version")
    has_runtime = "runtime" in data
    has_capabilities = "capabilities" in data

    if schema_version is None:
        if has_runtime or has_capabilities:
            raise InvalidPluginMetadata(
                "schema_version is required when runtime or capabilities is present"
            )
        return PluginAPIFamily.LEGACY
    if type(schema_version) is not int or schema_version != 2:
        raise UnsupportedMetadataVersion(
            f"unsupported metadata schema_version: {schema_version!r}"
        )

    runtime = data.get("runtime")
    if not isinstance(runtime, Mapping) or runtime.get("api") != "sdk":
        raise InvalidPluginMetadata("metadata v2 requires runtime.api to be 'sdk'")
    return PluginAPIFamily.SDK


def _required_string(data: Mapping[str, Any], field_name: str) -> str:
    value = data.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise InvalidPluginMetadata(
            f"metadata field {field_name!r} must be a non-empty string"
        )
    return value.strip()


def _optional_string(data: Mapping[str, Any], field_name: str) -> str | None:
    value = data.get(field_name)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise InvalidPluginMetadata(
            f"metadata field {field_name!r} must be a non-empty string"
        )
    return value.strip()


def _validate_specifier(value: str, field_name: str) -> str:
    try:
        SpecifierSet(value)
    except InvalidSpecifier as exc:
        raise InvalidPluginMetadata(
            f"{field_name} must be a valid PEP 440 version range"
        ) from exc
    return value


def _parse_runtime(data: Mapping[str, Any]) -> RuntimeMetadata:
    runtime = data.get("runtime")
    if not isinstance(runtime, Mapping):
        raise InvalidPluginMetadata("runtime must be a mapping")

    unknown = set(runtime) - {"api", "entrypoint", "sdk_version"}
    if unknown:
        raise InvalidPluginMetadata(
            f"unknown runtime fields: {', '.join(sorted(map(str, unknown)))}"
        )

    entrypoint = _required_string(runtime, "entrypoint")
    if entrypoint.count(":") != 1:
        raise InvalidPluginMetadata(
            "runtime.entrypoint must use '<module>:<PluginClass>'"
        )
    module, plugin_class = entrypoint.split(":", 1)
    if not module or any(not part.isidentifier() for part in module.split(".")):
        raise InvalidPluginMetadata("runtime.entrypoint contains an invalid module")
    if not plugin_class or any(
        not part.isidentifier() for part in plugin_class.split(".")
    ):
        raise InvalidPluginMetadata("runtime.entrypoint contains an invalid class name")

    sdk_version = _validate_specifier(
        _required_string(runtime, "sdk_version"),
        "runtime.sdk_version",
    )
    return RuntimeMetadata(
        api=PluginAPIFamily.SDK,
        entrypoint=entrypoint,
        module=module,
        plugin_class=plugin_class,
        sdk_version=sdk_version,
    )


def _parse_capability_group(
    capabilities: Mapping[str, Any],
    group_name: str,
) -> tuple[CapabilityRequirement, ...]:
    raw_group = capabilities.get(group_name, ())
    if not isinstance(raw_group, list | tuple):
        raise InvalidPluginMetadata(f"capabilities.{group_name} must be a list")

    result: list[CapabilityRequirement] = []
    for index, item in enumerate(raw_group):
        if not isinstance(item, Mapping):
            raise InvalidPluginMetadata(
                f"capabilities.{group_name}[{index}] must be a mapping"
            )
        unknown = set(item) - {"id", "scope"}
        if unknown:
            raise InvalidPluginMetadata(
                f"unknown fields in capabilities.{group_name}[{index}]: "
                f"{', '.join(sorted(map(str, unknown)))}"
            )
        capability_id = _required_string(item, "id")
        if not _CAPABILITY_ID_PATTERN.fullmatch(capability_id):
            raise InvalidPluginMetadata(f"invalid capability id: {capability_id!r}")
        scope = item.get("scope", {})
        if not isinstance(scope, Mapping):
            raise InvalidPluginMetadata(
                f"capabilities.{group_name}[{index}].scope must be a mapping"
            )
        result.append(CapabilityRequirement(capability_id, scope))
    return tuple(result)


def _parse_capabilities(data: Mapping[str, Any]) -> CapabilityDeclarations:
    capabilities = data.get("capabilities", {})
    if not isinstance(capabilities, Mapping):
        raise InvalidPluginMetadata("capabilities must be a mapping")
    unknown = set(capabilities) - {"required", "optional"}
    if unknown:
        raise InvalidPluginMetadata(
            f"unknown capabilities fields: {', '.join(sorted(map(str, unknown)))}"
        )

    required = _parse_capability_group(capabilities, "required")
    optional = _parse_capability_group(capabilities, "optional")
    ids = [item.id for item in (*required, *optional)]
    duplicates = sorted(
        capability_id for capability_id in set(ids) if ids.count(capability_id) > 1
    )
    if duplicates:
        raise InvalidPluginMetadata(
            f"duplicate capability declarations: {', '.join(duplicates)}"
        )
    return CapabilityDeclarations(required=required, optional=optional)


def parse_metadata(data: Mapping[str, Any]) -> PluginMetadata:
    family = detect_api_family(data)
    if family is PluginAPIFamily.LEGACY:
        raise LegacyPluginMetadata("metadata belongs to a legacy plugin")

    normalized = dict(data)
    if "desc" not in normalized and "description" in normalized:
        normalized["desc"] = normalized["description"]
    for field_name in _REQUIRED_STRING_FIELDS:
        _required_string(normalized, field_name)
    name = _required_string(normalized, "name")
    if not name.isidentifier() or keyword.iskeyword(name):
        raise InvalidPluginMetadata(
            "metadata field 'name' must be an importable Python identifier"
        )

    astrbot_version = _optional_string(normalized, "astrbot_version")
    if astrbot_version:
        _validate_specifier(astrbot_version, "astrbot_version")

    support_platforms = normalized.get("support_platforms", ())
    if not isinstance(support_platforms, list | tuple) or not all(
        isinstance(item, str) and item for item in support_platforms
    ):
        raise InvalidPluginMetadata(
            "support_platforms must be a list of non-empty strings"
        )

    pages = normalized.get("pages", ())
    if not isinstance(pages, list | tuple) or not all(
        isinstance(item, Mapping) for item in pages
    ):
        raise InvalidPluginMetadata("pages must be a list of mappings")
    immutable_pages = tuple(MappingProxyType(dict(item)) for item in pages)

    i18n = normalized.get("i18n", {})
    if not isinstance(i18n, Mapping):
        raise InvalidPluginMetadata("i18n must be a mapping")

    known_fields = {
        "schema_version",
        "name",
        "display_name",
        "desc",
        "description",
        "short_desc",
        "author",
        "version",
        "repo",
        "astrbot_version",
        "support_platforms",
        "pages",
        "i18n",
        "runtime",
        "capabilities",
    }
    extra = {key: value for key, value in normalized.items() if key not in known_fields}

    return PluginMetadata(
        schema_version=2,
        name=name,
        display_name=_optional_string(normalized, "display_name"),
        desc=_required_string(normalized, "desc"),
        short_desc=_optional_string(normalized, "short_desc"),
        author=_required_string(normalized, "author"),
        version=_required_string(normalized, "version"),
        repo=_optional_string(normalized, "repo"),
        astrbot_version=astrbot_version,
        support_platforms=tuple(support_platforms),
        pages=immutable_pages,
        i18n=i18n,
        runtime=_parse_runtime(normalized),
        capabilities=_parse_capabilities(normalized),
        extra=extra,
    )


def load_metadata(plugin_root: str | Path) -> PluginMetadata:
    return parse_metadata(read_metadata(plugin_root))
