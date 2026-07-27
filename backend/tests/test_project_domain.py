from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.database import SessionLocal
from app.main import app
from app.core.model_credentials import encrypt_api_key
from app.models import AIModel, ModelGroup, User, UserModelCredential


def register(client: TestClient, prefix: str) -> str:
    email = f"{prefix}-{uuid4()}@example.com"
    response = client.post(
        "/api/auth/register", json={"email": email, "password": "Strong123"}
    )
    assert response.status_code == 201
    return email


def workflow(node_id: str, source_node_id: str, *, asset_id: str | None = None) -> dict:
    return {
        "schema_version": 1,
        "viewport": {"x": 12, "y": -4, "zoom": 1.25},
        "nodes": [
            {
                "id": source_node_id,
                "type": "prompt",
                "position": {"x": -360, "y": 20},
                "size": {"width": 320, "height": 180},
                "data": {"title": "Prompt input", "prompt": "rainy future city"},
            },
            {
                "id": node_id,
                "type": "image",
                "position": {"x": 10, "y": 20},
                "size": {"width": 320, "height": 180},
                "data": {
                    "title": "Prompt",
                    "prompt": "rainy future city",
                    "asset_id": asset_id,
                },
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
    }


def test_project_domain_contract_and_tenant_isolation():
    owner = TestClient(app)
    other = TestClient(app)
    owner_email = register(owner, "owner")
    other_email = register(other, "other")
    node_id = str(uuid4())
    source_node_id = str(uuid4())
    test_group_id = uuid4()
    test_model_id = uuid4()

    try:
        with SessionLocal() as db:
            owner_user = db.scalar(select(User).where(User.email == owner_email))
            db.add(ModelGroup(id=test_group_id, name=f"Test group {uuid4()}", status="active"))
            db.flush()
            db.add(
                AIModel(
                    id=test_model_id,
                    group_id=test_group_id,
                    key=str(uuid4()),
                    model_name="provider-image-model",
                    base_url="https://models.example/v1",
                    display_name="Project test model",
                    kind="image",
                    status="active",
                )
            )
            db.add(
                UserModelCredential(
                    id=uuid4(),
                    user_id=owner_user.id,
                    group_id=test_group_id,
                    encrypted_api_key=encrypt_api_key(db, "project-test-key"),
                    api_key_last_four="-key",
                )
            )
            db.commit()
        folder_response = owner.post("/api/folders", json={"name": "Campaigns"})
        assert folder_response.status_code == 201
        folder_id = folder_response.json()["id"]
        assert owner.post("/api/folders", json={"name": " campaigns "}).status_code == 409

        create_response = owner.post(
            "/api/projects",
            json={
                "name": "Launch",
                "folder_id": folder_id,
                "project_type": "mixed",
                "workflow": workflow(node_id, source_node_id),
            },
        )
        assert create_response.status_code == 201, create_response.text
        project = create_response.json()
        project_id = project["id"]
        assert project["current_revision"] == 1
        assert other.get(f"/api/projects/{project_id}").status_code == 404
        assert other.get(f"/api/assets?project_id={project_id}").status_code == 404
        assert other.post(
            "/api/projects", json={"name": "Nope", "folder_id": folder_id}
        ).status_code == 404

        asset_response = owner.post(
            "/api/assets",
            json={
                "project_id": project_id,
                "kind": "text",
                "source": "user_input",
                "text_content": "rainy future city",
                "metadata": {"tags": ["city"]},
            },
        )
        assert asset_response.status_code == 201, asset_response.text
        asset = asset_response.json()
        assert asset["mime_type"] == "text/plain"
        assert asset["byte_size"] == len("rainy future city".encode())
        asset_id = asset["id"]
        assert other.post(
            "/api/assets",
            json={
                "project_id": project_id,
                "kind": "text",
                "source": "user_input",
                "text_content": "stolen",
            },
        ).status_code == 404

        save_response = owner.put(
            f"/api/projects/{project_id}/workflow",
            json={
                "base_revision": 1,
                "workflow": workflow(node_id, source_node_id, asset_id=asset_id),
                "reason": "manual",
            },
        )
        assert save_response.status_code == 200, save_response.text
        assert save_response.json()["revision"] == 2

        conflict = owner.put(
            f"/api/projects/{project_id}/workflow",
            json={"base_revision": 1, "workflow": workflow(node_id, source_node_id)},
        )
        assert conflict.status_code == 409
        assert conflict.json()["current_revision"] == 2

        invalid_graph = workflow(node_id, source_node_id)
        invalid_graph["edges"] = [
            {
                "id": str(uuid4()),
                "source_node_id": node_id,
                "target_node_id": str(uuid4()),
                "data": {},
            }
        ]
        assert owner.put(
            f"/api/projects/{project_id}/workflow",
            json={"base_revision": 2, "workflow": invalid_graph},
        ).status_code == 422

        generation = owner.post(
            "/api/generations",
            headers={"Idempotency-Key": str(uuid4())},
            json={
                "project_id": project_id,
                "node_id": node_id,
                "input_node_ids": [source_node_id],
                "input_revision": 2,
                "kind": "image",
                "model": str(test_model_id),
                "prompt": "rainy future city",
                "parameters": {},
            },
        )
        assert generation.status_code == 202, generation.text
        task_id = generation.json()["id"]
        assert other.get(f"/api/generations/{task_id}").status_code == 404

        versions = owner.get(f"/api/projects/{project_id}/versions").json()["items"]
        assert [item["reason"] for item in versions] == ["manual", "initial"]
        manual_version_id = versions[0]["id"]

        assert owner.delete(f"/api/assets/{asset_id}").status_code == 422
        remove_reference = owner.put(
            f"/api/projects/{project_id}/workflow",
            json={
                "base_revision": 2,
                "workflow": workflow(node_id, source_node_id),
                "reason": "manual",
            },
        )
        assert remove_reference.status_code == 200
        assert owner.delete(f"/api/assets/{asset_id}").status_code == 204
        assert owner.get(f"/api/assets/{asset_id}").status_code == 404
        restored_version = owner.post(
            f"/api/projects/{project_id}/versions/{manual_version_id}/restore"
        )
        assert restored_version.status_code == 200, restored_version.text
        assert restored_version.json()["revision"] == 4
        assert owner.get(f"/api/assets/{asset_id}").status_code == 200

        assert owner.delete(f"/api/folders/{folder_id}").status_code == 204
        assert owner.get(f"/api/projects/{project_id}").status_code == 404
        assert owner.post(f"/api/folders/{folder_id}/restore").status_code == 200
        assert owner.get(f"/api/projects/{project_id}").status_code == 200

        assert owner.get("/api/projects?folder_id=bad-id").status_code == 422
        root_list = owner.get("/api/projects?folder_id=root")
        assert root_list.status_code == 200
        assert root_list.json()["items"] == []
    finally:
        with SessionLocal() as db:
            users = list(db.scalars(select(User).where(User.email.in_([owner_email, other_email]))))
            for user in users:
                db.delete(user)
            db.execute(delete(AIModel).where(AIModel.id == test_model_id))
            db.execute(delete(ModelGroup).where(ModelGroup.id == test_group_id))
            db.commit()
