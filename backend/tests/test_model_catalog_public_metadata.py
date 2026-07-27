from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

from app.services.model_catalog import public_model, sanitize_public_metadata


def catalog_model(metadata: dict):
    return SimpleNamespace(
        id=uuid4(),
        group_id=uuid4(),
        model_name="gpt-image-test",
        display_name="Image Test",
        kind="image",
        description="",
        display_order=0,
        config_schema={},
        default_parameters={},
        model_metadata=metadata,
    )


def test_public_model_recursively_removes_sensitive_metadata_and_keeps_safe_config():
    matrix = {
        "schema_version": 1,
        "default_resolution_key": "1k",
        "default_aspect_ratio_key": "1:1",
        "resolutions": [
            {
                "key": "1k",
                "display_name": "1K",
                "ratios": [
                    {"key": "1:1", "display_name": "1:1", "size": "1024x1024"}
                ],
            }
        ],
    }
    metadata = {
        "protocol": "openai_images",
        "edit_protocol": "openai_images_multipart",
        "endpoint_path": "/images/generations",
        "supported_input_kinds": ["text", "image"],
        "image_size_matrix": matrix,
        "parameter_ui": {"quality": {"label": "画质", "options": ["high"]}},
        "api_key": "top-level-secret",
        "base-url": "https://private-provider.example/v1",
        "nested": {
            "APIKey": "nested-secret",
            "baseUrl": "https://nested-private.example/v1",
            "credential": {"access_token": "token-secret"},
            "encrypted_payload": "ciphertext",
            "safe_label": "公开说明",
        },
        "items": [
            {"Authorization": "Bearer secret", "display_name": "Visible"},
            {"client_secret": "secret", "quality": "high"},
        ],
    }
    original = deepcopy(metadata)

    result = public_model(catalog_model(metadata))

    assert result["metadata"] == {
        "protocol": "openai_images",
        "edit_protocol": "openai_images_multipart",
        "endpoint_path": "/images/generations",
        "supported_input_kinds": ["text", "image"],
        "image_size_matrix": matrix,
        "parameter_ui": {"quality": {"label": "画质", "options": ["high"]}},
        "nested": {"safe_label": "公开说明"},
        "items": [{"display_name": "Visible"}, {"quality": "high"}],
    }
    assert "base_url" not in result
    assert metadata == original


def test_public_metadata_filter_normalizes_key_case_and_separators():
    assert sanitize_public_metadata(
        {
            "provider_api-key": "secret",
            "Provider Base URL": "https://private.example",
            "password_hash": "secret",
            "private_key_pem": "secret",
            "refresh-token": "secret",
            "secret_value": "secret",
            "encrypted_blob": "ciphertext",
            "token_limit": 8192,
            "storage_key": "safe/object/path.png",
        }
    ) == {
        "token_limit": 8192,
        "storage_key": "safe/object/path.png",
    }
