import re
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import quote, urlparse
from uuid import UUID

import oss2

from ..config import settings


class AssetStorageError(RuntimeError):
    pass


class AssetStorageConfigurationError(AssetStorageError):
    pass


def _safe_object_key(value: str) -> str:
    key = value.strip().lstrip("/")
    parts = key.split("/")
    if not key or any(part in {"", ".", ".."} for part in parts):
        raise ValueError("Invalid asset storage key")
    normalized = PurePosixPath(key).as_posix()
    if normalized != key:
        raise ValueError("Invalid asset storage key")
    return normalized


def generated_asset_path(storage_key: str) -> Path:
    root = Path(settings.generated_storage_root).resolve()
    path = (root / storage_key).resolve()
    if root != path and root not in path.parents:
        raise ValueError("Invalid generated asset storage key")
    return path


def project_asset_path(storage_key: str, owner_id: UUID, project_id: UUID) -> Path:
    root = Path(settings.generated_storage_root).resolve()
    namespace = (root / str(owner_id) / str(project_id)).resolve()
    path = generated_asset_path(storage_key)
    if namespace != path and namespace not in path.parents:
        raise ValueError("Asset storage key does not belong to this project")
    return path


@dataclass(frozen=True)
class LocalAssetStorage:
    backend: str = "local"
    bucket_name: str | None = None

    def object_key(self, relative_key: str) -> str:
        return _safe_object_key(relative_key)

    def put_object(self, storage_key: str, data: bytes, mime_type: str) -> None:
        path = generated_asset_path(storage_key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def put_file(self, storage_key: str, source_path: Path, mime_type: str) -> None:
        path = generated_asset_path(storage_key)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=path.parent,
                prefix=f".{path.name}.",
                suffix=".upload",
                delete=False,
            ) as destination:
                temporary_path = Path(destination.name)
                with source_path.open("rb") as source:
                    shutil.copyfileobj(source, destination, length=1024 * 1024)
                destination.flush()
                os.fsync(destination.fileno())
            os.replace(temporary_path, path)
        except OSError as error:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            raise AssetStorageError("Failed to upload asset to local storage") from error

    def delete_object(self, storage_key: str) -> None:
        generated_asset_path(storage_key).unlink(missing_ok=True)

    def read_object(self, storage_key: str, max_bytes: int) -> bytes:
        if max_bytes <= 0:
            raise AssetStorageError("Asset read limit must be positive")
        try:
            with generated_asset_path(storage_key).open("rb") as file:
                data = file.read(max_bytes + 1)
        except OSError as error:
            raise AssetStorageError("Failed to read local asset") from error
        if len(data) > max_bytes:
            raise AssetStorageError("Asset exceeds the configured input size limit")
        return data

    def copy_object_to_file(
        self, storage_key: str, destination_path: Path, max_bytes: int
    ) -> int:
        if max_bytes <= 0:
            raise AssetStorageError("Asset read limit must be positive")
        written = 0
        try:
            with generated_asset_path(storage_key).open("rb") as source, destination_path.open(
                "wb"
            ) as destination:
                while chunk := source.read(1024 * 1024):
                    written += len(chunk)
                    if written > max_bytes:
                        raise AssetStorageError(
                            "Asset exceeds the configured input size limit"
                        )
                    destination.write(chunk)
        except AssetStorageError:
            destination_path.unlink(missing_ok=True)
            raise
        except OSError as error:
            destination_path.unlink(missing_ok=True)
            raise AssetStorageError("Failed to stream local asset") from error
        return written

    def validate_project_key(
        self, storage_key: str, owner_id: UUID, project_id: UUID
    ) -> None:
        project_asset_path(storage_key, owner_id, project_id)

    def local_path(self, storage_key: str) -> Path:
        return generated_asset_path(storage_key)


class OssAssetStorage:
    backend = "oss"

    def __init__(self) -> None:
        self.endpoint = self._validated_endpoint(settings.oss_endpoint, "OSS_ENDPOINT")
        internal = settings.oss_internal_endpoint.strip()
        self.internal_endpoint = (
            self._validated_endpoint(internal, "OSS_INTERNAL_ENDPOINT")
            if internal
            else self.endpoint
        )
        self.bucket_name = settings.oss_bucket_name.strip()
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]", self.bucket_name):
            raise AssetStorageConfigurationError("OSS_BUCKET_NAME is invalid")
        access_key_id = settings.oss_access_key_id.strip()
        access_key_secret = settings.oss_access_key_secret.strip()
        if not access_key_id or not access_key_secret:
            raise AssetStorageConfigurationError("OSS credentials are not configured")
        security_token = settings.oss_security_token.strip()
        self.auth = (
            oss2.StsAuth(access_key_id, access_key_secret, security_token)
            if security_token
            else oss2.Auth(access_key_id, access_key_secret)
        )
        prefix = settings.oss_prefix.strip().strip("/")
        self.prefix = _safe_object_key(prefix) if prefix else ""
        self.is_cname = settings.oss_cname

    @staticmethod
    def _validated_endpoint(value: str, name: str) -> str:
        endpoint = value.strip().rstrip("/")
        parsed = urlparse(endpoint)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise AssetStorageConfigurationError(f"{name} is invalid")
        return endpoint

    def _bucket(self, endpoint: str) -> oss2.Bucket:
        return oss2.Bucket(
            self.auth,
            endpoint,
            self.bucket_name,
            is_cname=self.is_cname,
        )

    def object_key(self, relative_key: str) -> str:
        relative = _safe_object_key(relative_key)
        return f"{self.prefix}/{relative}" if self.prefix else relative

    def put_object(self, storage_key: str, data: bytes, mime_type: str) -> None:
        key = _safe_object_key(storage_key)
        try:
            result = self._bucket(self.internal_endpoint).put_object(
                key,
                data,
                headers={
                    "Content-Type": mime_type,
                    "Cache-Control": "private, max-age=31536000, immutable",
                },
            )
        except Exception as error:
            raise AssetStorageError("Failed to upload generated asset to OSS") from error
        if not 200 <= result.status < 300:
            raise AssetStorageError("OSS rejected the generated asset upload")

    def put_file(self, storage_key: str, source_path: Path, mime_type: str) -> None:
        key = _safe_object_key(storage_key)
        try:
            result = self._bucket(self.internal_endpoint).put_object_from_file(
                key,
                str(source_path),
                headers={
                    "Content-Type": mime_type,
                    "Cache-Control": "private, max-age=31536000, immutable",
                },
            )
        except Exception as error:
            raise AssetStorageError("Failed to upload asset to OSS") from error
        if not 200 <= result.status < 300:
            raise AssetStorageError("OSS rejected the asset upload")

    def delete_object(self, storage_key: str) -> None:
        key = _safe_object_key(storage_key)
        try:
            self._bucket(self.internal_endpoint).delete_object(key)
        except Exception as error:
            raise AssetStorageError("Failed to delete generated asset from OSS") from error

    def read_object(self, storage_key: str, max_bytes: int) -> bytes:
        if max_bytes <= 0:
            raise AssetStorageError("Asset read limit must be positive")
        key = _safe_object_key(storage_key)
        try:
            result = self._bucket(self.internal_endpoint).get_object(
                key,
                byte_range=(0, max_bytes),
            )
            data = result.read(max_bytes + 1)
        except Exception as error:
            raise AssetStorageError("Failed to read asset from OSS") from error
        if len(data) > max_bytes:
            raise AssetStorageError("Asset exceeds the configured input size limit")
        return data

    def copy_object_to_file(
        self, storage_key: str, destination_path: Path, max_bytes: int
    ) -> int:
        if max_bytes <= 0:
            raise AssetStorageError("Asset read limit must be positive")
        key = _safe_object_key(storage_key)
        written = 0
        result = None
        try:
            result = self._bucket(self.internal_endpoint).get_object(key)
            with destination_path.open("wb") as destination:
                while chunk := result.read(1024 * 1024):
                    written += len(chunk)
                    if written > max_bytes:
                        raise AssetStorageError(
                            "Asset exceeds the configured input size limit"
                        )
                    destination.write(chunk)
        except AssetStorageError:
            destination_path.unlink(missing_ok=True)
            raise
        except Exception as error:
            destination_path.unlink(missing_ok=True)
            raise AssetStorageError("Failed to stream asset from OSS") from error
        finally:
            if result is not None:
                try:
                    result.close()
                except Exception:
                    pass
        return written

    def validate_project_key(
        self, storage_key: str, owner_id: UUID, project_id: UUID
    ) -> None:
        key = _safe_object_key(storage_key)
        project_prefix = self.object_key(f"{owner_id}/{project_id}") + "/"
        if not key.startswith(project_prefix):
            raise ValueError("Asset storage key does not belong to this project")

    def signed_get_url(self, storage_key: str, download_name: str | None = None) -> str:
        key = _safe_object_key(storage_key)
        expires = settings.oss_signed_url_expire_seconds
        if expires < 60 or expires > 86400:
            raise AssetStorageConfigurationError(
                "OSS_SIGNED_URL_EXPIRE_SECONDS must be between 60 and 86400"
            )
        params = None
        if download_name:
            encoded = quote(download_name, safe="")
            params = {
                "response-content-disposition": f"attachment; filename*=UTF-8''{encoded}"
            }
        try:
            return self._bucket(self.endpoint).sign_url(
                "GET",
                key,
                expires,
                params=params,
                slash_safe=True,
            )
        except Exception as error:
            raise AssetStorageError("Failed to sign the OSS asset URL") from error


def generation_asset_storage() -> LocalAssetStorage | OssAssetStorage:
    backend = settings.asset_storage_backend.strip().lower()
    if backend == "local":
        return LocalAssetStorage()
    if backend == "oss":
        return OssAssetStorage()
    raise AssetStorageConfigurationError(
        "ASSET_STORAGE_BACKEND must be either local or oss"
    )


def storage_metadata(storage: LocalAssetStorage | OssAssetStorage) -> dict:
    return {
        "storage_backend": storage.backend,
        "storage_bucket": storage.bucket_name,
        "storage_environment": settings.app_environment,
    }


def storage_for_asset(metadata: dict | None) -> LocalAssetStorage | OssAssetStorage:
    details = metadata if isinstance(metadata, dict) else {}
    backend = details.get("storage_backend", "local")
    if backend == "local":
        return LocalAssetStorage()
    if backend != "oss":
        raise AssetStorageConfigurationError("Asset uses an unsupported storage backend")
    storage = OssAssetStorage()
    recorded_bucket = details.get("storage_bucket")
    if recorded_bucket and recorded_bucket != storage.bucket_name:
        raise AssetStorageConfigurationError(
            "Asset belongs to an OSS bucket that is not configured in this environment"
        )
    recorded_environment = details.get("storage_environment")
    if recorded_environment and recorded_environment != settings.app_environment:
        raise AssetStorageConfigurationError(
            "Asset belongs to a different application environment"
        )
    return storage
