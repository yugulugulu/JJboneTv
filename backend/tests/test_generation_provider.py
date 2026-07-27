import base64
from contextlib import contextmanager
import logging
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.config import settings
from app.core.model_credentials import encrypt_api_key
from app.database import SessionLocal
from app.exceptions import DomainValidationError
from app.main import app
from app.models import AIModel, ModelGroup, User, UserModelCredential
from app.services import generation_provider
from app.services.asset_storage import LocalAssetStorage
from app.services.generation_provider import (
    effective_generation_prompt,
    image_edit_endpoint,
    image_generation_endpoint,
    multipart_scalar_parameters,
    provider_prompt,
    validate_generation_parameters,
)
from app.services.model_catalog import validate_http_api_key


def provider_config():
    return {
        "config_schema": {
            "type": "object",
            "required": ["size", "quality", "n"],
            "properties": {
                "size": {"type": "string", "enum": ["1024x1024", "1536x1024"]},
                "quality": {"type": "string", "enum": ["low", "medium", "high"]},
                "n": {"type": "integer", "minimum": 1, "maximum": 4},
            },
            "additionalProperties": False,
        },
        "default_parameters": {"size": "1024x1024", "quality": "medium", "n": 1},
    }


def test_generation_parameters_merge_defaults_and_validate_schema():
    assert validate_generation_parameters(provider_config(), {"quality": "high"}) == {
        "size": "1024x1024",
        "quality": "high",
        "n": 1,
    }

    with pytest.raises(DomainValidationError, match="Unsupported value"):
        validate_generation_parameters(provider_config(), {"quality": "ultra"})
    with pytest.raises(DomainValidationError, match="Unsupported model parameter"):
        validate_generation_parameters(provider_config(), {"seed": 42})
    with pytest.raises(DomainValidationError, match="above maximum"):
        validate_generation_parameters(provider_config(), {"n": 5})


def test_openai_images_endpoint_supports_base_and_full_urls():
    assert image_generation_endpoint("https://www.thinkai.tv/v1", {}) == (
        "https://www.thinkai.tv/v1/images/generations"
    )
    assert image_generation_endpoint(
        "https://www.thinkai.tv/v1/images/generations", {}
    ) == "https://www.thinkai.tv/v1/images/generations"
    assert image_generation_endpoint(
        "https://provider.example/v1", {"endpoint_path": "/custom/images"}
    ) == "https://provider.example/v1/custom/images"
    assert image_edit_endpoint(
        "https://provider.example/v1",
        {"edit_endpoint_path": "/images/edits"},
    ) == "https://provider.example/v1/images/edits"


def test_image_edit_multipart_parameters_are_scalar_only():
    assert multipart_scalar_parameters(
        {"quality": "high", "n": 2, "strength": 0.5, "transparent": False}
    ) == {
        "quality": "high",
        "n": "2",
        "strength": "0.5",
        "transparent": "false",
    }
    with pytest.raises(DomainValidationError, match="scalar value"):
        multipart_scalar_parameters({"references": ["not", "scalar"]})
    with pytest.raises(DomainValidationError, match="reserved"):
        multipart_scalar_parameters({"prompt": "cannot override fixed prompt"})


def test_image_local_prompt_has_priority_and_text_predecessor_is_fallback():
    references = [
        {"node_id": str(uuid4()), "kind": "text", "prompt": "first input"},
        {"node_id": str(uuid4()), "kind": "text", "prompt": "second input"},
    ]
    assert provider_prompt("", references[:1]) == "first input"
    assert provider_prompt("legacy guidance", references) == "first input\n\nsecond input"
    assert provider_prompt("legacy guidance", []) == "legacy guidance"
    assert effective_generation_prompt(
        "image",
        "local edit instruction",
        [{"node_id": str(uuid4()), "kind": "image", "asset_id": str(uuid4())}],
    ) == "local edit instruction"
    assert effective_generation_prompt("image", "local edit instruction", references) == (
        "local edit instruction"
    )
    assert effective_generation_prompt("image", "", references) == "first input\n\nsecond input"


def test_provider_api_key_must_be_safe_for_authorization_header():
    assert validate_http_api_key("  sk-test_123+/=  ") == "sk-test_123+/="

    with pytest.raises(DomainValidationError, match="ASCII"):
        validate_http_api_key("请填写真实密钥")
    with pytest.raises(DomainValidationError, match="Bearer"):
        validate_http_api_key("Bearer sk-test")


def test_unexpected_generation_failure_logs_task_and_stage(monkeypatch, caplog):
    task_id = uuid4()
    lease_token = uuid4()
    failed_calls = []

    class BrokenTransaction:
        def __enter__(self):
            raise RuntimeError("database unavailable")

        def __exit__(self, *_):
            return False

    class BrokenSessionFactory:
        @staticmethod
        def begin():
            return BrokenTransaction()

    monkeypatch.setattr(generation_provider, "SessionLocal", BrokenSessionFactory)
    monkeypatch.setattr(
        generation_provider,
        "fail_task",
        lambda *args, **kwargs: failed_calls.append((args, kwargs)),
    )

    with caplog.at_level(logging.ERROR, logger=generation_provider.__name__):
        generation_provider.process_claimed_generation_task(task_id, lease_token)

    assert str(task_id) in caplog.text
    assert "stage load_task" in caplog.text
    assert "RuntimeError" in caplog.text
    assert failed_calls == [
        (
            (task_id, lease_token, "生成服务发生内部错误"),
            {"outcome_unknown": False},
        )
    ]


def test_openai_image_edit_uses_multipart_and_creates_new_asset(monkeypatch, tmp_path):
    image_bytes = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9ZP9sAAAAASUVORK5CYII="
    )
    requests: list[dict] = []

    class FakeProviderClient:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        @contextmanager
        def stream(self, method, url, *, headers, data, files):
            assert method == "POST"
            serialized_files = [
                (field, (filename, file.read(), mime_type))
                for field, (filename, file, mime_type) in files
            ]
            requests.append(
                {
                    "url": url,
                    "headers": headers,
                    "data": data,
                    "files": serialized_files,
                }
            )
            yield httpx.Response(
                200,
                json={"data": [{"b64_json": base64.b64encode(image_bytes).decode()}]},
            )

    monkeypatch.setattr(
        generation_provider, "provider_http_client", lambda _timeout: FakeProviderClient()
    )
    original_storage_root = settings.generated_storage_root
    original_storage_backend = settings.asset_storage_backend
    settings.generated_storage_root = str(tmp_path)
    settings.asset_storage_backend = "local"
    client = TestClient(app)
    email = f"image-edit-{uuid4()}@example.com"
    group_id = uuid4()
    model_id = uuid4()
    try:
        registered = client.post(
            "/api/auth/register", json={"email": email, "password": "Strong123"}
        )
        user_id = registered.json()["user"]["id"]
        with SessionLocal() as db:
            db.add(ModelGroup(id=group_id, name=f"Edit {uuid4()}", status="active"))
            db.flush()
            db.add(
                AIModel(
                    id=model_id,
                    group_id=group_id,
                    key=str(uuid4()),
                    model_name="gpt-image-edit",
                    base_url="https://provider.example/v1",
                    display_name="Image edit test",
                    kind="image",
                    status="active",
                    config_schema={
                        "type": "object",
                        "properties": {"quality": {"type": "string"}, "n": {"type": "integer"}},
                        "additionalProperties": False,
                    },
                    default_parameters={"quality": "high", "n": 1},
                    model_metadata={
                        "protocol": "openai_images",
                        "supported_input_kinds": ["image"],
                        "edit_protocol": "openai_images_multipart",
                        "edit_endpoint_path": "/images/edits",
                    },
                )
            )
            db.add(
                UserModelCredential(
                    user_id=user_id,
                    group_id=group_id,
                    encrypted_api_key=encrypt_api_key(db, "edit-provider-key"),
                    api_key_last_four="-key",
                )
            )
            db.commit()

        project = client.post("/api/projects", json={"name": "Image edit"})
        project_id = project.json()["id"]
        input_key = f"{user_id}/{project_id}/input.png"
        LocalAssetStorage().put_object(input_key, image_bytes, "image/png")
        input_asset = client.post(
            "/api/assets",
            json={
                "project_id": project_id,
                "kind": "image",
                "source": "upload",
                "storage_key": input_key,
                "mime_type": "image/png",
                "byte_size": len(image_bytes),
                "status": "ready",
            },
        )
        assert input_asset.status_code == 201, input_asset.text
        image_node_id = str(uuid4())
        text_node_id = str(uuid4())
        target_node_id = str(uuid4())

        def workflow_node(node_id, node_type, data):
            return {
                "id": node_id,
                "type": node_type,
                "position": {"x": 0, "y": 0},
                "size": {"width": 320, "height": 180},
                "data": data,
            }

        workflow = {
            "schema_version": 1,
            "viewport": {"x": 0, "y": 0, "zoom": 1},
            "nodes": [
                workflow_node(image_node_id, "image", {"asset_id": input_asset.json()["id"]}),
                workflow_node(text_node_id, "prompt", {"prompt": "make the cup blue"}),
                workflow_node(target_node_id, "image", {}),
            ],
            "edges": [
                {
                    "id": str(uuid4()),
                    "source_node_id": image_node_id,
                    "target_node_id": target_node_id,
                    "data": {},
                },
                {
                    "id": str(uuid4()),
                    "source_node_id": text_node_id,
                    "target_node_id": target_node_id,
                    "data": {},
                },
            ],
        }
        saved = client.put(
            f"/api/projects/{project_id}/workflow",
            json={"base_revision": 0, "workflow": workflow},
        )
        assert saved.status_code == 200, saved.text
        generation = client.post(
            "/api/generations",
            headers={"Idempotency-Key": str(uuid4())},
            json={
                "project_id": project_id,
                "node_id": target_node_id,
                "input_node_ids": [image_node_id, text_node_id],
                "input_revision": 1,
                "kind": "image",
                "model": str(model_id),
                "parameters": {},
            },
        )
        assert generation.status_code == 202, generation.text
        generation_provider.run_generation_task(UUID(generation.json()["id"]))
        completed = client.get(f"/api/generations/{generation.json()['id']}").json()
        assert completed["status"] == "succeeded"
        assert completed["outputs"][0]["asset"]["id"] != input_asset.json()["id"]
        assert client.get(f"/api/assets/{input_asset.json()['id']}").status_code == 200
        assert requests == [
            {
                "url": "https://provider.example/v1/images/edits",
                "headers": {"Authorization": "Bearer edit-provider-key"},
                "data": {
                    "model": "gpt-image-edit",
                    "prompt": "make the cup blue",
                    "quality": "high",
                    "n": "1",
                },
                "files": [("image", ("input-1.png", image_bytes, "image/png"))],
            }
        ]
    finally:
        settings.generated_storage_root = original_storage_root
        settings.asset_storage_backend = original_storage_backend
        with SessionLocal() as db:
            user = db.scalar(select(User).where(User.email == email))
            if user is not None:
                db.delete(user)
            db.execute(delete(AIModel).where(AIModel.id == model_id))
            db.execute(delete(ModelGroup).where(ModelGroup.id == group_id))
            db.commit()


def test_openai_images_task_creates_private_project_asset(monkeypatch, tmp_path):
    image_bytes = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9ZP9sAAAAASUVORK5CYII="
    )

    class FakeProviderClient:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        @contextmanager
        def stream(self, method, url, *, headers, json):
            assert method == "POST"
            assert url == "https://provider.example/v1/images/generations"
            assert headers["Authorization"] == "Bearer test-provider-key"
            assert json == {
                "model": "gpt-image-2",
                "prompt": "use a ceramic material",
                "size": "1024x1024",
                "quality": "high",
                "n": 1,
            }
            yield httpx.Response(
                200,
                json={"created": 123, "data": [{"b64_json": base64.b64encode(image_bytes).decode()}]},
            )

    monkeypatch.setattr(
        generation_provider, "provider_http_client", lambda timeout: FakeProviderClient()
    )
    original_storage_root = settings.generated_storage_root
    original_storage_backend = settings.asset_storage_backend
    settings.generated_storage_root = str(tmp_path)
    settings.asset_storage_backend = "local"
    email = f"provider-{uuid4()}@example.com"
    other_email = f"provider-other-{uuid4()}@example.com"
    group_id = uuid4()
    model_id = uuid4()
    client = TestClient(app)
    other_client = TestClient(app)

    try:
        registered = client.post(
            "/api/auth/register", json={"email": email, "password": "Strong123"}
        )
        assert registered.status_code == 201
        user_id = registered.json()["user"]["id"]
        other_registered = other_client.post(
            "/api/auth/register",
            json={"email": other_email, "password": "Strong123"},
        )
        assert other_registered.status_code == 201
        other_user_id = other_registered.json()["user"]["id"]
        with SessionLocal() as db:
            db.add(ModelGroup(id=group_id, name=f"Provider {uuid4()}", status="active"))
            db.flush()
            db.add(
                AIModel(
                    id=model_id,
                    group_id=group_id,
                    key=str(uuid4()),
                    model_name="gpt-image-2",
                    base_url="https://provider.example/v1",
                    display_name="GPT Image 2 test",
                    kind="image",
                    status="active",
                    config_schema=provider_config()["config_schema"],
                    default_parameters=provider_config()["default_parameters"],
                    model_metadata={"protocol": "openai_images"},
                )
            )
            db.flush()
            db.add(
                UserModelCredential(
                    user_id=user_id,
                    group_id=group_id,
                    encrypted_api_key=encrypt_api_key(db, "test-provider-key"),
                    api_key_last_four="-key",
                )
            )
            db.commit()

        source_node_id = str(uuid4())
        node_id = str(uuid4())
        project = client.post(
            "/api/projects",
            json={
                "name": "Provider test",
                "project_type": "image",
                "workflow": {
                    "schema_version": 1,
                    "viewport": {"x": 0, "y": 0, "zoom": 1},
                    "nodes": [
                        {
                            "id": source_node_id,
                            "type": "prompt",
                            "position": {"x": -360, "y": 0},
                            "size": {"width": 320, "height": 180},
                            "data": {
                                "title": "Material context",
                                "prompt": "use a ceramic material",
                            },
                        },
                        {
                            "id": node_id,
                            "type": "image",
                            "position": {"x": 0, "y": 0},
                            "size": {"width": 320, "height": 240},
                            "data": {"title": "Generated image", "prompt": "a white cup"},
                        }
                    ],
                    "edges": [
                        {
                            "id": str(uuid4()),
                            "source_node_id": source_node_id,
                            "target_node_id": node_id,
                            "data": {},
                        }
                    ],
                },
            },
        )
        assert project.status_code == 201, project.text
        generation = client.post(
            "/api/generations",
            headers={"Idempotency-Key": str(uuid4())},
            json={
                "project_id": project.json()["id"],
                "node_id": node_id,
                "input_node_ids": [source_node_id],
                "input_revision": 1,
                "kind": "image",
                "model": str(model_id),
                "prompt": "",
                "parameters": {"quality": "high"},
            },
        )
        assert generation.status_code == 202, generation.text
        generation_provider.run_generation_task(UUID(generation.json()["id"]))
        completed = client.get(f"/api/generations/{generation.json()['id']}")
        assert completed.status_code == 200
        assert completed.json()["status"] == "succeeded"
        assert completed.json()["parameters"]["_input_references"] == [
                {
                    "node_id": source_node_id,
                    "kind": "text",
                    "prompt": "use a ceramic material",
                    "ordinal": 0,
                }
            ]
        asset = completed.json()["outputs"][0]["asset"]
        assert asset["width"] == 1024
        assert asset["height"] == 1024
        content = client.get(f"/api/assets/{asset['id']}/content")
        assert content.status_code == 200
        assert content.content == image_bytes
        assert "content-disposition" not in content.headers
        download = client.get(f"/api/assets/{asset['id']}/content?download=1")
        assert download.status_code == 200
        assert download.content == image_bytes
        assert download.headers["content-disposition"].startswith("attachment;")
        assert "gpt-image-2-0.png" in download.headers["content-disposition"]
        assert download.headers["x-content-type-options"] == "nosniff"
        assert other_client.get(f"/api/assets/{asset['id']}/content").status_code == 404
        assert TestClient(app).get(f"/api/assets/{asset['id']}/content").status_code == 401

        other_project = other_client.post("/api/projects", json={"name": "No cross access"})
        assert other_project.status_code == 201
        forged_storage_key = (
            f"{other_user_id}/{other_project.json()['id']}/../../{asset['storage_key']}"
        )
        forged_asset = other_client.post(
            "/api/assets",
            json={
                "project_id": other_project.json()["id"],
                "kind": "image",
                "source": "upload",
                "storage_key": forged_storage_key,
                "mime_type": "image/png",
                "byte_size": len(image_bytes),
                "status": "ready",
            },
        )
        assert forged_asset.status_code == 422
        assert forged_asset.json() == {
            "detail": "Media storage key must belong to the authenticated user and project"
        }

        referenced_workflow = project.json()["workflow"]
        referenced_workflow["nodes"][0]["data"]["asset_id"] = asset["id"]
        referenced_workflow["nodes"][0]["data"]["generation_task_id"] = generation.json()["id"]
        saved = client.put(
            f"/api/projects/{project.json()['id']}/workflow",
            json={
                "base_revision": 1,
                "workflow": referenced_workflow,
                "reason": "manual",
            },
        )
        assert saved.status_code == 200, saved.text
        manual_version_id = client.get(
            f"/api/projects/{project.json()['id']}/versions"
        ).json()["items"][0]["id"]

        in_use = client.delete(f"/api/assets/{asset['id']}")
        assert in_use.status_code == 422
        assert in_use.json() == {
            "detail": "Remove the asset from the current workflow before deleting it"
        }

        referenced_workflow["nodes"][0]["data"]["asset_id"] = None
        removed = client.put(
            f"/api/projects/{project.json()['id']}/workflow",
            json={"base_revision": 2, "workflow": referenced_workflow, "reason": "manual"},
        )
        assert removed.status_code == 200, removed.text
        assert client.delete(f"/api/assets/{asset['id']}").status_code == 204
        assert client.get(f"/api/assets/{asset['id']}/content").status_code == 404

        restored = client.post(
            f"/api/projects/{project.json()['id']}/versions/{manual_version_id}/restore"
        )
        assert restored.status_code == 200, restored.text
        assert client.get(f"/api/assets/{asset['id']}/content").content == image_bytes
    finally:
        settings.generated_storage_root = original_storage_root
        settings.asset_storage_backend = original_storage_backend
        with SessionLocal() as db:
            users = list(db.scalars(select(User).where(User.email.in_([email, other_email]))))
            for user in users:
                db.delete(user)
            db.execute(delete(AIModel).where(AIModel.id == model_id))
            db.execute(delete(ModelGroup).where(ModelGroup.id == group_id))
            db.commit()
