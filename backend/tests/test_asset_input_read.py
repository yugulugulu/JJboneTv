import pytest

from app.config import settings
from app.services import asset_storage
from app.services.asset_storage import AssetStorageError, LocalAssetStorage, OssAssetStorage


def _configure_oss(monkeypatch) -> None:
    monkeypatch.setattr(settings, "oss_endpoint", "https://oss-cn-test.aliyuncs.com")
    monkeypatch.setattr(
        settings,
        "oss_internal_endpoint",
        "https://oss-cn-test-internal.aliyuncs.com",
    )
    monkeypatch.setattr(settings, "oss_bucket_name", "thinktv-development")
    monkeypatch.setattr(settings, "oss_access_key_id", "test-access-key-id")
    monkeypatch.setattr(settings, "oss_access_key_secret", "test-access-key-secret")
    monkeypatch.setattr(settings, "oss_security_token", "")
    monkeypatch.setattr(settings, "oss_prefix", "thinktv/development")
    monkeypatch.setattr(settings, "oss_cname", False)


def test_local_input_read_is_bounded_and_fails_closed(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "generated_storage_root", str(tmp_path))
    storage = LocalAssetStorage()
    key = "owner/project/input.png"
    storage.put_object(key, b"123456", "image/png")

    assert storage.read_object(key, 6) == b"123456"
    with pytest.raises(AssetStorageError, match="size limit"):
        storage.read_object(key, 5)
    with pytest.raises(AssetStorageError, match="positive"):
        storage.read_object(key, 0)
    with pytest.raises(AssetStorageError, match="Failed to read"):
        storage.read_object("owner/project/missing.png", 6)
    with pytest.raises(ValueError, match="Invalid generated asset storage key"):
        storage.read_object("../../outside.png", 6)


def test_oss_input_read_uses_internal_endpoint_and_limit_plus_one(monkeypatch):
    _configure_oss(monkeypatch)
    calls: list[dict] = []
    payload = b"1234567"

    class FakeObject:
        def read(self, amount: int) -> bytes:
            calls.append({"operation": "read", "amount": amount})
            return payload[:amount]

    class FakeBucket:
        def __init__(self, _auth, endpoint, bucket_name, *, is_cname):
            self.endpoint = endpoint
            calls.append(
                {
                    "operation": "bucket",
                    "endpoint": endpoint,
                    "bucket_name": bucket_name,
                    "is_cname": is_cname,
                }
            )

        def get_object(self, key, *, byte_range):
            calls.append(
                {
                    "operation": "get",
                    "endpoint": self.endpoint,
                    "key": key,
                    "byte_range": byte_range,
                }
            )
            return FakeObject()

    monkeypatch.setattr(asset_storage.oss2, "Bucket", FakeBucket)
    storage = OssAssetStorage()
    key = storage.object_key("owner/project/input.png")

    assert storage.read_object(key, 7) == payload
    with pytest.raises(AssetStorageError, match="size limit"):
        storage.read_object(key, 6)

    get_calls = [call for call in calls if call["operation"] == "get"]
    assert get_calls == [
        {
            "operation": "get",
            "endpoint": "https://oss-cn-test-internal.aliyuncs.com",
            "key": key,
            "byte_range": (0, 7),
        },
        {
            "operation": "get",
            "endpoint": "https://oss-cn-test-internal.aliyuncs.com",
            "key": key,
            "byte_range": (0, 6),
        },
    ]
    assert [call["amount"] for call in calls if call["operation"] == "read"] == [8, 7]


def test_oss_input_read_wraps_remote_failures(monkeypatch):
    _configure_oss(monkeypatch)

    class FailingBucket:
        def __init__(self, *_args, **_kwargs):
            pass

        def get_object(self, _key, *, byte_range):
            assert byte_range == (0, 32)
            raise RuntimeError("secret provider detail")

    monkeypatch.setattr(asset_storage.oss2, "Bucket", FailingBucket)
    storage = OssAssetStorage()

    with pytest.raises(AssetStorageError) as captured:
        storage.read_object(storage.object_key("owner/project/input.png"), 32)
    assert str(captured.value) == "Failed to read asset from OSS"
    assert "secret provider detail" not in str(captured.value)
