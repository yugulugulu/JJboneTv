from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.admin_main import app as admin_app
from app.config import settings
from app.database import SessionLocal
from app.models import AdminAccount, AdminAuditLog, AIModel, ModelGroup


def test_admin_text_model_create_and_update_contract():
    admin_email = f"text-model-admin-{uuid4()}@example.com"
    admin_password = "AdminStrongPassword123"
    group_ids: list[UUID] = []
    model_ids: list[UUID] = []
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

    try:
        with TestClient(admin_app) as admin_client:
            assert admin_client.post(
                "/admin-api/text-models",
                json={
                    "group_id": str(uuid4()),
                    "base_url": "https://text-provider.example/v1",
                    "model_name": "unauthenticated-model",
                },
            ).status_code == 401
            assert admin_client.patch(
                f"/admin-api/text-models/{uuid4()}",
                json={
                    "group_id": str(uuid4()),
                    "base_url": "https://text-provider.example/v1",
                    "model_name": "unauthenticated-model",
                },
            ).status_code == 401

            login = admin_client.post(
                "/admin-api/auth/login",
                json={"email": admin_email, "password": admin_password},
            )
            assert login.status_code == 200, login.text

            def create_group(name: str, status: str) -> UUID:
                response = admin_client.post(
                    "/admin-api/model-groups",
                    json={
                        "name": f"{name} {uuid4().hex}",
                        "description": f"{status} text model group",
                        "status": status,
                    },
                )
                assert response.status_code == 201, response.text
                group_id = UUID(response.json()["id"])
                group_ids.append(group_id)
                return group_id

            active_group_id = create_group("Active text provider A", "active")
            target_group_id = create_group("Active text provider B", "active")
            disabled_group_id = create_group("Disabled text provider", "disabled")

            missing_group_id = admin_client.post(
                "/admin-api/text-models",
                json={
                    "base_url": "https://text-provider.example/v1",
                    "model_name": "missing-group",
                },
            )
            assert missing_group_id.status_code == 422

            invalid_group_id = admin_client.post(
                "/admin-api/text-models",
                json={
                    "group_id": "not-a-uuid",
                    "base_url": "https://text-provider.example/v1",
                    "model_name": "invalid-group",
                },
            )
            assert invalid_group_id.status_code == 422

            nonexistent_group = admin_client.post(
                "/admin-api/text-models",
                json={
                    "group_id": str(uuid4()),
                    "base_url": "https://text-provider.example/v1",
                    "model_name": "missing-group-record",
                },
            )
            assert nonexistent_group.status_code == 404

            inactive_group = admin_client.post(
                "/admin-api/text-models",
                json={
                    "group_id": str(disabled_group_id),
                    "base_url": "https://text-provider.example/v1",
                    "model_name": "disabled-group-model",
                },
            )
            assert inactive_group.status_code == 409
            assert inactive_group.json()["code"] == "MODEL_GROUP_NOT_ACTIVE"

            created = admin_client.post(
                "/admin-api/text-models",
                json={
                    "group_id": str(active_group_id),
                    "base_url": "https://text-provider.example/v1",
                    "model_name": "gpt-text-original",
                },
            )
            assert created.status_code == 201, created.text
            created_payload = created.json()
            text_model_id = UUID(created_payload["id"])
            model_ids.append(text_model_id)
            assert UUID(created_payload["group_id"]) == active_group_id
            assert created_payload["kind"] == "text"
            assert created_payload["status"] == "active"
            assert created_payload["display_name"] == "gpt-text-original"
            assert created_payload["protocol"] == "chat_completions"

            archived_created = admin_client.post(
                "/admin-api/text-models",
                json={
                    "group_id": str(active_group_id),
                    "base_url": "https://archived-text-provider.example/v1",
                    "model_name": "gpt-text-archived",
                },
            )
            assert archived_created.status_code == 201, archived_created.text
            archived_model_id = UUID(archived_created.json()["id"])
            model_ids.append(archived_model_id)

            with SessionLocal() as db:
                archived_model = db.get(AIModel, archived_model_id)
                assert archived_model is not None
                archived_model.status = "archived"
                db.commit()

            archived_patch = admin_client.patch(
                f"/admin-api/text-models/{archived_model_id}",
                json={
                    "group_id": str(target_group_id),
                    "base_url": "https://must-not-change.example/v1",
                    "model_name": "must-not-change-archived",
                },
            )
            assert archived_patch.status_code == 409
            assert archived_patch.json()["detail"] == "Archived models cannot be changed"

            with SessionLocal() as db:
                archived_model = db.get(AIModel, archived_model_id)
                assert archived_model is not None
                assert archived_model.group_id == active_group_id
                assert archived_model.base_url == "https://archived-text-provider.example/v1"
                assert archived_model.model_name == "gpt-text-archived"
                assert archived_model.status == "archived"

            with SessionLocal() as db:
                text_model = db.get(AIModel, text_model_id)
                assert text_model is not None
                text_model.status = "disabled"
                text_model.description = "Must survive simplified text model edits"
                text_model.display_order = 27
                text_model.config_schema = {"temperature": {"type": "number"}}
                text_model.default_parameters = {"temperature": 0.25}
                text_model.model_metadata = {
                    "protocol": "legacy-value",
                    "custom": {"keep": True},
                }
                db.commit()

            incomplete_patch = admin_client.patch(
                f"/admin-api/text-models/{text_model_id}",
                json={
                    "group_id": str(target_group_id),
                    "base_url": "https://new-text-provider.example/v1",
                },
            )
            assert incomplete_patch.status_code == 422

            inactive_patch = admin_client.patch(
                f"/admin-api/text-models/{text_model_id}",
                json={
                    "group_id": str(disabled_group_id),
                    "base_url": "https://new-text-provider.example/v1",
                    "model_name": "gpt-text-new",
                },
            )
            assert inactive_patch.status_code == 409
            assert inactive_patch.json()["code"] == "MODEL_GROUP_NOT_ACTIVE"

            missing_model = admin_client.patch(
                f"/admin-api/text-models/{uuid4()}",
                json={
                    "group_id": str(target_group_id),
                    "base_url": "https://new-text-provider.example/v1",
                    "model_name": "gpt-text-new",
                },
            )
            assert missing_model.status_code == 404

            updated = admin_client.patch(
                f"/admin-api/text-models/{text_model_id}",
                json={
                    "group_id": str(target_group_id),
                    "base_url": "https://new-text-provider.example/v1",
                    "model_name": "gpt-text-new",
                },
            )
            assert updated.status_code == 200, updated.text
            updated_payload = updated.json()
            assert UUID(updated_payload["group_id"]) == target_group_id
            assert updated_payload["base_url"] == "https://new-text-provider.example/v1"
            assert updated_payload["model_name"] == "gpt-text-new"
            assert updated_payload["display_name"] == "gpt-text-new"
            assert updated_payload["status"] == "disabled"
            assert updated_payload["description"] == "Must survive simplified text model edits"
            assert updated_payload["display_order"] == 27
            assert updated_payload["config_schema"] == {
                "temperature": {"type": "number"}
            }
            assert updated_payload["default_parameters"] == {"temperature": 0.25}
            assert updated_payload["metadata"] == {
                "protocol": "chat_completions",
                "custom": {"keep": True},
            }
            assert updated_payload["protocol"] == "chat_completions"

            image_model = admin_client.post(
                "/admin-api/models",
                json={
                    "group_id": str(active_group_id),
                    "model_name": "image-model",
                    "base_url": "https://image-provider.example/v1",
                    "display_name": "Image model",
                    "kind": "image",
                    "status": "active",
                },
            )
            assert image_model.status_code == 201, image_model.text
            image_model_id = UUID(image_model.json()["id"])
            model_ids.append(image_model_id)

            wrong_kind = admin_client.patch(
                f"/admin-api/text-models/{image_model_id}",
                json={
                    "group_id": str(target_group_id),
                    "base_url": "https://new-text-provider.example/v1",
                    "model_name": "must-not-update-image",
                },
            )
            assert wrong_kind.status_code == 409
            assert wrong_kind.json()["code"] == "MODEL_KIND_MISMATCH"

            with SessionLocal() as db:
                stored = db.get(AIModel, text_model_id)
                assert stored is not None
                assert stored.group_id == target_group_id
                assert stored.status == "disabled"
                assert stored.config_schema == {"temperature": {"type": "number"}}
                assert stored.default_parameters == {"temperature": 0.25}
                assert stored.model_metadata == {
                    "protocol": "chat_completions",
                    "custom": {"keep": True},
                }
                audit = db.scalar(
                    select(AdminAuditLog).where(
                        AdminAuditLog.entity_id == text_model_id,
                        AdminAuditLog.action == "model.text.update",
                    )
                )
                assert audit is not None
                assert audit.changes["before"]["group_id"] == str(active_group_id)
                assert audit.changes["after"]["group_id"] == str(target_group_id)
                assert audit.changes["before"]["status"] == "disabled"
                assert audit.changes["after"]["status"] == "disabled"
                assert audit.changes["before"]["metadata"]["custom"] == {"keep": True}
                assert audit.changes["after"]["metadata"] == {
                    "protocol": "chat_completions",
                    "custom": {"keep": True},
                }
    finally:
        (
            settings.admin_bootstrap_enabled,
            settings.admin_bootstrap_email,
            settings.admin_bootstrap_password,
            settings.admin_cookie_secure,
        ) = original_bootstrap
        with SessionLocal() as db:
            if model_ids:
                db.execute(delete(AdminAuditLog).where(AdminAuditLog.entity_id.in_(model_ids)))
                db.execute(delete(AIModel).where(AIModel.id.in_(model_ids)))
            if group_ids:
                db.execute(delete(AdminAuditLog).where(AdminAuditLog.entity_id.in_(group_ids)))
                db.execute(delete(ModelGroup).where(ModelGroup.id.in_(group_ids)))
            db.execute(delete(AdminAccount).where(AdminAccount.email == admin_email))
            db.commit()
