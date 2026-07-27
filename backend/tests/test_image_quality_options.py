import pytest

from app.exceptions import DomainValidationError
from app.services.generation_provider import normalize_generation_parameters
from app.services.image_quality_options import (
    IMAGE_QUALITY_OPTIONS_KEY,
    normalize_image_quality_metadata,
    validate_image_quality_options,
)


def quality_options(*, provider_parameter: str = "quality") -> dict:
    return {
        "schema_version": 1,
        "provider_parameter": provider_parameter,
        "default_key": "medium",
        "options": [
            {"key": "low", "display_name": "低质量", "provider_value": "low"},
            {"key": "medium", "display_name": "标准质量", "provider_value": "medium"},
            {"key": "high", "display_name": "高质量", "provider_value": "high"},
        ],
    }


def provider() -> dict:
    return {
        "kind": "image",
        "config_schema": {
            "type": "object",
            "properties": {
                "quality": {"type": "string", "enum": ["low", "medium", "high"]}
            },
            "additionalProperties": False,
        },
        "default_parameters": {"quality": "medium"},
        "metadata": {IMAGE_QUALITY_OPTIONS_KEY: quality_options()},
    }


def test_quality_selection_maps_user_key_to_provider_parameter():
    assert normalize_generation_parameters(provider(), {"quality_key": "high"}) == {
        "quality": "high"
    }


def test_quality_selection_allows_matching_native_value_but_rejects_conflict():
    assert normalize_generation_parameters(
        provider(), {"quality_key": "low", "quality": "low"}
    ) == {"quality": "low"}
    with pytest.raises(DomainValidationError, match="conflicts with quality"):
        normalize_generation_parameters(
            provider(), {"quality_key": "low", "quality": "high"}
        )


def test_quality_configuration_supports_custom_provider_parameter():
    configured = provider()
    configured["config_schema"]["properties"] = {
        "image_quality": {"type": "string", "enum": ["low", "medium", "high"]}
    }
    configured["default_parameters"] = {"image_quality": "medium"}
    configured["metadata"] = {
        IMAGE_QUALITY_OPTIONS_KEY: quality_options(provider_parameter="image_quality")
    }
    assert normalize_generation_parameters(configured, {"quality_key": "high"}) == {
        "image_quality": "high"
    }


def test_legacy_parameter_name_is_read_and_rewritten_to_provider_parameter():
    legacy_quality = quality_options()
    legacy_quality["parameter_name"] = legacy_quality.pop("provider_parameter")
    legacy = provider()
    legacy["metadata"] = {IMAGE_QUALITY_OPTIONS_KEY: legacy_quality}

    assert normalize_generation_parameters(legacy, {"quality_key": "high"}) == {
        "quality": "high"
    }
    normalized_metadata = normalize_image_quality_metadata(
        kind="image",
        config_schema=legacy["config_schema"],
        default_parameters=legacy["default_parameters"],
        metadata=legacy["metadata"],
        explicitly_submitted=False,
    )
    normalized_quality = normalized_metadata[IMAGE_QUALITY_OPTIONS_KEY]
    assert normalized_quality["provider_parameter"] == "quality"
    assert "parameter_name" not in normalized_quality


def test_conflicting_legacy_and_canonical_provider_parameters_are_rejected():
    configured = quality_options()
    configured["parameter_name"] = "image_quality"

    with pytest.raises(DomainValidationError, match="provider_parameter conflicts"):
        validate_image_quality_options(
            configured,
            provider()["config_schema"],
            provider()["default_parameters"],
        )


def test_quality_configuration_rejects_duplicate_keys_and_invalid_default():
    duplicate = quality_options()
    duplicate["options"].append(
        {"key": "LOW", "display_name": "重复", "provider_value": "low"}
    )
    with pytest.raises(DomainValidationError, match="duplicate key"):
        validate_image_quality_options(
            duplicate,
            provider()["config_schema"],
            provider()["default_parameters"],
        )

    invalid_default = quality_options()
    invalid_default["default_key"] = "ultra"
    with pytest.raises(DomainValidationError, match="must reference an option"):
        validate_image_quality_options(
            invalid_default,
            provider()["config_schema"],
            provider()["default_parameters"],
        )


def test_quality_configuration_must_match_schema_and_default_parameters():
    with pytest.raises(DomainValidationError, match="must define the quality parameter"):
        validate_image_quality_options(
            quality_options(),
            {"type": "object", "properties": {}},
            {"quality": "medium"},
        )
    with pytest.raises(DomainValidationError, match="must match the default"):
        validate_image_quality_options(
            quality_options(),
            provider()["config_schema"],
            {"quality": "high"},
        )


def test_non_image_explicit_quality_configuration_is_rejected():
    with pytest.raises(DomainValidationError, match="only supported for image"):
        normalize_image_quality_metadata(
            kind="video",
            config_schema={},
            default_parameters={},
            metadata={IMAGE_QUALITY_OPTIONS_KEY: quality_options()},
            explicitly_submitted=True,
        )


def test_legacy_image_model_keeps_native_quality_compatibility():
    legacy = provider()
    legacy["metadata"] = {}
    assert normalize_generation_parameters(legacy, {"quality": "high"}) == {
        "quality": "high"
    }
    with pytest.raises(DomainValidationError, match="does not support semantic quality"):
        normalize_generation_parameters(legacy, {"quality_key": "high"})
