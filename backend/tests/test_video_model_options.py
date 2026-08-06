import pytest

from app.exceptions import DomainValidationError
from app.services.video_model_options import (
    VIDEO_OPTIONS_KEY,
    extract_normalized_provider_config,
    normalize_video_options,
    validate_video_input_references,
    validate_video_prompt,
)


def video_options() -> dict:
    return {
        "schema_version": 1,
        "resolution": {
            "options": ["480p", "720p"],
            "default": "720p",
        },
        "ratio": {
            "options": ["16:9", "9:16", "1:1"],
            "default": "16:9",
        },
        "referenceImages": ["0", "1", "2", "4"],
        "referenceVideos": ["0", "1", "3"],
        "referenceAudios": ["0", "1"],
        "referenceVideoMaxDurationSeconds": 30,
        "referenceAudioMaxDurationSeconds": 60,
        "duration": {"min": 5, "max": 15, "default": 10},
    }


def normalize(config=None, defaults=None, metadata=None, *, kind="video", explicit=True):
    return normalize_video_options(
        kind=kind,
        config_schema=config if config is not None else {"type": "object", "properties": {}},
        default_parameters=defaults if defaults is not None else {},
        metadata=metadata if metadata is not None else {VIDEO_OPTIONS_KEY: video_options()},
        explicitly_submitted=explicit,
    )


def test_video_options_normalize_and_syncs_provider_schema_and_defaults():
    configured = video_options()
    configured["promptMaxLength"] = 4000
    normalized = normalize(metadata={VIDEO_OPTIONS_KEY: configured})

    assert normalized[VIDEO_OPTIONS_KEY]["resolution"]["default"] == "720p"
    assert normalized[VIDEO_OPTIONS_KEY]["referenceImages"] == ["0", "1", "2", "4"]
    assert normalized[VIDEO_OPTIONS_KEY]["promptMaxLength"] == 4000
    assert normalized["_normalized_config_schema"]["properties"]["resolution"] == {
        "type": "string",
        "enum": ["480p", "720p"],
    }
    assert normalized["_normalized_config_schema"]["properties"]["duration"] == {
        "type": "integer",
        "minimum": 5,
        "maximum": 15,
    }
    assert normalized["_normalized_default_parameters"] == {
        "resolution": "720p",
        "ratio": "16:9",
        "duration": 10,
    }

    metadata, schema, defaults = extract_normalized_provider_config(
        normalized, {"type": "object", "properties": {}}, {}
    )
    assert VIDEO_OPTIONS_KEY in metadata
    assert "_normalized_config_schema" not in metadata
    assert schema["properties"]["ratio"]["enum"] == ["16:9", "9:16", "1:1"]
    assert defaults["duration"] == 10


def test_empty_reference_lists_are_valid_and_mean_not_supported():
    configured = video_options()
    configured["referenceImages"] = []
    configured["referenceVideos"] = []
    configured["referenceAudios"] = []

    normalized = normalize(metadata={VIDEO_OPTIONS_KEY: configured})

    assert normalized[VIDEO_OPTIONS_KEY]["referenceImages"] == []
    assert normalized[VIDEO_OPTIONS_KEY]["referenceVideos"] == []
    assert normalized[VIDEO_OPTIONS_KEY]["referenceAudios"] == []


@pytest.mark.parametrize(
    ("mutator", "message"),
    [
        (lambda value: value["resolution"].update({"default": "1080p"}), "resolution.default"),
        (lambda value: value["ratio"].update({"default": "4:3"}), "ratio.default"),
        (lambda value: value["duration"].update({"default": 20}), "duration"),
        (lambda value: value["duration"].update({"min": 20, "max": 10}), "duration"),
        (lambda value: value.update({"referenceImages": ["one"]}), "referenceImages"),
        (lambda value: value.update({"referenceVideos": ["1", "01"]}), "referenceVideos"),
    ],
)
def test_video_options_reject_invalid_configuration(mutator, message):
    configured = video_options()
    mutator(configured)

    with pytest.raises(DomainValidationError, match=message):
        normalize(metadata={VIDEO_OPTIONS_KEY: configured})


@pytest.mark.parametrize("value", [0, -1, True, 1.5, "100", 4001])
def test_video_options_reject_invalid_prompt_max_length(value):
    configured = video_options()
    configured["promptMaxLength"] = value

    with pytest.raises(DomainValidationError, match="promptMaxLength"):
        normalize(metadata={VIDEO_OPTIONS_KEY: configured})


@pytest.mark.parametrize("value", [1, 4000])
def test_video_options_accept_prompt_max_length_boundaries(value):
    configured = video_options()
    configured["promptMaxLength"] = value

    normalized = normalize(metadata={VIDEO_OPTIONS_KEY: configured})

    assert normalized[VIDEO_OPTIONS_KEY]["promptMaxLength"] == value


def test_null_or_missing_prompt_max_length_keeps_prompt_unlimited():
    configured = video_options()
    configured["promptMaxLength"] = None
    normalized = normalize(metadata={VIDEO_OPTIONS_KEY: configured})

    assert normalized[VIDEO_OPTIONS_KEY]["promptMaxLength"] is None
    validate_video_prompt(normalized, "任意长度😀" * 2_000)

    configured.pop("promptMaxLength")
    normalized = normalize(metadata={VIDEO_OPTIONS_KEY: configured})
    validate_video_prompt(normalized, "任意长度😀" * 2_000)


def test_video_prompt_length_uses_unicode_code_points_and_exact_boundary():
    metadata = {VIDEO_OPTIONS_KEY: {"promptMaxLength": 3}}

    validate_video_prompt(metadata, "中😀文")
    with pytest.raises(DomainValidationError, match="cannot exceed 3"):
        validate_video_prompt(metadata, "中😀文字")


def test_video_options_are_rejected_for_non_video_models_when_explicit():
    with pytest.raises(DomainValidationError, match="only supported for video"):
        normalize(kind="image", metadata={VIDEO_OPTIONS_KEY: video_options()})


def test_omitting_video_options_keeps_legacy_model_untouched():
    config = {"type": "object", "properties": {"prompt": {"type": "string"}}}
    defaults = {"prompt": ""}
    metadata = {"protocol": "custom"}

    normalized = normalize(config=config, defaults=defaults, metadata=metadata)

    assert normalized == metadata


def test_missing_reference_count_fields_keep_legacy_unrestricted_behavior():
    configured = video_options()
    configured.pop("referenceImages")
    configured.pop("referenceVideos")
    configured.pop("referenceAudios")

    normalized = normalize(metadata={VIDEO_OPTIONS_KEY: configured})

    assert "referenceImages" not in normalized[VIDEO_OPTIONS_KEY]
    validate_video_input_references(
        normalized,
        [
            {"kind": "image"},
            {"kind": "image"},
            {"kind": "video", "duration_ms": 1},
        ],
    )


def test_video_reference_count_uses_maximum_and_empty_array_disables_kind():
    configured = video_options()
    normalized = normalize(metadata={VIDEO_OPTIONS_KEY: configured})

    validate_video_input_references(
        normalized,
        [{"kind": "image"}, {"kind": "image"}, {"kind": "image"}],
    )
    with pytest.raises(DomainValidationError, match="more than 4 image"):
        validate_video_input_references(
            normalized,
            [
                {"kind": "image"},
                {"kind": "image"},
                {"kind": "image"},
                {"kind": "image"},
                {"kind": "image"},
            ],
        )

    configured["referenceImages"] = []
    normalized = normalize(metadata={VIDEO_OPTIONS_KEY: configured})
    with pytest.raises(DomainValidationError, match="referenceImages"):
        validate_video_input_references(normalized, [{"kind": "image"}])
    validate_video_input_references(normalized, [])


def test_video_and_audio_reference_duration_limits_use_asset_duration_ms():
    normalized = normalize()

    validate_video_input_references(
        normalized,
        [
            {"kind": "video", "duration_ms": 10_000},
            {"kind": "audio", "duration_ms": 60_000},
        ],
    )

    with pytest.raises(DomainValidationError, match="Total video reference duration"):
        validate_video_input_references(
            normalized,
            [
                {"kind": "video", "duration_ms": 30_001},
            ],
        )

    with pytest.raises(DomainValidationError, match="duration is unavailable"):
        validate_video_input_references(normalized, [{"kind": "video"}])

    with pytest.raises(DomainValidationError, match="Total audio reference duration"):
        validate_video_input_references(
            normalized,
            [{"kind": "audio", "duration_ms": 60_001}],
        )


def test_video_reference_file_size_limits():
    normalized = normalize()

    with pytest.raises(DomainValidationError, match="Each image reference"):
        validate_video_input_references(
            normalized,
            [{"kind": "image", "byte_size": 20 * 1024 * 1024 + 1}],
        )

    with pytest.raises(DomainValidationError, match="Total video reference size"):
        validate_video_input_references(
            normalized,
            [
                {"kind": "video", "byte_size": 150 * 1024 * 1024},
                {"kind": "video", "byte_size": 50 * 1024 * 1024 + 1},
            ],
        )

    with pytest.raises(DomainValidationError, match="Total audio reference size"):
        validate_video_input_references(
            normalized,
            [
                {"kind": "audio", "byte_size": 25 * 1024 * 1024},
                {"kind": "audio", "byte_size": 25 * 1024 * 1024 + 1},
            ],
        )
