import io
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import delete, select

from app.config import settings
from app.database import SessionLocal
from app.main import app
from app.models import Asset, Folder, Project, StorageDeletionJob, User
from app.services.asset_storage import generated_asset_path
from app.services.storage_deletion_jobs import process_claimed_storage_deletion
from app.services.trash_purge import purge_expired_trash


def _image_bytes() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (5, 4), color="purple").save(output, format="PNG")
    return output.getvalue()


def _register(client: TestClient, prefix: str) -> tuple[str, UUID]:
    email = f"{prefix}-{uuid4()}@example.com"
    response = client.post(
        "/api/auth/register", json={"email": email, "password": "Strong123"}
    )
    assert response.status_code == 201, response.text
    return email, UUID(response.json()["user"]["id"])


def _cleanup_user(email: str, owner_id: UUID) -> None:
    with SessionLocal.begin() as db:
        db.execute(delete(StorageDeletionJob).where(StorageDeletionJob.owner_id == owner_id))
        user = db.scalar(select(User).where(User.email == email))
        if user is not None:
            db.delete(user)


def _upload(client: TestClient, project_id: str) -> dict:
    response = client.post(
        f"/api/projects/{project_id}/assets/images",
        headers={"Idempotency-Key": str(uuid4())},
        files={"file": ("asset.png", _image_bytes(), "image/png")},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _run_job(job_id: UUID) -> None:
    lease_token = uuid4()
    with SessionLocal.begin() as db:
        job = db.scalar(
            select(StorageDeletionJob)
            .where(StorageDeletionJob.id == job_id)
            .with_for_update()
        )
        assert job is not None
        job.status = "running"
        job.attempt_count = 1
        job.lease_token = lease_token
        job.lease_owner = "test-worker"
        job.lease_expires_at = datetime.now(timezone.utc) + timedelta(minutes=1)
    process_claimed_storage_deletion(job_id, lease_token)


def test_deleting_unreferenced_image_queues_and_removes_storage_object(
    monkeypatch, tmp_path: Path
):
    monkeypatch.setattr(settings, "asset_storage_backend", "local")
    monkeypatch.setattr(settings, "generated_storage_root", str(tmp_path))
    monkeypatch.setattr(settings, "app_environment", "test")
    client = TestClient(app)
    email, owner_id = _register(client, "asset-delete")
    try:
        project_id = client.post("/api/projects", json={"name": "Asset delete"}).json()["id"]
        uploaded = _upload(client, project_id)
        with SessionLocal() as db:
            asset = db.scalar(select(Asset).where(Asset.id == UUID(uploaded["id"])))
            assert asset is not None and asset.storage_key is not None
            storage_key = asset.storage_key
        stored_path = generated_asset_path(storage_key)
        assert stored_path.exists()

        response = client.delete(f"/api/assets/{uploaded['id']}")
        assert response.status_code == 204, response.text
        with SessionLocal() as db:
            asset = db.scalar(select(Asset).where(Asset.id == UUID(uploaded["id"])))
            job = db.scalar(
                select(StorageDeletionJob).where(
                    StorageDeletionJob.asset_id == UUID(uploaded["id"])
                )
            )
            assert asset is not None
            assert asset.deleted_at is not None
            assert asset.status == "deleting"
            assert job is not None
            assert job.reason == "asset_deleted"
            job_id = job.id

        _run_job(job_id)
        assert not stored_path.exists()
        with SessionLocal() as db:
            job = db.scalar(select(StorageDeletionJob).where(StorageDeletionJob.id == job_id))
            assert job is not None
            assert job.status == "succeeded"
            assert job.completed_at is not None
    finally:
        _cleanup_user(email, owner_id)


def test_expired_folder_trash_purges_project_and_queues_each_object(
    monkeypatch, tmp_path: Path
):
    monkeypatch.setattr(settings, "asset_storage_backend", "local")
    monkeypatch.setattr(settings, "generated_storage_root", str(tmp_path))
    monkeypatch.setattr(settings, "app_environment", "test")
    monkeypatch.setattr(settings, "trash_retention_days", 30)
    client = TestClient(app)
    email, owner_id = _register(client, "folder-purge")
    try:
        folder = client.post("/api/folders", json={"name": "Disposable"}).json()
        project = client.post(
            "/api/projects",
            json={"name": "Disposable project", "folder_id": folder["id"]},
        ).json()
        uploaded = _upload(client, project["id"])
        with SessionLocal() as db:
            asset = db.scalar(select(Asset).where(Asset.id == UUID(uploaded["id"])))
            assert asset is not None and asset.storage_key is not None
            storage_key = asset.storage_key
        stored_path = generated_asset_path(storage_key)
        assert stored_path.exists()

        response = client.delete(f"/api/folders/{folder['id']}")
        assert response.status_code == 204, response.text
        purged_projects, purged_folders = purge_expired_trash(
            now=datetime.now(timezone.utc) + timedelta(days=31)
        )
        assert (purged_projects, purged_folders) == (1, 1)

        with SessionLocal() as db:
            assert db.get(Project, UUID(project["id"])) is None
            assert db.get(Folder, UUID(folder["id"])) is None
            job = db.scalar(
                select(StorageDeletionJob).where(
                    StorageDeletionJob.owner_id == owner_id,
                    StorageDeletionJob.storage_key == storage_key,
                )
            )
            assert job is not None
            assert job.asset_id is None
            assert job.reason == "project_purged"
            job_id = job.id

        # Database deletion and remote deletion are deliberately decoupled. The
        # durable job removes the object after the purge transaction commits.
        assert stored_path.exists()
        _run_job(job_id)
        assert not stored_path.exists()
    finally:
        _cleanup_user(email, owner_id)
