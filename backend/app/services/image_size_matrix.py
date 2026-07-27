import re
from copy import deepcopy
from math import gcd
from typing import Any

from ..exceptions import DomainValidationError


IMAGE_SIZE_MATRIX_KEY = "image_size_matrix"
RESOLUTION_KEY_PARAMETER = "resolution_key"
ASPECT_RATIO_KEY_PARAMETER = "aspect_ratio_key"
LEGACY_RESOLUTION_PARAMETER = "resolution"
LEGACY_ASPECT_RATIO_PARAMETER = "aspect_ratio"
SEMANTIC_SIZE_PARAMETERS = {
    RESOLUTION_KEY_PARAMETER,
    ASPECT_RATIO_KEY_PARAMETER,
}
IMAGE_SIZE_UI_PARAMETERS = {
    RESOLUTION_KEY_PARAMETER,
    ASPECT_RATIO_KEY_PARAMETER,
    LEGACY_RESOLUTION_PARAMETER,
    LEGACY_ASPECT_RATIO_PARAMETER,
}
_SIZE_PATTERN = re.compile(r"^([1-9]\d{0,4})x([1-9]\d{0,4})$")
_MAX_IMAGE_SIDE = 32768
_MAX_MATRIX_ITEMS = 32
_MAX_KEY_LENGTH = 64
_MAX_DISPLAY_NAME_LENGTH = 80


def _require_object(value: Any, field: str) -> dict:
    if not isinstance(value, dict):
        raise DomainValidationError(f"{field} must be an object")
    return value


def _require_non_empty_string(
    value: Any, field: str, *, max_length: int | None = None
) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DomainValidationError(f"{field} must be a non-empty string")
    if max_length is not None and len(value.strip()) > max_length:
        raise DomainValidationError(f"{field} cannot exceed {max_length} characters")
    return value


def _normalized_key(value: Any, field: str) -> str:
    return _require_non_empty_string(
        value, field, max_length=_MAX_KEY_LENGTH
    ).strip().casefold()


def _normalized_ratio_key(value: Any, field: str) -> str:
    normalized = _normalized_key(value, field)
    match = re.fullmatch(r"([1-9]\d*):([1-9]\d*)", normalized)
    if match is None:
        return normalized
    width, height = (int(component) for component in match.groups())
    divisor = gcd(width, height)
    return f"{width // divisor}:{height // divisor}"


def _validate_size(value: Any, field: str) -> str:
    size = _require_non_empty_string(value, field)
    match = _SIZE_PATTERN.fullmatch(size)
    if match is None:
        raise DomainValidationError(f"{field} must use the positive WIDTHxHEIGHT format")
    width, height = (int(component) for component in match.groups())
    if width > _MAX_IMAGE_SIDE or height > _MAX_IMAGE_SIDE:
        raise DomainValidationError(f"{field} cannot exceed 32768 pixels per side")
    return size


def is_valid_concrete_image_size(value: Any) -> bool:
    """Return whether a persisted provider size is safe to use as a frozen value."""

    try:
        _validate_size(value, "size")
    except DomainValidationError:
        return False
    return True


def validate_image_size_matrix(
    matrix: Any,
    config_schema: Any,
    default_parameters: Any,
) -> dict[tuple[str, str], str]:
    """Validate the authoritative image-size matrix and return its lookup table."""

    matrix = _require_object(matrix, f"metadata.{IMAGE_SIZE_MATRIX_KEY}")
    if type(matrix.get("schema_version")) is not int or matrix["schema_version"] != 1:
        raise DomainValidationError(
            f"metadata.{IMAGE_SIZE_MATRIX_KEY}.schema_version must be 1"
        )
    default_resolution_key = _normalized_key(
        matrix.get("default_resolution_key"),
        f"metadata.{IMAGE_SIZE_MATRIX_KEY}.default_resolution_key",
    )
    default_aspect_ratio_key = _normalized_ratio_key(
        matrix.get("default_aspect_ratio_key"),
        f"metadata.{IMAGE_SIZE_MATRIX_KEY}.default_aspect_ratio_key",
    )
    resolutions = matrix.get("resolutions")
    if not isinstance(resolutions, list) or not resolutions:
        raise DomainValidationError(
            f"metadata.{IMAGE_SIZE_MATRIX_KEY}.resolutions must be a non-empty array"
        )
    if len(resolutions) > _MAX_MATRIX_ITEMS:
        raise DomainValidationError(
            f"metadata.{IMAGE_SIZE_MATRIX_KEY}.resolutions cannot contain more than 32 items"
        )

    lookup: dict[tuple[str, str], str] = {}
    resolution_keys: set[str] = set()
    for resolution_index, resolution_value in enumerate(resolutions):
        prefix = f"metadata.{IMAGE_SIZE_MATRIX_KEY}.resolutions[{resolution_index}]"
        resolution = _require_object(resolution_value, prefix)
        resolution_key = _normalized_key(resolution.get("key"), f"{prefix}.key")
        _require_non_empty_string(
            resolution.get("display_name"),
            f"{prefix}.display_name",
            max_length=_MAX_DISPLAY_NAME_LENGTH,
        )
        if resolution_key in resolution_keys:
            raise DomainValidationError(
                f"Duplicate image resolution key: {resolution_key}"
            )
        resolution_keys.add(resolution_key)

        ratios = resolution.get("ratios")
        if not isinstance(ratios, list) or not ratios:
            raise DomainValidationError(f"{prefix}.ratios must be a non-empty array")
        if len(ratios) > _MAX_MATRIX_ITEMS:
            raise DomainValidationError(f"{prefix}.ratios cannot contain more than 32 items")
        ratio_keys: set[str] = set()
        for ratio_index, ratio_value in enumerate(ratios):
            ratio_prefix = f"{prefix}.ratios[{ratio_index}]"
            ratio = _require_object(ratio_value, ratio_prefix)
            ratio_key = _normalized_ratio_key(ratio.get("key"), f"{ratio_prefix}.key")
            _require_non_empty_string(
                ratio.get("display_name"),
                f"{ratio_prefix}.display_name",
                max_length=_MAX_DISPLAY_NAME_LENGTH,
            )
            if ratio_key in ratio_keys:
                raise DomainValidationError(
                    f"Duplicate aspect ratio key under {resolution_key}: {ratio_key}"
                )
            ratio_keys.add(ratio_key)
            lookup[(resolution_key, ratio_key)] = _validate_size(
                ratio.get("size"), f"{ratio_prefix}.size"
            )

    default_pair = (default_resolution_key, default_aspect_ratio_key)
    if default_pair not in lookup:
        raise DomainValidationError(
            "The default image resolution and aspect ratio combination does not exist"
        )

    schema = _require_object(config_schema, "config_schema")
    properties = _require_object(schema.get("properties"), "config_schema.properties")
    size_definition = _require_object(
        properties.get("size"), "config_schema.properties.size"
    )
    size_enum = size_definition.get("enum")
    if not isinstance(size_enum, list) or not all(isinstance(value, str) for value in size_enum):
        raise DomainValidationError("config_schema.properties.size.enum must be a string array")
    if set(size_enum) != set(lookup.values()):
        raise DomainValidationError(
            "config_schema.properties.size.enum must match the image size matrix"
        )

    defaults = _require_object(default_parameters, "default_parameters")
    expected_default_size = lookup[default_pair]
    if defaults.get("size") != expected_default_size:
        raise DomainValidationError(
            "default_parameters.size must match the default image size matrix combination"
        )
    return lookup


def normalize_model_metadata(
    *,
    kind: str,
    config_schema: Any,
    default_parameters: Any,
    metadata: Any,
    matrix_explicitly_submitted: bool,
) -> dict:
    """Validate an image matrix or remove a legacy matrix after changing model kind."""

    normalized = deepcopy(_require_object(metadata, "metadata"))
    if kind != "image":
        if IMAGE_SIZE_MATRIX_KEY in normalized and matrix_explicitly_submitted:
            raise DomainValidationError(
                "metadata.image_size_matrix is only supported for image models"
            )
        normalized.pop(IMAGE_SIZE_MATRIX_KEY, None)
        return normalized

    if IMAGE_SIZE_MATRIX_KEY in normalized:
        validate_image_size_matrix(
            normalized[IMAGE_SIZE_MATRIX_KEY], config_schema, default_parameters
        )
    return normalized


def image_size_lookup(provider: dict) -> dict[tuple[str, str], str] | None:
    metadata = provider.get("metadata") or {}
    if not isinstance(metadata, dict) or IMAGE_SIZE_MATRIX_KEY not in metadata:
        return None
    return validate_image_size_matrix(
        metadata[IMAGE_SIZE_MATRIX_KEY],
        provider.get("config_schema") or {},
        provider.get("default_parameters") or {},
    )


def resolve_image_size_parameters(provider: dict, supplied: dict) -> dict:
    """Resolve UI semantic keys to the provider's concrete size parameter."""

    if not isinstance(supplied, dict):
        raise DomainValidationError("Generation parameters must be an object")
    normalized = dict(supplied)
    has_resolution = RESOLUTION_KEY_PARAMETER in normalized
    has_aspect_ratio = ASPECT_RATIO_KEY_PARAMETER in normalized

    if provider.get("kind") != "image":
        if has_resolution or has_aspect_ratio:
            raise DomainValidationError(
                "resolution_key and aspect_ratio_key are only supported for image models"
            )
        return normalized

    lookup = image_size_lookup(provider)
    if lookup is None:
        if has_resolution or has_aspect_ratio:
            raise DomainValidationError(
                "The selected image model does not support semantic resolution parameters"
            )
        # Without a matrix, legacy names may be native provider parameters and must
        # remain available to the model schema validator.
        return normalized

    has_legacy_resolution = LEGACY_RESOLUTION_PARAMETER in normalized
    has_legacy_aspect_ratio = LEGACY_ASPECT_RATIO_PARAMETER in normalized
    if has_resolution or has_aspect_ratio or has_legacy_resolution or has_legacy_aspect_ratio:
        resolution_values = [
            (parameter, normalized.pop(parameter))
            for parameter in (RESOLUTION_KEY_PARAMETER, LEGACY_RESOLUTION_PARAMETER)
            if parameter in normalized
        ]
        aspect_ratio_values = [
            (parameter, normalized.pop(parameter))
            for parameter in (ASPECT_RATIO_KEY_PARAMETER, LEGACY_ASPECT_RATIO_PARAMETER)
            if parameter in normalized
        ]

        normalized_resolutions = [
            (_normalized_key(value, parameter), parameter)
            for parameter, value in resolution_values
        ]
        if len({value for value, _ in normalized_resolutions}) > 1:
            raise DomainValidationError(
                "resolution conflicts with resolution_key"
            )
        normalized_aspect_ratios = [
            (_normalized_ratio_key(value, parameter), parameter)
            for parameter, value in aspect_ratio_values
        ]
        if len({value for value, _ in normalized_aspect_ratios}) > 1:
            raise DomainValidationError(
                "aspect_ratio conflicts with aspect_ratio_key"
            )

        matrix = provider["metadata"][IMAGE_SIZE_MATRIX_KEY]
        resolution_key = (
            normalized_resolutions[0][0]
            if normalized_resolutions
            else _normalized_key(
                matrix["default_resolution_key"],
                f"metadata.{IMAGE_SIZE_MATRIX_KEY}.default_resolution_key",
            )
        )
        aspect_ratio_key = (
            normalized_aspect_ratios[0][0]
            if normalized_aspect_ratios
            else _normalized_ratio_key(
                matrix["default_aspect_ratio_key"],
                f"metadata.{IMAGE_SIZE_MATRIX_KEY}.default_aspect_ratio_key",
            )
        )
        size = lookup.get((resolution_key, aspect_ratio_key))
        if size is None:
            raise DomainValidationError(
                "The selected image resolution and aspect ratio combination is unsupported"
            )
        supplied_size = normalized.get("size")
        if supplied_size is not None and supplied_size != size:
            raise DomainValidationError(
                "The supplied size does not match resolution_key and aspect_ratio_key"
            )
        normalized["size"] = size
    return normalized


def ensure_matrix_size_is_supported(provider: dict, parameters: dict) -> None:
    """Keep legacy size-only requests valid only when the configured matrix supports them."""

    lookup = image_size_lookup(provider)
    if lookup is None:
        return
    size = parameters.get("size")
    if size not in set(lookup.values()):
        raise DomainValidationError("Unsupported value for model parameter: size")
