import io
from pathlib import Path
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import select

from app.config import settings
from app.database import SessionLocal
from app.main import app
from app.models import ProjectVersion, User


def _image_bytes(color: str) -> bytes:
    return _image_bytes_with_size(color, (8, 6))


def _image_bytes_with_size(color: str, size: tuple[int, int]) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", size, color=color).save(output, format="PNG")
    return output.getvalue()


def _register(client: TestClient, prefix: str) -> str:
    email = f"{prefix}-{uuid4()}@example.com"
    response = client.post(
        "/api/auth/register", json={"email": email, "password": "Strong123"}
    )
    assert response.status_code == 201, response.text
    return email


def _delete_user(email: str) -> None:
    with SessionLocal.begin() as db:
        user = db.scalar(select(User).where(User.email == email))
        if user is not None:
            db.delete(user)


def _workflow(node_id: UUID, asset_id: str, *, node_type: str = "image") -> dict:
    return {
        "schema_version": 1,
        "viewport": {"x": 0, "y": 0, "zoom": 1},
        "nodes": [
            {
                "id": str(node_id),
                "type": node_type,
                "position": {"x": 0, "y": 0},
                "size": {"width": 320, "height": 240},
                "data": {
                    "title": "Editable image" if node_type == "image" else "Prompt",
                    "asset_id": asset_id if node_type == "image" else None,
                },
            }
        ],
        "edges": [],
    }


def _upload_source(client: TestClient, project_id: str, content: bytes) -> dict:
    response = client.post(
        f"/api/projects/{project_id}/assets/images",
        headers={"Idempotency-Key": str(uuid4())},
        files={"file": ("source.png", content, "image/png")},
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_image_edit_replaces_node_preserves_history_and_replays(
    monkeypatch, tmp_path: Path
):
    monkeypatch.setattr(settings, "asset_storage_backend", "local")
    monkeypatch.setattr(settings, "generated_storage_root", str(tmp_path))
    client = TestClient(app)
    email = _register(client, "image-edit")
    try:
        project_response = client.post("/api/projects", json={"name": "Image edit"})
        assert project_response.status_code == 201, project_response.text
        project_id = project_response.json()["id"]
        base_revision = project_response.json()["current_revision"]
        source_content = _image_bytes("red")
        edited_content = _image_bytes("blue")
        source = _upload_source(client, project_id, source_content)
        cover_response = client.patch(
            f"/api/projects/{project_id}",
            json={"cover_asset_id": source["id"]},
        )
        assert cover_response.status_code == 200, cover_response.text
        node_id = uuid4()

        workflow_response = client.put(
            f"/api/projects/{project_id}/workflow",
            json={
                "base_revision": base_revision,
                "workflow": _workflow(node_id, source["id"]),
                "reason": "manual",
            },
        )
        assert workflow_response.status_code == 200, workflow_response.text
        edit_base_revision = workflow_response.json()["revision"]
        idempotency_key = str(uuid4())

        edit = client.post(
            f"/api/projects/{project_id}/nodes/{node_id}/image-edit",
            headers={"Idempotency-Key": idempotency_key},
            data={
                "base_revision": str(edit_base_revision),
                "expected_asset_id": source["id"],
            },
            files={"file": ("edited.png", edited_content, "image/png")},
        )
        assert edit.status_code == 201, edit.text
        body = edit.json()
        assert body["replayed"] is False
        assert body["revision"] == edit_base_revision + 1
        assert body["asset"]["id"] != source["id"]
        assert body["asset"]["metadata"]["edited_from_asset_id"] == source["id"]
        assert body["workflow"]["nodes"][0]["data"]["asset_id"] == body["asset"]["id"]
        assert client.get(f"/api/assets/{body['asset']['id']}/content").content == edited_content
        updated_project = client.get(f"/api/projects/{project_id}")
        assert updated_project.status_code == 200, updated_project.text
        assert updated_project.json()["cover_asset_id"] == body["asset"]["id"]

        replay = client.post(
            f"/api/projects/{project_id}/nodes/{node_id}/image-edit",
            headers={"Idempotency-Key": idempotency_key},
            data={
                "base_revision": str(edit_base_revision),
                "expected_asset_id": source["id"],
            },
            files={"file": ("renamed.png", edited_content, "image/png")},
        )
        assert replay.status_code == 201, replay.text
        assert replay.headers["Idempotency-Replayed"] == "true"
        assert replay.json()["replayed"] is True
        assert replay.json()["asset"]["id"] == body["asset"]["id"]

        conflict = client.post(
            f"/api/projects/{project_id}/nodes/{node_id}/image-edit",
            headers={"Idempotency-Key": idempotency_key},
            data={
                "base_revision": str(edit_base_revision),
                "expected_asset_id": source["id"],
            },
            files={"file": ("different.png", _image_bytes("green"), "image/png")},
        )
        assert conflict.status_code == 409
        assert conflict.json()["detail"]["code"] == "IDEMPOTENCY_KEY_CONFLICT"

        with SessionLocal() as db:
            versions = list(
                db.scalars(
                    select(ProjectVersion)
                    .where(ProjectVersion.project_id == UUID(project_id))
                    .order_by(ProjectVersion.version_no)
                )
            )
        assert len(versions) == 2
        assert versions[0].snapshot["nodes"][0]["data"]["asset_id"] == source["id"]
        assert (
            versions[1].snapshot["nodes"][0]["data"]["asset_id"]
            == body["asset"]["id"]
        )
    finally:
        _delete_user(email)


def test_image_edit_rejects_stale_asset_non_image_node_and_invalid_file(
    monkeypatch, tmp_path: Path
):
    monkeypatch.setattr(settings, "asset_storage_backend", "local")
    monkeypatch.setattr(settings, "generated_storage_root", str(tmp_path))
    client = TestClient(app)
    email = _register(client, "image-edit-errors")
    try:
        project = client.post("/api/projects", json={"name": "Image edit errors"}).json()
        source = _upload_source(client, project["id"], _image_bytes("red"))
        node_id = uuid4()
        saved = client.put(
            f"/api/projects/{project['id']}/workflow",
            json={
                "base_revision": project["current_revision"],
                "workflow": _workflow(node_id, source["id"]),
                "reason": "manual",
            },
        ).json()
        endpoint = f"/api/projects/{project['id']}/nodes/{node_id}/image-edit"

        stale_revision = client.post(
            endpoint,
            headers={"Idempotency-Key": str(uuid4())},
            data={"base_revision": "0", "expected_asset_id": source["id"]},
            files={"file": ("edited.png", _image_bytes("blue"), "image/png")},
        )
        assert stale_revision.status_code == 409
        assert stale_revision.json()["detail"]["code"] == "REVISION_CONFLICT"

        stale_asset = client.post(
            endpoint,
            headers={"Idempotency-Key": str(uuid4())},
            data={
                "base_revision": str(saved["revision"]),
                "expected_asset_id": str(uuid4()),
            },
            files={"file": ("edited.png", _image_bytes("blue"), "image/png")},
        )
        assert stale_asset.status_code == 409

        invalid = client.post(
            endpoint,
            headers={"Idempotency-Key": str(uuid4())},
            data={
                "base_revision": str(saved["revision"]),
                "expected_asset_id": source["id"],
            },
            files={"file": ("edited.png", b"not-an-image", "image/png")},
        )
        assert invalid.status_code == 422
        assert invalid.json()["detail"]["code"] == "INVALID_IMAGE"

        wrong_dimensions = client.post(
            endpoint,
            headers={"Idempotency-Key": str(uuid4())},
            data={
                "base_revision": str(saved["revision"]),
                "expected_asset_id": source["id"],
            },
            files={
                "file": (
                    "edited.png",
                    _image_bytes_with_size("blue", (7, 6)),
                    "image/png",
                )
            },
        )
        assert wrong_dimensions.status_code == 422
        assert (
            wrong_dimensions.json()["detail"]
            == "Edited image dimensions must exactly match the source image"
        )

        prompt_project = client.post(
            "/api/projects",
            json={
                "name": "Prompt only",
                "workflow": _workflow(uuid4(), source["id"], node_type="prompt"),
            },
        )
        assert prompt_project.status_code == 201, prompt_project.text
        prompt_node_id = prompt_project.json()["workflow"]["nodes"][0]["id"]
        wrong_type = client.post(
            f"/api/projects/{prompt_project.json()['id']}/nodes/{prompt_node_id}/image-edit",
            headers={"Idempotency-Key": str(uuid4())},
            data={
                "base_revision": str(prompt_project.json()["current_revision"]),
                "expected_asset_id": source["id"],
            },
            files={"file": ("edited.png", _image_bytes("blue"), "image/png")},
        )
        assert wrong_type.status_code == 422
    finally:
        _delete_user(email)
