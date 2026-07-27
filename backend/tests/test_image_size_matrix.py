from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.admin_schemas import AdminModelUpdate
from app.exceptions import DomainValidationError
from app.services.admin import AdminService
from app.services.generation_provider import (
    ProviderGenerationError,
    frozen_generation_parameters,
    normalize_generation_parameters,
)
from app.services.image_size_matrix import (
    IMAGE_SIZE_MATRIX_KEY,
    normalize_model_metadata,
    validate_image_size_matrix,
)


def image_size_matrix() -> dict:
    return {
        "schema_version": 1,
        "default_resolution_key": "1k",
        "default_aspect_ratio_key": "1:1",
        "resolutions": [
            {
                "key": "1k",
                "display_name": "1K",
                "ratios": [
                    {"key": "1:1", "display_name": "1:1", "size": "1024x1024"},
                    {"key": "16:9", "display_name": "16:9", "size": "1024x576"},
                ],
            },
            {
                "key": "2k",
                "display_name": "2K",
                "ratios": [
                    {"key": "1:1", "display_name": "1:1", "size": "2048x2048"},
                    {"key": "16:9", "display_name": "16:9", "size": "2048x1152"},
                ],
            },
        ],
    }


def image_schema(*sizes: str) -> dict:
    return {
        "type": "object",
        "properties": {
            "size": {"type": "string", "enum": list(sizes)},
            "quality": {"type": "string", "enum": ["low", "medium", "high"]},
        },
        "additionalProperties": False,
    }


def image_provider() -> dict:
    return {
        "kind": "image",
        "config_schema": image_schema(
            "1024x1024", "1024x576", "2048x2048", "2048x1152"
        ),
        "default_parameters": {"size": "1024x1024", "quality": "medium"},
        "metadata": {IMAGE_SIZE_MATRIX_KEY: image_size_matrix()},
    }


def test_image_size_matrix_validates_lookup_schema_and_default():
    provider = image_provider()
    assert validate_image_size_matrix(
        provider["metadata"][IMAGE_SIZE_MATRIX_KEY],
        provider["config_schema"],
        provider["default_parameters"],
    ) == {
        ("1k", "1:1"): "1024x1024",
        ("1k", "16:9"): "1024x576",
        ("2k", "1:1"): "2048x2048",
        ("2k", "16:9"): "2048x1152",
    }


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda matrix: matrix["resolutions"].append(matrix["resolutions"][0].copy()),
            "Duplicate image resolution key",
        ),
        (
            lambda matrix: matrix["resolutions"][0]["ratios"].append(
                matrix["resolutions"][0]["ratios"][0].copy()
            ),
            "Duplicate aspect ratio key",
        ),
        (
            lambda matrix: matrix["resolutions"][0]["ratios"][0].update(
                size="0x1024"
            ),
            "positive WIDTHxHEIGHT",
        ),
        (
            lambda matrix: matrix["resolutions"][0]["ratios"][0].update(
                size="32769x1024"
            ),
            "cannot exceed 32768",
        ),
        (
            lambda matrix: matrix.update(default_aspect_ratio_key="9:16"),
            "default image resolution",
        ),
    ],
)
def test_image_size_matrix_rejects_invalid_structure(mutate, message):
    matrix = image_size_matrix()
    mutate(matrix)
    with pytest.raises(DomainValidationError, match=message):
        validate_image_size_matrix(
            matrix,
            image_provider()["config_schema"],
            image_provider()["default_parameters"],
        )


def test_image_size_matrix_requires_schema_enum_and_default_size_to_match():
    with pytest.raises(DomainValidationError, match="enum must match"):
        validate_image_size_matrix(
            image_size_matrix(),
            image_schema("1024x1024"),
            {"size": "1024x1024"},
        )
    with pytest.raises(DomainValidationError, match="default_parameters.size"):
        validate_image_size_matrix(
            image_size_matrix(),
            image_provider()["config_schema"],
            {"size": "2048x2048"},
        )


def test_non_image_rejects_explicit_matrix_but_clears_legacy_matrix():
    metadata = {"protocol": "custom", IMAGE_SIZE_MATRIX_KEY: image_size_matrix()}
    with pytest.raises(DomainValidationError, match="only supported for image"):
        normalize_model_metadata(
            kind="video",
            config_schema={},
            default_parameters={},
            metadata=metadata,
            matrix_explicitly_submitted=True,
        )
    assert normalize_model_metadata(
        kind="video",
        config_schema={},
        default_parameters={},
        metadata=metadata,
        matrix_explicitly_submitted=False,
    ) == {"protocol": "custom"}


def test_image_model_rejects_a_null_matrix_when_the_field_is_present():
    with pytest.raises(DomainValidationError, match="must be an object"):
        normalize_model_metadata(
            kind="image",
            config_schema={},
            default_parameters={},
            metadata={IMAGE_SIZE_MATRIX_KEY: None},
            matrix_explicitly_submitted=True,
        )


def test_generation_semantic_keys_resolve_to_provider_size_and_are_not_persisted():
    assert normalize_generation_parameters(
        image_provider(),
        {
            "resolution_key": "2k",
            "aspect_ratio_key": "16:9",
            "quality": "high",
        },
    ) == {"size": "2048x1152", "quality": "high"}

    with pytest.raises(DomainValidationError, match="supplied size does not match"):
        normalize_generation_parameters(
            image_provider(),
            {
                "resolution_key": "2k",
                "aspect_ratio_key": "16:9",
                "size": "1024x1024",
            },
        )


def test_generation_legacy_aliases_resolve_and_are_not_persisted():
    assert normalize_generation_parameters(
        image_provider(),
        {
            "resolution": "2K",
            "aspect_ratio": "32:18",
            "quality": "high",
        },
    ) == {"size": "2048x1152", "quality": "high"}


def test_generation_accepts_matching_aliases_and_rejects_conflicts():
    assert normalize_generation_parameters(
        image_provider(),
        {
            "resolution": "2k",
            "resolution_key": " 2K ",
            "aspect_ratio": "32:18",
            "aspect_ratio_key": "16:9",
        },
    )["size"] == "2048x1152"

    with pytest.raises(DomainValidationError, match="resolution conflicts"):
        normalize_generation_parameters(
            image_provider(),
            {"resolution": "1k", "resolution_key": "2k"},
        )
    with pytest.raises(DomainValidationError, match="aspect_ratio conflicts"):
        normalize_generation_parameters(
            image_provider(),
            {"aspect_ratio": "1:1", "aspect_ratio_key": "16:9"},
        )


def test_generation_normalizes_defaults_before_request_parameters_override_them():
    provider = image_provider()
    provider["default_parameters"] = {
        "size": "1024x1024",
        "resolution": "1k",
        "aspect_ratio": "1:1",
        "quality": "medium",
    }
    assert normalize_generation_parameters(provider, {"quality": "high"}) == {
        "size": "1024x1024",
        "quality": "high",
    }
    assert normalize_generation_parameters(
        provider,
        {"resolution_key": "2k", "aspect_ratio_key": "16:9"},
    ) == {"size": "2048x1152", "quality": "medium"}


def test_generation_matrix_accepts_supported_legacy_size_and_rejects_other_values():
    assert normalize_generation_parameters(
        image_provider(), {"size": "1024x576"}
    )["size"] == "1024x576"
    with pytest.raises(DomainValidationError, match="Unsupported value"):
        normalize_generation_parameters(
            image_provider(), {"size": "1536x1024"}
        )


def test_generation_fills_a_missing_semantic_key_from_matrix_defaults():
    assert normalize_generation_parameters(
        image_provider(), {"resolution_key": "2k"}
    )["size"] == "2048x2048"
    assert normalize_generation_parameters(
        image_provider(), {"aspect_ratio_key": "16:9"}
    )["size"] == "1024x576"
    with pytest.raises(DomainValidationError, match="must be a non-empty string"):
        normalize_generation_parameters(
            image_provider(),
            {"resolution_key": None, "aspect_ratio_key": "16:9"},
        )


def test_generation_rejects_unknown_and_non_image_semantic_keys():
    with pytest.raises(DomainValidationError, match="combination is unsupported"):
        normalize_generation_parameters(
            image_provider(),
            {"resolution_key": "2k", "aspect_ratio_key": "9:16"},
        )
    with pytest.raises(DomainValidationError, match="only supported for image"):
        normalize_generation_parameters(
            {"kind": "video", "config_schema": {}, "default_parameters": {}},
            {"resolution_key": "1k", "aspect_ratio_key": "1:1"},
        )


def test_generation_uses_default_size_when_no_semantic_or_size_is_supplied():
    assert normalize_generation_parameters(image_provider(), {"quality": "high"}) == {
        "size": "1024x1024",
        "quality": "high",
    }


def test_matrix_keys_are_normalized_for_duplicates_and_resolution():
    matrix = image_size_matrix()
    matrix["resolutions"][1]["key"] = " 1K "
    with pytest.raises(DomainValidationError, match="Duplicate image resolution key"):
        validate_image_size_matrix(
            matrix,
            image_provider()["config_schema"],
            image_provider()["default_parameters"],
        )

    matrix = image_size_matrix()
    matrix["resolutions"][0]["ratios"][0]["key"] = "32:18"
    with pytest.raises(DomainValidationError, match="Duplicate aspect ratio key"):
        validate_image_size_matrix(
            matrix,
            image_provider()["config_schema"],
            image_provider()["default_parameters"],
        )

    assert normalize_generation_parameters(
        image_provider(),
        {"resolution_key": " 2K ", "aspect_ratio_key": "32:18"},
    )["size"] == "2048x1152"


def test_matrix_limits_item_counts_and_key_display_lengths():
    matrix = image_size_matrix()
    matrix["resolutions"] = [
        {
            "key": f"r{index}",
            "display_name": f"R{index}",
            "ratios": [{"key": "1:1", "display_name": "1:1", "size": "1024x1024"}],
        }
        for index in range(33)
    ]
    with pytest.raises(DomainValidationError, match="more than 32"):
        validate_image_size_matrix(matrix, image_schema("1024x1024"), {"size": "1024x1024"})

    matrix = image_size_matrix()
    matrix["resolutions"][0]["ratios"] = [
        {
            "key": f"{index + 1}:37",
            "display_name": f"{index + 1}:37",
            "size": "1024x1024",
        }
        for index in range(33)
    ]
    with pytest.raises(DomainValidationError, match="more than 32"):
        validate_image_size_matrix(matrix, image_schema("1024x1024"), {"size": "1024x1024"})

    matrix = image_size_matrix()
    matrix["resolutions"][0]["key"] = "x" * 65
    with pytest.raises(DomainValidationError, match="64 characters"):
        validate_image_size_matrix(
            matrix,
            image_provider()["config_schema"],
            image_provider()["default_parameters"],
        )

    matrix = image_size_matrix()
    matrix["resolutions"][0]["display_name"] = "x" * 81
    with pytest.raises(DomainValidationError, match="80 characters"):
        validate_image_size_matrix(
            matrix,
            image_provider()["config_schema"],
            image_provider()["default_parameters"],
        )
def test_legacy_image_model_without_matrix_still_accepts_size_only():
    provider = image_provider()
    provider["metadata"] = {}
    assert normalize_generation_parameters(provider, {"size": "1024x576"})["size"] == (
        "1024x576"
    )
    with pytest.raises(DomainValidationError, match="does not support semantic"):
        normalize_generation_parameters(
            provider,
            {"resolution_key": "1k", "aspect_ratio_key": "1:1"},
        )


def test_image_model_without_matrix_preserves_native_aspect_ratio_schema_field():
    provider = {
        "kind": "image",
        "config_schema": {
            "type": "object",
            "properties": {
                "aspect_ratio": {"type": "string", "enum": ["1:1", "16:9"]}
            },
            "additionalProperties": False,
        },
        "default_parameters": {"aspect_ratio": "1:1"},
        "metadata": {},
    }
    assert normalize_generation_parameters(provider, {"aspect_ratio": "16:9"}) == {
        "aspect_ratio": "16:9"
    }

    provider["config_schema"]["properties"] = {}
    provider["default_parameters"] = {}
    with pytest.raises(DomainValidationError, match="Unsupported model parameter"):
        normalize_generation_parameters(provider, {"aspect_ratio": "16:9"})


def test_worker_uses_frozen_provider_parameters_without_semantic_re_resolution():
    persisted = {"size": "2048x1152", "quality": "high"}
    assert frozen_generation_parameters(persisted) == persisted
    assert frozen_generation_parameters(
        {
            "size": "2048x1152",
            "resolution": "2k",
            "aspect_ratio": "16:9",
            "resolution_key": "2k",
            "aspect_ratio_key": "16:9",
            "quality": "high",
        },
        image_provider(),
    ) == {"size": "2048x1152", "quality": "high"}
    with pytest.raises(ProviderGenerationError, match="unresolved image size"):
        frozen_generation_parameters(
            {"resolution_key": "2k", "aspect_ratio_key": "16:9"}
        )
    with pytest.raises(ProviderGenerationError, match="unresolved image size"):
        frozen_generation_parameters(
            {"size": "invalid", "resolution": "2k", "aspect_ratio": "16:9"},
            image_provider(),
        )

    native_provider = {
        "kind": "image",
        "config_schema": {},
        "default_parameters": {},
        "metadata": {},
    }
    native_parameters = {"size": "1024x1024", "aspect_ratio": "16:9"}
    assert frozen_generation_parameters(native_parameters, native_provider) == native_parameters


class FakeDatabase:
    def commit(self):
        return None

    def rollback(self):
        return None

    def refresh(self, _model):
        return None


class FakeAdminRepository:
    def __init__(self, model):
        self.model = model
        self.audit = None

    def get_model(self, _model_id):
        return self.model

    def add_audit(self, **values):
        self.audit = values


def configured_model():
    provider = image_provider()
    return SimpleNamespace(
        id=uuid4(),
        group_id=uuid4(),
        model_name="image-v1",
        base_url="https://provider.example/v1",
        display_name="Image V1",
        kind="image",
        description="",
        status="active",
        display_order=0,
        config_schema=provider["config_schema"],
        default_parameters=provider["default_parameters"],
        model_metadata=provider["metadata"],
    )


def test_admin_patch_validates_the_merged_model_configuration_before_mutating():
    model = configured_model()
    service = AdminService(FakeDatabase())
    service.repo = FakeAdminRepository(model)
    invalid_schema = image_schema("1024x1024")

    with pytest.raises(DomainValidationError, match="enum must match"):
        service.update_model(
            uuid4(), model.id, AdminModelUpdate(config_schema=invalid_schema)
        )
    assert model.config_schema != invalid_schema


def test_admin_kind_change_clears_an_unsubmitted_legacy_matrix():
    model = configured_model()
    service = AdminService(FakeDatabase())
    service.repo = FakeAdminRepository(model)

    updated = service.update_model(uuid4(), model.id, AdminModelUpdate(kind="video"))
    assert updated.kind == "video"
    assert IMAGE_SIZE_MATRIX_KEY not in updated.model_metadata
