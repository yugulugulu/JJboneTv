from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import settings
from app.database import SessionLocal
from app.main import app
from app.models import User
from app.services import asset_storage
from app.services.asset_storage import (
    AssetStorageError,
    AssetStorageConfigurationError,
    LocalAssetStorage,
    OssAssetStorage,
    storage_for_asset,
    storage_metadata,
)


def _configure_oss(monkeypatch, *, bucket: str = "thinktv-development") -> None:
    monkeypatch.setattr(settings, "app_environment", "development")
    monkeypatch.setattr(settings, "asset_storage_backend", "oss")
    monkeypatch.setattr(settings, "oss_endpoint", "https://oss-cn-test.aliyuncs.com")
    monkeypatch.setattr(
        settings,
        "oss_internal_endpoint",
        "https://oss-cn-test-internal.aliyuncs.com",
    )
    monkeypatch.setattr(settings, "oss_bucket_name", bucket)
    monkeypatch.setattr(settings, "oss_access_key_id", "test-access-key-id")
    monkeypatch.setattr(settings, "oss_access_key_secret", "test-access-key-secret")
    monkeypatch.setattr(settings, "oss_security_token", "")
    monkeypatch.setattr(settings, "oss_prefix", "thinktv/development")
    monkeypatch.setattr(settings, "oss_cname", False)
    monkeypatch.setattr(settings, "oss_signed_url_expire_seconds", 900)


def test_oss_storage_uploads_privately_and_signs_public_url(monkeypatch):
    _configure_oss(monkeypatch)
    calls: list[dict] = []

    class FakeBucket:
        def __init__(self, _auth, endpoint, bucket_name, *, is_cname):
            self.endpoint = endpoint
            self.bucket_name = bucket_name
            calls.append(
                {
                    "operation": "bucket",
                    "endpoint": endpoint,
                    "bucket": bucket_name,
                    "is_cname": is_cname,
                }
            )

        def put_object(self, key, data, *, headers):
            calls.append(
                {
                    "operation": "put",
                    "endpoint": self.endpoint,
                    "key": key,
                    "data": data,
                    "headers": headers,
                }
            )
            return SimpleNamespace(status=200)

        def delete_object(self, key):
            calls.append({"operation": "delete", "endpoint": self.endpoint, "key": key})

        def sign_url(self, method, key, expires, *, params, slash_safe):
            calls.append(
                {
                    "operation": "sign",
                    "endpoint": self.endpoint,
                    "method": method,
                    "key": key,
                    "expires": expires,
                    "params": params,
                    "slash_safe": slash_safe,
                }
            )
            return f"https://signed.example/{key}?signature=test"

    monkeypatch.setattr(asset_storage.oss2, "Bucket", FakeBucket)
    storage = OssAssetStorage()
    key = storage.object_key(f"{uuid4()}/{uuid4()}/{uuid4()}-0.png")
    storage.put_object(key, b"png-data", "image/png")
    parts = key.split("/")
    storage.validate_project_key(key, parts[-3], parts[-2])
    url = storage.signed_get_url(key, "generated image.png")
    storage.delete_object(key)

    assert key.startswith("thinktv/development/")
    assert url.startswith("https://signed.example/thinktv/development/")
    put = next(call for call in calls if call["operation"] == "put")
    assert put["endpoint"] == "https://oss-cn-test-internal.aliyuncs.com"
    assert put["headers"]["Content-Type"] == "image/png"
    signed = next(call for call in calls if call["operation"] == "sign")
    assert signed["endpoint"] == "https://oss-cn-test.aliyuncs.com"
    assert signed["expires"] == 900
    assert signed["params"] == {
        "response-content-disposition": "attachment; filename*=UTF-8''generated%20image.png"
    }
    assert storage_metadata(storage) == {
        "storage_backend": "oss",
        "storage_bucket": "thinktv-development",
        "storage_environment": "development",
    }


def test_storage_for_asset_keeps_local_compatibility_and_rejects_other_bucket(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(settings, "generated_storage_root", str(tmp_path))
    local = storage_for_asset({})
    assert isinstance(local, LocalAssetStorage)
    key = local.object_key(f"{uuid4()}/{uuid4()}/asset.png")
    local.put_object(key, b"local", "image/png")
    assert local.local_path(key).read_bytes() == b"local"
    assert local.read_object(key, 5) == b"local"
    with pytest.raises(AssetStorageError, match="size limit"):
        local.read_object(key, 4)

    _configure_oss(monkeypatch, bucket="thinktv-development")
    with pytest.raises(AssetStorageConfigurationError, match="not configured"):
        storage_for_asset(
            {"storage_backend": "oss", "storage_bucket": "thinktv-production"}
        )
    with pytest.raises(AssetStorageConfigurationError, match="different application"):
        storage_for_asset(
            {
                "storage_backend": "oss",
                "storage_bucket": "thinktv-development",
                "storage_environment": "production",
            }
        )

    oss = OssAssetStorage()
    with pytest.raises(ValueError, match="does not belong"):
        oss.validate_project_key(
            "thinktv/development/other-user/other-project/asset.png",
            uuid4(),
            uuid4(),
        )


def test_oss_storage_reads_through_internal_endpoint_with_hard_limit(monkeypatch):
    _configure_oss(monkeypatch)
    calls: list[tuple[str, str, tuple[int, int]]] = []

    class FakeObject:
        def __init__(self, data: bytes):
            self.data = data

        def read(self, amount: int) -> bytes:
            return self.data[:amount]

    class FakeBucket:
        def __init__(self, _auth, endpoint, _bucket_name, *, is_cname):
            self.endpoint = endpoint

        def get_object(self, key, *, byte_range):
            calls.append((self.endpoint, key, byte_range))
            return FakeObject(b"123456")

    monkeypatch.setattr(asset_storage.oss2, "Bucket", FakeBucket)
    storage = OssAssetStorage()
    key = storage.object_key(f"{uuid4()}/{uuid4()}/input.png")

    assert storage.read_object(key, 6) == b"123456"
    with pytest.raises(AssetStorageError, match="size limit"):
        storage.read_object(key, 5)
    assert calls[0] == (
        "https://oss-cn-test-internal.aliyuncs.com",
        key,
        (0, 6),
    )


def test_oss_asset_content_redirects_only_after_api_auth(monkeypatch):
    signed_requests: list[tuple[str, str | None]] = []

    class FakeRemoteStorage:
        def validate_project_key(self, _key, _owner_id, _project_id) -> None:
            return None

        def signed_get_url(self, key: str, download_name: str | None = None) -> str:
            signed_requests.append((key, download_name))
            return "https://private-bucket.oss.example/signed-object"

    monkeypatch.setattr(
        "app.controllers.project_domain.storage_for_asset",
        lambda _metadata: FakeRemoteStorage(),
    )
    client = TestClient(app)
    email = f"oss-content-{uuid4()}@example.com"
    try:
        registered = client.post(
            "/api/auth/register", json={"email": email, "password": "Strong123"}
        )
        assert registered.status_code == 201
        user_id = registered.json()["user"]["id"]
        project = client.post("/api/projects", json={"name": "OSS content"})
        assert project.status_code == 201
        project_id = project.json()["id"]
        key = f"{user_id}/{project_id}/{uuid4()}.png"
        asset = client.post(
            "/api/assets",
            json={
                "project_id": project_id,
                "kind": "image",
                "source": "generation",
                "storage_key": key,
                "original_name": "result.png",
                "mime_type": "image/png",
                "byte_size": 123,
                "status": "ready",
                "metadata": {
                    "storage_backend": "oss",
                    "storage_bucket": "thinktv-development",
                },
            },
        )
        assert asset.status_code == 201, asset.text

        content = client.get(
            f"/api/assets/{asset.json()['id']}/content", follow_redirects=False
        )
        assert content.status_code == 307
        assert content.headers["location"] == (
            "https://private-bucket.oss.example/signed-object"
        )
        assert content.headers["cache-control"] == "private, no-store"
        assert signed_requests[-1] == (key, None)

        download = client.get(
            f"/api/assets/{asset.json()['id']}/content?download=1",
            follow_redirects=False,
        )
        assert download.status_code == 307
        assert signed_requests[-1] == (key, "result.png")
        assert TestClient(app).get(
            f"/api/assets/{asset.json()['id']}/content",
            follow_redirects=False,
        ).status_code == 401
    finally:
        with SessionLocal() as db:
            user = db.scalar(select(User).where(User.email == email))
            if user is not None:
                db.delete(user)
                db.commit()
