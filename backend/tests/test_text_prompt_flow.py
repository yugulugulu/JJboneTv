"""Integration coverage for platform text models and homepage image generation."""

from uuid import UUID, uuid4

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select

from app.admin_main import app as admin_app
from app.config import settings
from app.database import SessionLocal
from app.main import app as public_app
from app.models import (
    AdminAccount,
    AdminAuditLog,
    AIModel,
    GenerationTask,
    ModelGroup,
    ModelProviderCredential,
    Project,
    ProjectVersion,
    User,
)
from app.services import prompt_optimization


class FakePromptResponse:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class FakePromptClient:
    def __init__(self, state: dict):
        self.state = state

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def post(self, url, *, headers, json):
        self.state["request"] = {"url": url, "headers": headers, "json": json}
        if self.state.get("error") is not None:
            raise self.state["error"]
        return FakePromptResponse(
            self.state.get("status_code", 200),
            self.state.get(
                "payload",
                {"choices": [{"message": {"content": "完整的红色火箭图片提示词"}}]},
            ),
        )


def test_platform_text_model_prompt_optimization_and_atomic_homepage_generation(monkeypatch):
    admin_email = f"text-flow-admin-{uuid4()}@example.com"
    admin_password = "AdminStrongPassword123"
    user_email = f"text-flow-user-{uuid4()}@example.com"
    other_email = f"text-flow-other-{uuid4()}@example.com"
    model_ids: list[UUID] = []
    group_ids: list[UUID] = []
    project_id: UUID | None = None
    original_bootstrap = (
        settings.admin_bootstrap_enabled,
        settings.admin_bootstrap_email,
        settings.admin_bootstrap_password,
        settings.admin_cookie_secure,
    )
    settings.admin_bootstrap_enabled = True
    settings.admin_bootstrap_email = admin_email
    settings.admin_bootstrap_password = admin_password
    settings.admin_cookie_secure = False

    prompt_state: dict = {}
    monkeypatch.setattr(
        prompt_optimization,
        "prompt_http_client",
        lambda _timeout: FakePromptClient(prompt_state),
    )

    try:
        with (
            TestClient(admin_app) as admin_client,
            TestClient(public_app) as user_client,
            TestClient(public_app) as other_client,
        ):
            assert admin_client.post(
                "/admin-api/auth/login",
                json={"email": admin_email, "password": admin_password},
            ).status_code == 200
            registered = user_client.post(
                "/api/auth/register",
                json={"email": user_email, "password": "Strong123"},
            )
            assert registered.status_code == 201, registered.text
            user_id = UUID(registered.json()["user"]["id"])
            assert other_client.post(
                "/api/auth/register",
                json={"email": other_email, "password": "Strong123"},
            ).status_code == 201

            text_group_response = admin_client.post(
                "/admin-api/model-groups",
                json={
                    "name": f"Text provider {uuid4().hex}",
                    "description": "User-configured text model credentials",
                    "status": "active",
                },
            )
            assert text_group_response.status_code == 201, text_group_response.text
            text_group_id = UUID(text_group_response.json()["id"])
            group_ids.append(text_group_id)

            rejected_admin_key = admin_client.post(
                "/admin-api/text-models",
                json={
                    "group_id": str(text_group_id),
                    "base_url": "https://text-provider.example/v1",
                    "model_name": "gpt-text-test",
                    "api_key": "must-be-configured-by-user",
                },
            )
            assert rejected_admin_key.status_code == 422

            created_text = admin_client.post(
                "/admin-api/text-models",
                json={
                    "group_id": str(text_group_id),
                    "base_url": "https://text-provider.example/v1",
                    "model_name": "gpt-text-test",
                },
            )
            assert created_text.status_code == 201, created_text.text
            text_model = created_text.json()
            text_model_id = UUID(text_model["id"])
            model_ids.append(text_model_id)
            assert UUID(text_model["group_id"]) == text_group_id
            assert "api_key" not in text_model
            assert text_model["kind"] == "text"
            assert text_model["protocol"] == "chat_completions"
            assert text_model["credential_configured"] is False
            assert text_model["api_key_last_four"] is None

            with SessionLocal() as db:
                assert db.get(ModelProviderCredential, text_model_id) is None
                assert db.get(ModelGroup, text_group_id).status == "active"

            groups = user_client.get("/api/model-groups")
            assert groups.status_code == 200
            text_group = next(
                item for item in groups.json()["items"] if UUID(item["id"]) == text_group_id
            )
            assert text_group["credential_configured"] is False
            assert text_model_id in {UUID(item["id"]) for item in text_group["models"]}

            available = user_client.get("/api/models")
            assert available.status_code == 200
            assert text_model_id not in {UUID(item["id"]) for item in available.json()["items"]}
            assert other_client.get("/api/models").json()["items"] == []

            saved_text_credential = user_client.put(
                f"/api/model-groups/{text_group_id}/credential",
                json={"api_key": "user-text-key-9876"},
            )
            assert saved_text_credential.status_code == 200, saved_text_credential.text
            assert saved_text_credential.json()["api_key_last_four"] == "9876"

            available = user_client.get("/api/models")
            assert available.status_code == 200
            assert text_model_id in {
                UUID(item["id"]) for item in available.json()["items"] if item["kind"] == "text"
            }
            assert text_model_id not in {
                UUID(item["id"]) for item in other_client.get("/api/models").json()["items"]
            }

            optimized = user_client.post(
                "/api/prompt-optimizations",
                json={
                    "model_id": str(text_model_id),
                    "target_kind": "image",
                    "input": "我想要生成一个红色的火箭",
                },
            )
            assert optimized.status_code == 200, optimized.text
            assert optimized.json()["optimized_prompt"] == "完整的红色火箭图片提示词"
            assert prompt_state["request"]["url"] == (
                "https://text-provider.example/v1/chat/completions"
            )
            assert prompt_state["request"]["headers"]["Authorization"] == (
                "Bearer user-text-key-9876"
            )
            assert prompt_state["request"]["json"]["model"] == "gpt-text-test"
            assert [message["role"] for message in prompt_state["request"]["json"]["messages"]] == [
                "system",
                "user",
            ]
            assert "api_key" not in str(prompt_state["request"]["json"]).lower()

            assert user_client.post(
                "/api/prompt-optimizations",
                json={
                    "model_id": str(text_model_id),
                    "target_kind": "audio",
                    "input": "不允许的用途",
                },
            ).status_code == 422

            prompt_state["payload"] = {"choices": []}
            malformed = user_client.post(
                "/api/prompt-optimizations",
                json={
                    "model_id": str(text_model_id),
                    "target_kind": "video",
                    "input": "火箭升空",
                },
            )
            assert malformed.status_code == 502
            prompt_state.pop("payload")
            prompt_state["error"] = httpx.ReadTimeout("timeout")
            timed_out = user_client.post(
                "/api/prompt-optimizations",
                json={
                    "model_id": str(text_model_id),
                    "target_kind": "image",
                    "input": "火箭",
                },
            )
            assert timed_out.status_code == 504
            prompt_state.pop("error")

            group = admin_client.post(
                "/admin-api/model-groups",
                json={
                    "name": f"Homepage image provider {uuid4().hex}",
                    "description": "Atomic homepage generation test",
                    "status": "active",
                },
            )
            assert group.status_code == 201, group.text
            group_id = UUID(group.json()["id"])
            group_ids.append(group_id)
            image = admin_client.post(
                "/admin-api/models",
                json={
                    "group_id": str(group_id),
                    "model_name": "image-homepage-test",
                    "base_url": "https://image-provider.example/v1",
                    "display_name": "Homepage Image Test",
                    "kind": "image",
                    "status": "active",
                    "config_schema": {
                        "type": "object",
                        "required": ["size", "quality"],
                        "properties": {
                            "size": {"type": "string", "enum": ["1024x1024"]},
                            "quality": {
                                "type": "string",
                                "enum": ["low", "medium", "high"],
                            },
                        },
                        "additionalProperties": False,
                    },
                    "default_parameters": {"size": "1024x1024", "quality": "medium"},
                    "metadata": {
                        "protocol": "openai_images",
                        "image_size_matrix": {
                            "schema_version": 1,
                            "default_resolution_key": "1k",
                            "default_aspect_ratio_key": "1:1",
                            "resolutions": [
                                {
                                    "key": "1k",
                                    "display_name": "1K",
                                    "ratios": [
                                        {
                                            "key": "1:1",
                                            "display_name": "1:1",
                                            "size": "1024x1024",
                                        }
                                    ],
                                }
                            ],
                        },
                    },
                },
            )
            assert image.status_code == 201, image.text
            image_model_id = UUID(image.json()["id"])
            model_ids.append(image_model_id)
            assert user_client.put(
                f"/api/model-groups/{group_id}/credential",
                json={"api_key": "user-image-key-1234"},
            ).status_code == 200

            idempotency_key = f"homepage-{uuid4()}"
            generation_payload = {
                "prompt": "一枚红色火箭从海边发射",
                "image_model_id": str(image_model_id),
                "parameters": {
                    "resolution": "1k",
                    "aspect_ratio": "1:1",
                    "quality": "high",
                },
                "name": "红色火箭",
            }
            generated = user_client.post(
                "/api/image-project-generations",
                headers={"Idempotency-Key": idempotency_key},
                json=generation_payload,
            )
            assert generated.status_code == 202, generated.text
            generated_body = generated.json()
            project_id = UUID(generated_body["project"]["id"])
            task_id = UUID(generated_body["generation"]["id"])
            prompt_node_id = UUID(generated_body["prompt_node_id"])
            image_node_id = UUID(generated_body["image_node_id"])
            workflow = generated_body["project"]["workflow"]
            assert generated_body["project"]["current_revision"] == 1
            assert len(workflow["nodes"]) == 2
            assert len(workflow["edges"]) == 1
            assert workflow["edges"][0]["source_node_id"] == str(prompt_node_id)
            assert workflow["edges"][0]["target_node_id"] == str(image_node_id)
            image_node = next(node for node in workflow["nodes"] if node["type"] == "image")
            prompt_node = next(node for node in workflow["nodes"] if node["type"] == "prompt")
            assert prompt_node["data"]["prompt"] == generation_payload["prompt"]
            assert image_node["data"]["generation_task_id"] == str(task_id)
            assert image_node["data"]["parameters"] == {
                "size": "1024x1024",
                "quality": "high",
            }
            assert generated_body["generation"]["parameters"]["size"] == "1024x1024"
            assert generated_body["generation"]["parameters"]["quality"] == "high"
            assert generated_body["generation"]["parameters"]["_input_references"] == [
                {
                    "node_id": str(prompt_node_id),
                    "kind": "text",
                    "prompt": generation_payload["prompt"],
                    "ordinal": 0,
                }
            ]

            replay = user_client.post(
                "/api/image-project-generations",
                headers={"Idempotency-Key": idempotency_key},
                json=generation_payload,
            )
            assert replay.status_code == 202, replay.text
            assert replay.headers["Idempotency-Replayed"] == "true"
            assert replay.json()["project"]["id"] == str(project_id)
            assert replay.json()["generation"]["id"] == str(task_id)

            conflict_payload = {**generation_payload, "prompt": "另一个提示词"}
            assert user_client.post(
                "/api/image-project-generations",
                headers={"Idempotency-Key": idempotency_key},
                json=conflict_payload,
            ).status_code == 409

            reconciled = user_client.get(
                f"/api/projects/{project_id}/generations",
                params={"latest_per_node": "true"},
            )
            assert reconciled.status_code == 200, reconciled.text
            assert [item["id"] for item in reconciled.json()["items"]] == [str(task_id)]
            assert other_client.get(
                f"/api/projects/{project_id}/generations"
            ).status_code == 404

            with SessionLocal() as db:
                assert db.scalar(
                    select(func.count(Project.id)).where(Project.id == project_id)
                ) == 1
                assert db.scalar(
                    select(func.count(ProjectVersion.id)).where(
                        ProjectVersion.project_id == project_id,
                        ProjectVersion.version_no == 1,
                    )
                ) == 1
                task = db.get(GenerationTask, task_id)
                assert task is not None
                assert task.owner_id == user_id
                assert task.node_id == image_node_id
                assert task.input_revision == 1
                assert task.parameters["size"] == "1024x1024"
                assert not {
                    "resolution",
                    "aspect_ratio",
                    "resolution_key",
                    "aspect_ratio_key",
                }.intersection(task.parameters)
    finally:
        (
            settings.admin_bootstrap_enabled,
            settings.admin_bootstrap_email,
            settings.admin_bootstrap_password,
            settings.admin_cookie_secure,
        ) = original_bootstrap
        with SessionLocal() as db:
            users = list(
                db.scalars(select(User).where(User.email.in_([user_email, other_email])))
            )
            for user in users:
                db.delete(user)
            if model_ids:
                db.execute(delete(AdminAuditLog).where(AdminAuditLog.entity_id.in_(model_ids)))
                db.execute(delete(AIModel).where(AIModel.id.in_(model_ids)))
            if group_ids:
                db.execute(delete(AdminAuditLog).where(AdminAuditLog.entity_id.in_(group_ids)))
                db.execute(delete(ModelGroup).where(ModelGroup.id.in_(group_ids)))
            db.execute(delete(AdminAccount).where(AdminAccount.email == admin_email))
            db.commit()
