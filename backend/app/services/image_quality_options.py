from copy import deepcopy
from typing import Any

from ..exceptions import DomainValidationError


IMAGE_QUALITY_OPTIONS_KEY = "image_quality_options"
QUALITY_KEY_PARAMETER = "quality_key"
_MAX_OPTIONS = 32
_MAX_KEY_LENGTH = 64
_MAX_DISPLAY_NAME_LENGTH = 80
_MAX_PROVIDER_PARAMETER_LENGTH = 80
_RESERVED_PARAMETER_NAMES = {"prompt", "model", "_input_references", QUALITY_KEY_PARAMETER}


def _require_object(value: Any, field: str) -> dict:
    if not isinstance(value, dict):
        raise DomainValidationError(f"{field} must be an object")
    return value


def _require_string(value: Any, field: str, *, max_length: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DomainValidationError(f"{field} must be a non-empty string")
    normalized = value.strip()
    if len(normalized) > max_length:
        raise DomainValidationError(f"{field} cannot exceed {max_length} characters")
    return normalized


def _matches_schema(value: Any, definition: dict) -> bool:
    expected = definition.get("type")
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    return expected is None


def validate_image_quality_options(
    value: Any,
    config_schema: Any,
    default_parameters: Any,
) -> dict:
    """Validate admin-configured UI quality choices and return a normalized lookup."""

    quality = _require_object(value, f"metadata.{IMAGE_QUALITY_OPTIONS_KEY}")
    if quality.get("schema_version") != 1:
        raise DomainValidationError(
            f"metadata.{IMAGE_QUALITY_OPTIONS_KEY}.schema_version must be 1"
        )
    provider_parameter = _require_string(
        quality.get("provider_parameter", quality.get("parameter_name")),
        f"metadata.{IMAGE_QUALITY_OPTIONS_KEY}.provider_parameter",
        max_length=_MAX_PROVIDER_PARAMETER_LENGTH,
    )
    legacy_parameter = quality.get("parameter_name")
    if (
        "provider_parameter" in quality
        and legacy_parameter is not None
        and legacy_parameter != provider_parameter
    ):
        raise DomainValidationError(
            f"metadata.{IMAGE_QUALITY_OPTIONS_KEY}.provider_parameter conflicts with the legacy field"
        )
    if provider_parameter in _RESERVED_PARAMETER_NAMES:
        raise DomainValidationError(
            f"metadata.{IMAGE_QUALITY_OPTIONS_KEY}.provider_parameter is reserved"
        )
    default_key = _require_string(
        quality.get("default_key"),
        f"metadata.{IMAGE_QUALITY_OPTIONS_KEY}.default_key",
        max_length=_MAX_KEY_LENGTH,
    ).casefold()
    options = quality.get("options")
    if not isinstance(options, list) or not options:
        raise DomainValidationError(
            f"metadata.{IMAGE_QUALITY_OPTIONS_KEY}.options must be a non-empty array"
        )
    if len(options) > _MAX_OPTIONS:
        raise DomainValidationError(
            f"metadata.{IMAGE_QUALITY_OPTIONS_KEY}.options cannot contain more than {_MAX_OPTIONS} items"
        )

    schema = _require_object(config_schema, "config_schema")
    properties = schema.get("properties")
    if not isinstance(properties, dict) or not isinstance(
        properties.get(provider_parameter), dict
    ):
        raise DomainValidationError(
            f"config_schema.properties.{provider_parameter} must define the quality parameter"
        )
    definition = properties[provider_parameter]

    lookup: dict[str, dict] = {}
    for index, raw_option in enumerate(options):
        prefix = f"metadata.{IMAGE_QUALITY_OPTIONS_KEY}.options[{index}]"
        option = _require_object(raw_option, prefix)
        key = _require_string(
            option.get("key"), f"{prefix}.key", max_length=_MAX_KEY_LENGTH
        ).casefold()
        if key in lookup:
            raise DomainValidationError(
                f"metadata.{IMAGE_QUALITY_OPTIONS_KEY}.options contains duplicate key: {key}"
            )
        display_name = _require_string(
            option.get("display_name"),
            f"{prefix}.display_name",
            max_length=_MAX_DISPLAY_NAME_LENGTH,
        )
        if "provider_value" not in option:
            raise DomainValidationError(f"{prefix}.provider_value is required")
        provider_value = option["provider_value"]
        if not _matches_schema(provider_value, definition):
            raise DomainValidationError(
                f"{prefix}.provider_value has an invalid type for {provider_parameter}"
            )
        if "enum" in definition and provider_value not in definition["enum"]:
            raise DomainValidationError(
                f"{prefix}.provider_value is not allowed by config_schema"
            )
        lookup[key] = {
            "key": key,
            "display_name": display_name,
            "provider_value": provider_value,
        }

    if default_key not in lookup:
        raise DomainValidationError(
            f"metadata.{IMAGE_QUALITY_OPTIONS_KEY}.default_key must reference an option"
        )
    defaults = _require_object(default_parameters, "default_parameters")
    if defaults.get(provider_parameter) != lookup[default_key]["provider_value"]:
        raise DomainValidationError(
            f"default_parameters.{provider_parameter} must match the default image quality option"
        )
    return {
        "provider_parameter": provider_parameter,
        "default_key": default_key,
        "options": lookup,
    }


def normalize_image_quality_metadata(
    *,
    kind: str,
    config_schema: Any,
    default_parameters: Any,
    metadata: Any,
    explicitly_submitted: bool,
) -> dict:
    normalized = deepcopy(_require_object(metadata, "metadata"))
    if kind != "image":
        if IMAGE_QUALITY_OPTIONS_KEY in normalized and explicitly_submitted:
            raise DomainValidationError(
                "metadata.image_quality_options is only supported for image models"
            )
        normalized.pop(IMAGE_QUALITY_OPTIONS_KEY, None)
        return normalized
    if IMAGE_QUALITY_OPTIONS_KEY in normalized:
        configured = validate_image_quality_options(
            normalized[IMAGE_QUALITY_OPTIONS_KEY], config_schema, default_parameters
        )
        quality = deepcopy(normalized[IMAGE_QUALITY_OPTIONS_KEY])
        quality.pop("parameter_name", None)
        quality["provider_parameter"] = configured["provider_parameter"]
        quality["default_key"] = configured["default_key"]
        quality["options"] = list(configured["options"].values())
        normalized[IMAGE_QUALITY_OPTIONS_KEY] = quality
    return normalized


def image_quality_lookup(provider: dict) -> dict | None:
    metadata = provider.get("metadata") or {}
    if not isinstance(metadata, dict) or IMAGE_QUALITY_OPTIONS_KEY not in metadata:
        return None
    return validate_image_quality_options(
        metadata[IMAGE_QUALITY_OPTIONS_KEY],
        provider.get("config_schema") or {},
        provider.get("default_parameters") or {},
    )


def resolve_image_quality_parameters(provider: dict, supplied: dict) -> dict:
    if not isinstance(supplied, dict):
        raise DomainValidationError("Generation parameters must be an object")
    normalized = dict(supplied)
    has_quality_key = QUALITY_KEY_PARAMETER in normalized
    if provider.get("kind") != "image":
        if has_quality_key:
            raise DomainValidationError("quality_key is only supported for image models")
        return normalized

    configured = image_quality_lookup(provider)
    if configured is None:
        if has_quality_key:
            raise DomainValidationError(
                "The selected image model does not support semantic quality parameters"
            )
        return normalized
    if not has_quality_key:
        return normalized

    raw_key = normalized.pop(QUALITY_KEY_PARAMETER)
    key = _require_string(raw_key, QUALITY_KEY_PARAMETER, max_length=_MAX_KEY_LENGTH).casefold()
    option = configured["options"].get(key)
    if option is None:
        raise DomainValidationError("Unsupported image quality option")
    provider_parameter = configured["provider_parameter"]
    provider_value = option["provider_value"]
    if (
        provider_parameter in normalized
        and normalized[provider_parameter] != provider_value
    ):
        raise DomainValidationError(
            f"{QUALITY_KEY_PARAMETER} conflicts with {provider_parameter}"
        )
    normalized[provider_parameter] = provider_value
    return normalized
