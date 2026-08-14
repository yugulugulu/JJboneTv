"""Validation and normalization for administrator-configured video models."""

from copy import deepcopy
from typing import Any

from ..exceptions import DomainValidationError


VIDEO_OPTIONS_KEY = "video_options"
_MAX_OPTIONS = 32
_MAX_STRING_LENGTH = 80
_MAX_DURATION_SECONDS = 86_400
_MAX_PROMPT_LENGTH = 4_000
MAX_REFERENCE_IMAGE_BYTES = 20 * 1024 * 1024
MAX_REFERENCE_VIDEO_TOTAL_BYTES = 200 * 1024 * 1024
MAX_REFERENCE_AUDIO_TOTAL_BYTES = 50 * 1024 * 1024


def _object(value: Any, field: str) -> dict:
    if not isinstance(value, dict):
        raise DomainValidationError(f"{field} must be an object")
    return value


def _string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DomainValidationError(f"{field} must be a non-empty string")
    normalized = value.strip()
    if len(normalized) > _MAX_STRING_LENGTH:
        raise DomainValidationError(f"{field} cannot exceed {_MAX_STRING_LENGTH} characters")
    return normalized


def _string_options(value: Any, field: str) -> list[str]:
    if not isinstance(value, list) or not value:
        raise DomainValidationError(f"{field} must be a non-empty array")
    if len(value) > _MAX_OPTIONS:
        raise DomainValidationError(f"{field} cannot contain more than {_MAX_OPTIONS} items")
    result: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        normalized = _string(item, f"{field}[{index}]")
        key = normalized.casefold()
        if key in seen:
            raise DomainValidationError(f"{field} contains duplicate values")
        seen.add(key)
        result.append(normalized)
    return result


def _count_options(value: Any, field: str) -> list[str]:
    # Accept the legacy array form as well as the new single maximum-count
    # form.  Normalization keeps the persisted representation compatible with
    # existing models while allowing the admin UI to submit one value.
    if isinstance(value, (str, int)) and not isinstance(value, bool):
        value = [value]
    if not isinstance(value, list):
        raise DomainValidationError(f"{field} must be a non-negative integer")
    # An empty array explicitly means that the provider does not accept this
    # reference kind. This is distinct from a missing field, which is
    # normalized to the same empty set below for backwards-compatible
    # configuration.
    if not value:
        return []
    numbers: list[int] = []
    seen_numbers: set[int] = set()
    for index, raw_item in enumerate(value):
        item = _string(raw_item, f"{field}[{index}]")
        if not item.isdigit():
            raise DomainValidationError(f"{field}[{index}] must be a non-negative integer string")
        number = int(item)
        if number > 100:
            raise DomainValidationError(f"{field}[{index}] cannot exceed 100")
        if number in seen_numbers:
            raise DomainValidationError(f"{field} contains duplicate values")
        seen_numbers.add(number)
        numbers.append(number)
    return [str(number) for number in sorted(numbers)]


def validate_video_input_references(
    metadata: Any,
    input_references: list[dict],
) -> None:
    """Validate media predecessor counts and durations against video_options.

    Count fields are intentionally optional for backwards compatibility with
    video models created before these options existed.  The current admin
    contract stores one maximum count per kind; legacy arrays are still read
    by taking their largest configured value.  An explicit empty list means
    that the corresponding predecessor kind is unsupported.
    """

    image_references = [
        reference for reference in input_references if reference.get("kind") == "image"
    ]
    video_references = [
        reference for reference in input_references if reference.get("kind") == "video"
    ]
    audio_references = [
        reference for reference in input_references if reference.get("kind") == "audio"
    ]

    for reference in image_references:
        byte_size = _byte_size(reference, "image")
        if byte_size is not None and byte_size > MAX_REFERENCE_IMAGE_BYTES:
            raise DomainValidationError(
                "unsupported-input: Each image reference must not exceed 20 MB"
            )
    for references, limit, label in (
        (video_references, MAX_REFERENCE_VIDEO_TOTAL_BYTES, "video"),
        (audio_references, MAX_REFERENCE_AUDIO_TOTAL_BYTES, "audio"),
    ):
        total_bytes = sum(
            byte_size
            for reference in references
            if (byte_size := _byte_size(reference, label)) is not None
        )
        if total_bytes > limit:
            limit_mb = limit // (1024 * 1024)
            raise DomainValidationError(
                f"unsupported-input: Total {label} reference size must not exceed {limit_mb} MB"
            )

    if not isinstance(metadata, dict):
        return
    raw_options = metadata.get(VIDEO_OPTIONS_KEY)
    if raw_options is None:
        return
    options = _object(raw_options, f"metadata.{VIDEO_OPTIONS_KEY}")

    kind_fields = {
        "image": "referenceImages",
        "video": "referenceVideos",
        "audio": "referenceAudios",
    }
    for kind, field_name in kind_fields.items():
        if field_name not in options:
            continue
        allowed_counts = _count_options(
            options[field_name], f"metadata.{VIDEO_OPTIONS_KEY}.{field_name}"
        )
        count = sum(reference.get("kind") == kind for reference in input_references)
        if count == 0 and not allowed_counts:
            # An explicit empty set disables the kind; a request with no
            # predecessors of that kind remains valid.
            continue
        if not allowed_counts and count > 0:
            raise DomainValidationError(
                f"unsupported-input: metadata.{VIDEO_OPTIONS_KEY}.{field_name} "
                f"does not allow {kind} reference(s)"
            )
        if count > int(allowed_counts[-1]):
            raise DomainValidationError(
                f"unsupported-input: metadata.{VIDEO_OPTIONS_KEY}.{field_name} "
                f"does not allow more than {allowed_counts[-1]} {kind} reference(s)"
            )

    duration_fields = {
        "video": "referenceVideoMaxDurationSeconds",
        "audio": "referenceAudioMaxDurationSeconds",
    }
    for kind, field_name in duration_fields.items():
        if field_name not in options or options[field_name] is None:
            continue
        limit_seconds = _duration(
            options[field_name], f"metadata.{VIDEO_OPTIONS_KEY}.{field_name}", allow_null=True
        )
        if limit_seconds is None:
            continue
        references = [
            reference for reference in input_references if reference.get("kind") == kind
        ]
        if not references:
            continue
        total_duration_ms = 0
        for reference in references:
            duration_ms = reference.get("duration_ms")
            if duration_ms is None:
                raise DomainValidationError(
                    f"unsupported-input: {kind} reference duration is unavailable"
                )
            if isinstance(duration_ms, bool) or not isinstance(duration_ms, int) or duration_ms < 0:
                raise DomainValidationError(
                    f"unsupported-input: {kind} reference duration is invalid"
                )
            total_duration_ms += duration_ms
        if total_duration_ms > limit_seconds * 1000:
            raise DomainValidationError(
                f"unsupported-input: Total {kind} reference duration exceeds "
                f"metadata.{VIDEO_OPTIONS_KEY}.{field_name}"
            )


def _byte_size(reference: dict, kind: str) -> int | None:
    value = reference.get("byte_size")
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise DomainValidationError(
            f"unsupported-input: {kind} reference size is unavailable or invalid"
        )
    return value


def _duration(value: Any, field: str, *, allow_null: bool = False) -> int | None:
    if value is None and allow_null:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise DomainValidationError(f"{field} must be a non-negative integer number of seconds")
    if value < 0 or value > _MAX_DURATION_SECONDS:
        raise DomainValidationError(f"{field} must be between 0 and {_MAX_DURATION_SECONDS} seconds")
    return value


def _prompt_max_length(value: Any, field: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise DomainValidationError(
            f"{field} must be an integer between 1 and {_MAX_PROMPT_LENGTH}"
        )
    if value < 1 or value > _MAX_PROMPT_LENGTH:
        raise DomainValidationError(
            f"{field} must be between 1 and {_MAX_PROMPT_LENGTH}"
        )
    return value


def validate_video_prompt(metadata: Any, final_prompt: str) -> None:
    """Validate the compiled provider prompt against the active model limit."""

    if not isinstance(metadata, dict):
        return
    raw_options = metadata.get(VIDEO_OPTIONS_KEY)
    if raw_options is None:
        return
    options = _object(raw_options, f"metadata.{VIDEO_OPTIONS_KEY}")
    if "promptMaxLength" not in options:
        return
    limit = _prompt_max_length(
        options["promptMaxLength"],
        f"metadata.{VIDEO_OPTIONS_KEY}.promptMaxLength",
    )
    if limit is not None and len(final_prompt) > limit:
        raise DomainValidationError(
            f"invalid-input: Video prompt cannot exceed {limit} characters"
        )


def normalize_video_options(
    *,
    kind: str,
    config_schema: Any,
    default_parameters: Any,
    metadata: Any,
    explicitly_submitted: bool,
) -> dict:
    """Validate metadata.video_options and synchronize provider schema/defaults."""

    normalized_metadata = deepcopy(_object(metadata, "metadata"))
    if kind != "video":
        if VIDEO_OPTIONS_KEY in normalized_metadata and explicitly_submitted:
            raise DomainValidationError("metadata.video_options is only supported for video models")
        normalized_metadata.pop(VIDEO_OPTIONS_KEY, None)
        return normalized_metadata

    if VIDEO_OPTIONS_KEY not in normalized_metadata:
        return normalized_metadata

    raw = _object(normalized_metadata[VIDEO_OPTIONS_KEY], f"metadata.{VIDEO_OPTIONS_KEY}")
    if raw.get("schema_version", 1) != 1:
        raise DomainValidationError("metadata.video_options.schema_version must be 1")

    resolution = _object(raw.get("resolution"), "metadata.video_options.resolution")
    ratio = _object(raw.get("ratio"), "metadata.video_options.ratio")
    resolution_options = _string_options(resolution.get("options"), "metadata.video_options.resolution.options")
    ratio_options = _string_options(ratio.get("options"), "metadata.video_options.ratio.options")
    resolution_default = _string(resolution.get("default"), "metadata.video_options.resolution.default")
    ratio_default = _string(ratio.get("default"), "metadata.video_options.ratio.default")
    if resolution_default.casefold() not in {item.casefold() for item in resolution_options}:
        raise DomainValidationError("metadata.video_options.resolution.default must be one of options")
    if ratio_default.casefold() not in {item.casefold() for item in ratio_options}:
        raise DomainValidationError("metadata.video_options.ratio.default must be one of options")

    duration = _object(raw.get("duration"), "metadata.video_options.duration")
    min_seconds = _duration(duration.get("min"), "metadata.video_options.duration.min")
    max_seconds = _duration(duration.get("max"), "metadata.video_options.duration.max")
    default_seconds = _duration(duration.get("default"), "metadata.video_options.duration.default")
    if min_seconds is None or max_seconds is None or default_seconds is None:
        raise DomainValidationError("metadata.video_options.duration values are required")
    if min_seconds > max_seconds or not min_seconds <= default_seconds <= max_seconds:
        raise DomainValidationError("metadata.video_options.duration must satisfy min <= default <= max")

    normalized = {
        "schema_version": 1,
        "resolution": {"options": resolution_options, "default": resolution_default},
        "ratio": {"options": ratio_options, "default": ratio_default},
        "referenceVideoMaxDurationSeconds": _duration(
            raw.get("referenceVideoMaxDurationSeconds"), "metadata.video_options.referenceVideoMaxDurationSeconds", allow_null=True
        ),
        "referenceAudioMaxDurationSeconds": _duration(
            raw.get("referenceAudioMaxDurationSeconds"), "metadata.video_options.referenceAudioMaxDurationSeconds", allow_null=True
        ),
        "duration": {"min": min_seconds, "max": max_seconds, "default": default_seconds},
    }
    # Missing count fields retain legacy/unrestricted semantics.  An
    # explicitly supplied empty list is preserved as [] and means unsupported.
    for field_name in ("referenceImages", "referenceVideos", "referenceAudios"):
        if field_name in raw:
            normalized[field_name] = _count_options(
                raw[field_name], f"metadata.video_options.{field_name}"
            )
    if "promptMaxLength" in raw:
        normalized["promptMaxLength"] = _prompt_max_length(
            raw["promptMaxLength"],
            "metadata.video_options.promptMaxLength",
        )

    # Reference counts are the source of truth for media input support.
    # When a count is explicitly configured, synchronize the corresponding
    # media kind so administrators do not need to maintain the same capability
    # in both video_options and supported_input_kinds.
    supported_input_kinds = normalized_metadata.get("supported_input_kinds")
    if isinstance(supported_input_kinds, list):
        synchronized_kinds = [
            item for item in supported_input_kinds if isinstance(item, str) and item.strip()
        ]
    else:
        synchronized_kinds = ["text"]
    for kind, field_name in (
        ("image", "referenceImages"),
        ("video", "referenceVideos"),
        ("audio", "referenceAudios"),
    ):
        if field_name not in normalized:
            continue
        maximum = max((int(item) for item in normalized[field_name]), default=0)
        synchronized_kinds = [item for item in synchronized_kinds if item != kind]
        if maximum > 0:
            synchronized_kinds.append(kind)
    if any(
        field_name in normalized
        for field_name in ("referenceImages", "referenceVideos", "referenceAudios")
    ):
        normalized_metadata["supported_input_kinds"] = list(dict.fromkeys(synchronized_kinds))

    schema = _object(config_schema, "config_schema")
    properties = schema.get("properties")
    properties = dict(properties) if isinstance(properties, dict) else {}
    properties["resolution"] = {**(properties.get("resolution") or {}), "type": "string", "enum": resolution_options}
    properties["ratio"] = {**(properties.get("ratio") or {}), "type": "string", "enum": ratio_options}
    properties["duration"] = {
        **(properties.get("duration") or {}),
        "type": "integer",
        "minimum": min_seconds,
        "maximum": max_seconds,
    }
    schema = {**schema, "type": schema.get("type") or "object", "properties": properties}
    defaults = _object(default_parameters, "default_parameters")
    defaults = {
        **defaults,
        "resolution": resolution_default,
        "ratio": ratio_default,
        "duration": default_seconds,
    }

    normalized_metadata[VIDEO_OPTIONS_KEY] = normalized
    normalized_metadata["_normalized_config_schema"] = schema
    normalized_metadata["_normalized_default_parameters"] = defaults
    return normalized_metadata


def extract_normalized_provider_config(metadata: dict, config_schema: dict, default_parameters: dict) -> tuple[dict, dict, dict]:
    """Remove internal normalization markers added by normalize_video_options."""

    normalized_metadata = dict(metadata)
    schema = normalized_metadata.pop("_normalized_config_schema", config_schema)
    defaults = normalized_metadata.pop("_normalized_default_parameters", default_parameters)
    return normalized_metadata, schema, defaults
