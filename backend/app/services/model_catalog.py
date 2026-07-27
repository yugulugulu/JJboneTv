from uuid import UUID

from sqlalchemy.orm import Session

from ..core.model_credentials import decrypt_api_key, encrypt_api_key
from ..exceptions import DomainValidationError, ResourceNotFoundError
from ..models import AIModel, UserModelCredential
from ..repositories.model_catalog import ModelCatalogRepository


_SENSITIVE_METADATA_KEY_PARTS = (
    "apikey",
    "baseurl",
    "credential",
    "authorization",
    "password",
    "passwd",
    "accesstoken",
    "refreshtoken",
    "bearertoken",
    "clientsecret",
    "secretkey",
    "accesskey",
    "privatekey",
    "encryptionkey",
    "encrypted",
    "secret",
)


def _normalized_metadata_key(key: str) -> str:
    return "".join(character for character in key.casefold() if character.isalnum())


def _is_sensitive_metadata_key(key: str) -> bool:
    normalized = _normalized_metadata_key(key)
    return (
        normalized in {"secret", "token"}
        or normalized.endswith("secret")
        or normalized.endswith("token")
        or any(part in normalized for part in _SENSITIVE_METADATA_KEY_PARTS)
    )


def sanitize_public_metadata(value):
    """Return a recursive copy safe for unauthenticated provider-catalog clients."""

    if isinstance(value, dict):
        return {
            key: sanitize_public_metadata(item)
            for key, item in value.items()
            if isinstance(key, str) and not _is_sensitive_metadata_key(key)
        }
    if isinstance(value, list):
        return [sanitize_public_metadata(item) for item in value]
    return value


def validate_http_api_key(api_key: str) -> str:
    value = api_key.strip()
    if not value:
        raise DomainValidationError("API Key 不能为空")
    if value.lower().startswith("bearer "):
        raise DomainValidationError("只需填写 API Key 本身，请移除 Bearer 前缀")
    if any(ord(character) < 33 or ord(character) > 126 for character in value):
        raise DomainValidationError("API Key 只能包含英文、数字和 ASCII 符号")
    return value


def public_model(model: AIModel) -> dict:
    return {
        "id": model.id,
        "group_id": model.group_id,
        "model_name": model.model_name,
        "display_name": model.display_name,
        "name": model.display_name,
        "kind": model.kind,
        "description": model.description,
        "display_order": model.display_order,
        "config_schema": model.config_schema,
        "default_parameters": model.default_parameters,
        "metadata": sanitize_public_metadata(model.model_metadata),
        "enabled": True,
    }


def credential_status(credential: UserModelCredential | None, group_id: UUID) -> dict:
    return {
        "group_id": group_id,
        "credential_configured": credential is not None,
        "api_key_last_four": credential.api_key_last_four if credential else None,
        "credential_updated_at": credential.updated_at if credential else None,
    }


class ModelCatalogService:
    def __init__(self, db: Session):
        self.db = db
        self.repo = ModelCatalogRepository(db)

    def list_groups(self, user_id: UUID) -> list[dict]:
        rows = self.repo.list_active_groups_with_credentials(user_id)
        models = self.repo.list_active_models_for_groups([group.id for group, _ in rows])
        by_group: dict[UUID, list[dict]] = {}
        for model in models:
            by_group.setdefault(model.group_id, []).append(public_model(model))
        return [
            {
                "id": group.id,
                "name": group.name,
                "description": group.description,
                "display_order": group.display_order,
                **credential_status(credential, group.id),
                "models": by_group.get(group.id, []),
            }
            for group, credential in rows
        ]

    def list_available_models(self, user_id: UUID) -> list[dict]:
        return [public_model(model) for model in self.repo.list_available_models(user_id)]

    def save_credential(self, user_id: UUID, group_id: UUID, api_key: str) -> dict:
        if self.repo.get_active_group(group_id) is None:
            raise ResourceNotFoundError
        api_key = validate_http_api_key(api_key)
        credential = self.repo.save_credential(
            user_id,
            group_id,
            encrypt_api_key(self.db, api_key),
            api_key[-4:],
        )
        self.db.commit()
        self.db.refresh(credential)
        return credential_status(credential, group_id)

    def delete_credential(self, user_id: UUID, group_id: UUID) -> None:
        if self.repo.get_active_group(group_id) is None:
            raise ResourceNotFoundError
        self.repo.delete_credential(user_id, group_id)
        self.db.commit()

    def resolve_provider_configuration(
        self, user_id: UUID, model_id: UUID, *, lock_model: bool = False
    ) -> dict:
        row = self.repo.get_available_model_with_credentials(
            user_id, model_id, lock_model=lock_model
        )
        if row is None:
            raise ResourceNotFoundError
        model, credential = row
        return {
            "model_id": model.id,
            "group_id": model.group_id,
            "base_url": model.base_url,
            "model_name": model.model_name,
            "kind": model.kind,
            "config_schema": model.config_schema,
            "default_parameters": model.default_parameters,
            "metadata": model.model_metadata,
            "api_key": decrypt_api_key(self.db, credential.encrypted_api_key),
        }
