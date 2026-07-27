from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import delete, select

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
    Project,
    User,
    UserModelCredential,
)
from app.services.admin import AdminService


def test_admin_auth_users_models_and_audit_contract():
    admin_email = f"admin-{uuid4()}@example.com"
    admin_password = "AdminStrongPassword123"
    user_email = f"managed-{uuid4()}@example.com"
    created_model_ids = []
    created_group_id = None
    original_bootstrap_email = settings.admin_bootstrap_email
    original_bootstrap_password = settings.admin_bootstrap_password
    original_bootstrap_enabled = settings.admin_bootstrap_enabled
    original_cookie_secure = settings.admin_cookie_secure
    settings.admin_bootstrap_enabled = True
    settings.admin_bootstrap_email = admin_email
    settings.admin_bootstrap_password = admin_password
    settings.admin_cookie_secure = False

    try:
        with TestClient(admin_app) as admin_client, TestClient(public_app) as user_client:
            assert admin_client.get("/admin-api/health").status_code == 200
            assert admin_client.get("/admin-api/users").status_code == 401
            assert admin_client.get("/admin-api/models").status_code == 401
            assert admin_client.get("/admin-api/model-groups").status_code == 401

            register = user_client.post(
                "/api/auth/register",
                json={"email": user_email, "password": "Strong123"},
            )
            assert register.status_code == 201
            user_id = register.json()["user"]["id"]
            assert user_client.get("/admin-api/users").status_code == 404

            login = admin_client.post(
                "/admin-api/auth/login",
                json={"email": admin_email.upper(), "password": admin_password},
            )
            assert login.status_code == 200, login.text
            cookie = login.headers["set-cookie"]
            assert "thinktv_admin_session=" in cookie
            assert "HttpOnly" in cookie
            assert "SameSite=strict" in cookie
            assert admin_client.get("/admin-api/auth/me").status_code == 200

            folder = user_client.post("/api/folders", json={"name": "Admin stats"})
            assert folder.status_code == 201
            project = user_client.post(
                "/api/projects",
                json={"name": "Counted project", "folder_id": folder.json()["id"]},
            )
            assert project.status_code == 201
            asset = user_client.post(
                "/api/assets",
                json={
                    "project_id": project.json()["id"],
                    "kind": "text",
                    "source": "user_input",
                    "text_content": "count me",
                },
            )
            assert asset.status_code == 201

            users = admin_client.get(
                "/admin-api/users",
                params={"search": "MANAGED-", "page": 1, "page_size": 10, "sort_order": "asc"},
            )
            assert users.status_code == 200, users.text
            payload = users.json()
            assert payload["total"] == 1
            assert payload["items"] == [
                {
                    "id": user_id,
                    "email": user_email,
                    "is_active": True,
                    "disabled_at": None,
                    "disabled_by_admin_id": None,
                    "created_at": payload["items"][0]["created_at"],
                    "project_count": 1,
                    "folder_count": 1,
                    "asset_count": 1,
                }
            ]
            assert "password_hash" not in payload["items"][0]
            detail = admin_client.get(f"/admin-api/users/{user_id}")
            assert detail.status_code == 200
            assert "password_hash" not in detail.json()

            group = admin_client.post(
                "/admin-api/model-groups",
                json={
                    "name": f"Test provider {uuid4().hex}",
                    "description": "Credential scope for integration tests",
                    "status": "active",
                    "display_order": 2,
                },
            )
            assert group.status_code == 201, group.text
            created_group_id = group.json()["id"]
            assert group.json()["model_count"] == 0

            active_model = admin_client.post(
                "/admin-api/models",
                json={
                    "group_id": created_group_id,
                    "model_name": "vendor-active-v1",
                    "base_url": "https://models.example/v1",
                    "display_name": "Admin Active Model",
                    "kind": "image",
                    "status": "active",
                    "display_order": 5,
                    "config_schema": {"type": "object"},
                    "default_parameters": {"quality": "high"},
                    "metadata": {"family": "test"},
                },
            )
            assert active_model.status_code == 201, active_model.text
            active_id = active_model.json()["id"]
            created_model_ids.append(active_id)
            assert active_model.json()["metadata"] == {"family": "test"}
            assert "key" not in active_model.json()
            manual_key = admin_client.post(
                "/admin-api/models",
                json={
                    "key": "manual-key-is-not-allowed",
                    "group_id": created_group_id,
                    "model_name": "duplicate",
                    "display_name": "Duplicate",
                    "kind": "image",
                },
            )
            assert manual_key.status_code == 422

            draft_model = admin_client.post(
                "/admin-api/models",
                json={
                    "group_id": created_group_id,
                    "model_name": "vendor-draft-v1",
                    "display_name": "Admin Draft Model",
                    "kind": "video",
                },
            )
            assert draft_model.status_code == 201
            draft_id = draft_model.json()["id"]
            created_model_ids.append(draft_id)

            public_models = user_client.get("/api/models")
            assert public_models.status_code == 200
            assert public_models.json()["items"] == []

            groups = user_client.get("/api/model-groups")
            assert groups.status_code == 200
            configured_group = next(
                item for item in groups.json()["items"] if item["id"] == created_group_id
            )
            assert configured_group["credential_configured"] is False
            assert {item["id"] for item in configured_group["models"]} == {active_id}

            credential = user_client.put(
                f"/api/model-groups/{created_group_id}/credential",
                json={"api_key": "secret-provider-key-1234"},
            )
            assert credential.status_code == 200, credential.text
            assert credential.json()["api_key_last_four"] == "1234"
            assert "api_key" not in credential.json()
            with SessionLocal() as db:
                stored = db.scalar(
                    select(UserModelCredential).where(
                        UserModelCredential.user_id == user_id,
                        UserModelCredential.group_id == created_group_id,
                    )
                )
                assert stored is not None
                assert stored.encrypted_api_key != b"secret-provider-key-1234"
                assert b"secret-provider-key-1234" not in stored.encrypted_api_key

            public_models = user_client.get("/api/models").json()["items"]
            public_by_id = {item["id"]: item for item in public_models}
            assert active_id in public_by_id
            assert draft_id not in public_by_id
            assert "base_url" not in public_by_id[active_id]
            assert "key" not in public_by_id[active_id]

            enable_draft = admin_client.patch(
                f"/admin-api/models/{draft_id}",
                json={"status": "active", "display_order": 6},
            )
            assert enable_draft.status_code == 200
            assert draft_id in {item["id"] for item in user_client.get("/api/models").json()["items"]}

            remove_active = admin_client.delete(f"/admin-api/models/{active_id}")
            assert remove_active.status_code == 204
            assert admin_client.get(f"/admin-api/models/{active_id}").status_code == 404
            assert active_id not in {item["id"] for item in user_client.get("/api/models").json()["items"]}

            delete_credential = user_client.delete(
                f"/api/model-groups/{created_group_id}/credential"
            )
            assert delete_credential.status_code == 204
            assert user_client.get("/api/models").json()["items"] == []

            nonempty_group = admin_client.delete(
                f"/admin-api/model-groups/{created_group_id}"
            )
            assert nonempty_group.status_code == 409
            assert nonempty_group.json() == {
                "detail": "Model group still contains models",
                "code": "GROUP_NOT_EMPTY",
            }
            assert admin_client.delete(f"/admin-api/models/{draft_id}").status_code == 204
            assert admin_client.delete(f"/admin-api/model-groups/{created_group_id}").status_code == 204
            assert admin_client.get("/admin-api/model-groups").status_code == 200

            logout = admin_client.post("/admin-api/auth/logout")
            assert logout.status_code == 204
            assert "thinktv_admin_session=" in logout.headers["set-cookie"]
            assert admin_client.get("/admin-api/auth/me").status_code == 401

        with SessionLocal() as db:
            admin = db.scalar(select(AdminAccount).where(AdminAccount.email == admin_email))
            assert admin is not None
            stored_credential = db.scalar(
                select(UserModelCredential).where(UserModelCredential.group_id == created_group_id)
            )
            assert stored_credential is None
            logs = list(
                db.scalars(
                    select(AdminAuditLog)
                    .where(AdminAuditLog.entity_id.in_(created_model_ids))
                    .order_by(AdminAuditLog.created_at)
                )
            )
            assert [log.action for log in logs] == [
                "model.create",
                "model.create",
                "model.update",
                "model.delete",
                "model.delete",
            ]
    finally:
        settings.admin_bootstrap_email = original_bootstrap_email
        settings.admin_bootstrap_password = original_bootstrap_password
        settings.admin_bootstrap_enabled = original_bootstrap_enabled
        settings.admin_cookie_secure = original_cookie_secure
        with SessionLocal() as db:
            if created_model_ids:
                db.execute(
                    delete(AdminAuditLog).where(AdminAuditLog.entity_id.in_(created_model_ids))
                )
                db.execute(delete(AIModel).where(AIModel.id.in_(created_model_ids)))
            if created_group_id:
                db.execute(delete(AdminAuditLog).where(AdminAuditLog.entity_id == created_group_id))
                db.execute(delete(ModelGroup).where(ModelGroup.id == created_group_id))
            db.execute(delete(User).where(User.email == user_email))
            db.execute(delete(AdminAccount).where(AdminAccount.email == admin_email))
            db.commit()


def test_admin_can_disable_and_reenable_user_with_immediate_session_revocation():
    admin_email = f"status-admin-{uuid4()}@example.com"
    admin_password = "AdminStrongPassword123"
    user_email = f"status-user-{uuid4()}@example.com"
    user_id = None
    original_cookie_secure = settings.admin_cookie_secure
    settings.admin_cookie_secure = False

    try:
        with SessionLocal() as db:
            AdminService(db).bootstrap(admin_email, admin_password)

        with TestClient(admin_app) as admin_client, TestClient(public_app) as user_client:
            assert admin_client.post(
                "/admin-api/auth/login",
                json={"email": admin_email, "password": admin_password},
            ).status_code == 200
            register = user_client.post(
                "/api/auth/register",
                json={"email": user_email, "password": "Strong123"},
            )
            assert register.status_code == 201
            user_id = register.json()["user"]["id"]
            assert user_client.get("/api/auth/me").status_code == 200

            disabled = admin_client.patch(
                f"/admin-api/users/{user_id}/status", json={"is_active": False}
            )
            assert disabled.status_code == 200, disabled.text
            assert disabled.json()["is_active"] is False
            assert disabled.json()["disabled_at"] is not None
            assert disabled.json()["disabled_by_admin_id"] is not None
            disabled_at = disabled.json()["disabled_at"]
            disabled_by_admin_id = disabled.json()["disabled_by_admin_id"]

            repeated_disabled = admin_client.patch(
                f"/admin-api/users/{user_id}/status", json={"is_active": False}
            )
            assert repeated_disabled.status_code == 200
            assert repeated_disabled.json()["is_active"] is False
            assert repeated_disabled.json()["disabled_at"] == disabled_at
            assert repeated_disabled.json()["disabled_by_admin_id"] == disabled_by_admin_id

            revoked = user_client.get("/api/auth/me")
            assert revoked.status_code == 401
            assert revoked.json() == {
                "detail": "Account disabled",
                "code": "ACCOUNT_DISABLED",
            }
            assert "access_token=" in revoked.headers["set-cookie"]
            assert "Max-Age=0" in revoked.headers["set-cookie"]

            disabled_login = user_client.post(
                "/api/auth/login",
                json={"email": user_email, "password": "Strong123"},
            )
            assert disabled_login.status_code == 401
            assert disabled_login.json() == {"detail": "Invalid email or password"}

            enabled = admin_client.patch(
                f"/admin-api/users/{user_id}/status", json={"is_active": True}
            )
            assert enabled.status_code == 200
            assert enabled.json()["is_active"] is True
            assert enabled.json()["disabled_at"] is None
            assert enabled.json()["disabled_by_admin_id"] is None

            repeated_enabled = admin_client.patch(
                f"/admin-api/users/{user_id}/status", json={"is_active": True}
            )
            assert repeated_enabled.status_code == 200
            assert repeated_enabled.json()["is_active"] is True
            assert repeated_enabled.json()["disabled_at"] is None
            assert repeated_enabled.json()["disabled_by_admin_id"] is None
            assert user_client.post(
                "/api/auth/login",
                json={"email": user_email, "password": "Strong123"},
            ).status_code == 200

        with SessionLocal() as db:
            logs = list(
                db.scalars(
                    select(AdminAuditLog)
                    .where(
                        AdminAuditLog.entity_type == "user",
                        AdminAuditLog.entity_id == user_id,
                    )
                    .order_by(AdminAuditLog.created_at)
                )
            )
            assert [log.action for log in logs] == [
                "user.status.update",
                "user.status.update",
            ]
            assert [log.changes["before"]["is_active"] for log in logs] == [True, False]
            assert [log.changes["after"]["is_active"] for log in logs] == [False, True]
            assert "password" not in str([log.changes for log in logs]).lower()
    finally:
        settings.admin_cookie_secure = original_cookie_secure
        with SessionLocal() as db:
            if user_id is not None:
                db.execute(delete(AdminAuditLog).where(AdminAuditLog.entity_id == user_id))
            db.execute(delete(User).where(User.email == user_email))
            db.execute(delete(AdminAccount).where(AdminAccount.email == admin_email))
            db.commit()


def test_model_delete_conflict_preserves_history_and_empty_group_delete_cascades_credentials():
    admin_email = f"delete-admin-{uuid4()}@example.com"
    admin_password = "AdminStrongPassword123"
    user_email = f"delete-user-{uuid4()}@example.com"
    user_id = None
    project_id = None
    group_id = None
    model_id = None
    task_id = uuid4()
    original_cookie_secure = settings.admin_cookie_secure
    settings.admin_cookie_secure = False

    try:
        with SessionLocal() as db:
            AdminService(db).bootstrap(admin_email, admin_password)
        with TestClient(admin_app) as admin_client, TestClient(public_app) as user_client:
            assert admin_client.post(
                "/admin-api/auth/login",
                json={"email": admin_email, "password": admin_password},
            ).status_code == 200
            register = user_client.post(
                "/api/auth/register",
                json={"email": user_email, "password": "Strong123"},
            )
            user_id = register.json()["user"]["id"]
            project = user_client.post("/api/projects", json={"name": "History owner"})
            assert project.status_code == 201
            project_id = project.json()["id"]

            group = admin_client.post(
                "/admin-api/model-groups",
                json={"name": f"Delete provider {uuid4().hex}", "status": "active"},
            )
            assert group.status_code == 201
            group_id = group.json()["id"]
            model = admin_client.post(
                "/admin-api/models",
                json={
                    "group_id": group_id,
                    "model_name": "deletable-image-v1",
                    "base_url": "https://models.example/v1/images/generations",
                    "display_name": "Deletable image",
                    "kind": "image",
                    "status": "active",
                    "metadata": {"nested": {"api_key": "must-never-be-audited"}},
                },
            )
            assert model.status_code == 201, model.text
            model_id = model.json()["id"]

            credential = user_client.put(
                f"/api/model-groups/{group_id}/credential",
                json={"api_key": "secret-provider-key-1234"},
            )
            assert credential.status_code == 200

            with SessionLocal() as db:
                db.add(
                    GenerationTask(
                        id=task_id,
                        owner_id=user_id,
                        project_id=project_id,
                        node_id=uuid4(),
                        input_revision=0,
                        kind="image",
                        model=model_id,
                        prompt="historical prompt",
                        parameters={},
                        idempotency_key=f"delete-test-{uuid4()}",
                        request_hash="a" * 64,
                        status="queued",
                        result={},
                    )
                )
                db.commit()

            in_use = admin_client.delete(f"/admin-api/models/{model_id}")
            assert in_use.status_code == 409
            assert in_use.json()["code"] == "MODEL_IN_USE"

            with SessionLocal() as db:
                task = db.get(GenerationTask, task_id)
                task.status = "succeeded"
                db.commit()

            assert admin_client.delete(f"/admin-api/models/{model_id}").status_code == 204
            assert admin_client.delete(f"/admin-api/models/{model_id}").status_code == 404
            assert admin_client.delete(f"/admin-api/model-groups/{group_id}").status_code == 204
            assert admin_client.delete(f"/admin-api/model-groups/{group_id}").status_code == 404

        with SessionLocal() as db:
            historical_task = db.get(GenerationTask, task_id)
            assert historical_task is not None
            assert historical_task.model == model_id
            assert db.scalar(
                select(UserModelCredential).where(UserModelCredential.group_id == group_id)
            ) is None
            model_delete_log = db.scalar(
                select(AdminAuditLog).where(
                    AdminAuditLog.entity_id == model_id,
                    AdminAuditLog.action == "model.delete",
                )
            )
            assert model_delete_log is not None
            assert "must-never-be-audited" not in str(model_delete_log.changes)
            assert "[REDACTED]" in str(model_delete_log.changes)
    finally:
        settings.admin_cookie_secure = original_cookie_secure
        with SessionLocal() as db:
            db.execute(delete(AdminAuditLog).where(AdminAuditLog.entity_id.in_([value for value in (group_id, model_id) if value])))
            db.execute(delete(GenerationTask).where(GenerationTask.id == task_id))
            if model_id is not None:
                db.execute(delete(AIModel).where(AIModel.id == model_id))
            if group_id is not None:
                db.execute(delete(ModelGroup).where(ModelGroup.id == group_id))
            db.execute(delete(User).where(User.email == user_email))
            db.execute(delete(AdminAccount).where(AdminAccount.email == admin_email))
            db.commit()


def test_bootstrap_requires_complete_strong_credentials():
    from app.services.admin import AdminService, normalize_admin_email

    with SessionLocal() as db:
        service = AdminService(db)
        assert normalize_admin_email("Admin@ThinkTV.local") == "admin@thinktv.local"
        assert service.bootstrap("", "") is None
        try:
            service.bootstrap("admin@example.com", "")
        except RuntimeError as error:
            assert "configured together" in str(error)
        else:
            raise AssertionError("Partial bootstrap configuration must fail")

        try:
            service.bootstrap("admin@example.com", "weak")
        except RuntimeError as error:
            assert "at least 16" in str(error)
        else:
            raise AssertionError("Weak bootstrap password must fail")


def test_user_access_token_cannot_authenticate_as_admin():
    from app.core.security import create_access_token

    user_token = create_access_token(uuid4())
    with TestClient(admin_app) as admin_client:
        admin_client.cookies.set("thinktv_admin_session", user_token)
        response = admin_client.get("/admin-api/auth/me")

    assert response.status_code == 401
