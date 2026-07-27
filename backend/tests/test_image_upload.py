import io
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import select

from app.config import settings
from app.database import SessionLocal
from app.main import app
from app.models import User
from app.services import project_domain
from app.services.asset_storage import AssetStorageError


def _image_bytes(image_format: str, *, color: str = "red") -> bytes:
    output = io.BytesIO()
    image = Image.new("RGB", (3, 2), color=color)
    image.save(output, format=image_format)
    return output.getvalue()


def _register(client: TestClient, prefix: str) -> tuple[str, str]:
    email = f"{prefix}-{uuid4()}@example.com"
    response = client.post(
        "/api/auth/register", json={"email": email, "password": "Strong123"}
    )
    assert response.status_code == 201, response.text
    return email, response.json()["user"]["id"]


def _delete_user(email: str) -> None:
    with SessionLocal.begin() as db:
        user = db.scalar(select(User).where(User.email == email))
        if user is not None:
            db.delete(user)


@pytest.mark.parametrize(
    ("image_format", "filename", "submitted_type", "expected_type"),
    [
        ("JPEG", "photo.jpg", "application/octet-stream", "image/jpeg"),
        ("PNG", "photo.png", "text/plain", "image/png"),
        ("WEBP", "photo.webp", "image/png", "image/webp"),
        ("GIF", "photo.gif", "image/jpeg", "image/gif"),
    ],
)
def test_project_image_upload_sniffs_supported_formats_and_persists(
    image_format, filename, submitted_type, expected_type, monkeypatch, tmp_path
):
    monkeypatch.setattr(settings, "asset_storage_backend", "local")
    monkeypatch.setattr(settings, "generated_storage_root", str(tmp_path))
    client = TestClient(app)
    email, _ = _register(client, "upload-formats")
    try:
        project = client.post("/api/projects", json={"name": "Upload formats"})
        assert project.status_code == 201, project.text
        project_id = project.json()["id"]
        content = _image_bytes(image_format)

        response = client.post(
            f"/api/projects/{project_id}/assets/images",
            headers={"Idempotency-Key": str(uuid4())},
            files={"file": (filename, content, submitted_type)},
        )

        assert response.status_code == 201, response.text
        asset = response.json()
        assert asset["project_id"] == project_id
        assert asset["kind"] == "image"
        assert asset["source"] == "upload"
        assert asset["status"] == "ready"
        assert asset["mime_type"] == expected_type
        assert asset["byte_size"] == len(content)
        assert (asset["width"], asset["height"]) == (3, 2)
        downloaded = client.get(f"/api/assets/{asset['id']}/content")
        assert downloaded.status_code == 200
        assert downloaded.content == content
    finally:
        _delete_user(email)


def test_project_image_upload_requires_auth_project_ownership_and_idempotency_key(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(settings, "asset_storage_backend", "local")
    monkeypatch.setattr(settings, "generated_storage_root", str(tmp_path))
    owner = TestClient(app)
    stranger = TestClient(app)
    owner_email, _ = _register(owner, "upload-owner")
    stranger_email, _ = _register(stranger, "upload-stranger")
    try:
        project_id = owner.post("/api/projects", json={"name": "Private"}).json()["id"]
        content = _image_bytes("PNG")
        endpoint = f"/api/projects/{project_id}/assets/images"

        missing_key = owner.post(
            endpoint, files={"file": ("image.png", content, "image/png")}
        )
        assert missing_key.status_code == 400
        assert missing_key.json()["detail"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"

        unauthenticated = TestClient(app).post(
            endpoint,
            headers={"Idempotency-Key": str(uuid4())},
            files={"file": ("image.png", content, "image/png")},
        )
        assert unauthenticated.status_code == 401

        forbidden_as_not_found = stranger.post(
            endpoint,
            headers={"Idempotency-Key": str(uuid4())},
            files={"file": ("image.png", content, "image/png")},
        )
        assert forbidden_as_not_found.status_code == 404
    finally:
        _delete_user(owner_email)
        _delete_user(stranger_email)


def test_project_image_upload_enforces_exact_size_limit_and_rejects_invalid_files(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(settings, "asset_storage_backend", "local")
    monkeypatch.setattr(settings, "generated_storage_root", str(tmp_path))
    client = TestClient(app)
    email, _ = _register(client, "upload-validation")
    try:
        project_id = client.post("/api/projects", json={"name": "Validation"}).json()["id"]
        endpoint = f"/api/projects/{project_id}/assets/images"
        png = _image_bytes("PNG")
        monkeypatch.setattr(settings, "user_image_upload_max_bytes", len(png))

        exact = client.post(
            endpoint,
            headers={"Idempotency-Key": str(uuid4())},
            files={"file": ("exact.png", png, "image/png")},
        )
        assert exact.status_code == 201, exact.text

        oversized = client.post(
            endpoint,
            headers={"Idempotency-Key": str(uuid4())},
            files={"file": ("large.png", png + b"x", "image/png")},
        )
        assert oversized.status_code == 413
        assert oversized.json()["detail"]["code"] == "FILE_TOO_LARGE"

        monkeypatch.setattr(settings, "user_image_upload_max_bytes", 1024 * 1024)
        corrupt = client.post(
            endpoint,
            headers={"Idempotency-Key": str(uuid4())},
            files={"file": ("fake.png", b"not an image", "image/png")},
        )
        assert corrupt.status_code == 422
        assert corrupt.json()["detail"]["code"] == "INVALID_IMAGE"

        svg = client.post(
            endpoint,
            headers={"Idempotency-Key": str(uuid4())},
            files={
                "file": (
                    "vector.svg",
                    b'<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1"/>',
                    "image/svg+xml",
                )
            },
        )
        assert svg.status_code in {415, 422}
        assert svg.json()["detail"]["code"] in {"UNSUPPORTED_IMAGE_TYPE", "INVALID_IMAGE"}
    finally:
        _delete_user(email)


def test_project_image_upload_is_idempotent_and_detects_content_conflicts(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(settings, "asset_storage_backend", "local")
    monkeypatch.setattr(settings, "generated_storage_root", str(tmp_path))
    client = TestClient(app)
    email, _ = _register(client, "upload-idempotency")
    try:
        project_id = client.post("/api/projects", json={"name": "Idempotency"}).json()["id"]
        endpoint = f"/api/projects/{project_id}/assets/images"
        key = str(uuid4())
        first_content = _image_bytes("PNG", color="red")
        second_content = _image_bytes("PNG", color="blue")

        first = client.post(
            endpoint,
            headers={"Idempotency-Key": key},
            files={"file": ("first.png", first_content, "image/png")},
        )
        replay = client.post(
            endpoint,
            headers={"Idempotency-Key": key},
            files={"file": ("renamed.png", first_content, "image/png")},
        )
        conflict = client.post(
            endpoint,
            headers={"Idempotency-Key": key},
            files={"file": ("different.png", second_content, "image/png")},
        )

        assert first.status_code == 201, first.text
        assert replay.status_code == 201, replay.text
        assert replay.headers["Idempotency-Replayed"] == "true"
        assert replay.json()["id"] == first.json()["id"]
        assert conflict.status_code == 409
        assert conflict.json()["detail"]["code"] == "IDEMPOTENCY_KEY_CONFLICT"
    finally:
        _delete_user(email)


def test_project_image_upload_storage_failure_returns_503_without_ready_asset(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(settings, "asset_storage_backend", "local")
    monkeypatch.setattr(settings, "generated_storage_root", str(tmp_path))

    class FailingStorage:
        def object_key(self, key):
            return key

        def put_file(self, *_args, **_kwargs):
            raise AssetStorageError("unavailable")

    monkeypatch.setattr(project_domain, "generation_asset_storage", lambda: FailingStorage())
    client = TestClient(app)
    email, _ = _register(client, "upload-storage-failure")
    try:
        project_id = client.post("/api/projects", json={"name": "Storage failure"}).json()["id"]
        before = client.get(f"/api/projects/{project_id}/assets").json()
        response = client.post(
            f"/api/projects/{project_id}/assets/images",
            headers={"Idempotency-Key": str(uuid4())},
            files={"file": ("image.png", _image_bytes("PNG"), "image/png")},
        )
        after = client.get(f"/api/projects/{project_id}/assets").json()

        assert response.status_code == 503
        assert response.json()["detail"]["code"] == "UPLOAD_FAILED"
        assert after == before
    finally:
        _delete_user(email)
