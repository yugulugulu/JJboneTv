from datetime import date, datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import (
    AdminAccount,
    AdminAuditLog,
    AIModel,
    Asset,
    Folder,
    GenerationTask,
    ModelProviderCredential,
    ModelGroup,
    Project,
    User,
)


SENSITIVE_AUDIT_FIELDS = {
    "api_key",
    "apikey",
    "authorization",
    "encrypted_api_key",
    "password",
    "password_hash",
    "secret",
    "token",
}


def _safe_audit_value(value):
    if isinstance(value, dict):
        safe = {}
        for key, item in value.items():
            normalized_key = str(key).lower().replace("-", "_")
            if normalized_key in SENSITIVE_AUDIT_FIELDS or normalized_key.endswith("_api_key"):
                safe[str(key)] = "[REDACTED]"
            else:
                safe[str(key)] = _safe_audit_value(item)
        return safe
    if isinstance(value, (list, tuple)):
        return [_safe_audit_value(item) for item in value]
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


class AdminRepository:
    def __init__(self, db: Session):
        self.db = db

    def get_admin_by_id(self, admin_id: UUID) -> AdminAccount | None:
        return self.db.get(AdminAccount, admin_id)

    def get_admin_by_email(self, email: str) -> AdminAccount | None:
        return self.db.scalar(
            select(AdminAccount).where(func.lower(AdminAccount.email) == email.lower())
        )

    def create_admin(self, email: str, password_hash: str) -> AdminAccount:
        admin = AdminAccount(email=email, password_hash=password_hash)
        self.db.add(admin)
        return admin

    @staticmethod
    def _user_counts():
        project_count = (
            select(func.count(Project.id))
            .where(Project.owner_id == User.id, Project.deleted_at.is_(None))
            .correlate(User)
            .scalar_subquery()
        )
        folder_count = (
            select(func.count(Folder.id))
            .where(Folder.owner_id == User.id, Folder.deleted_at.is_(None))
            .correlate(User)
            .scalar_subquery()
        )
        asset_count = (
            select(func.count(Asset.id))
            .where(Asset.owner_id == User.id, Asset.deleted_at.is_(None))
            .correlate(User)
            .scalar_subquery()
        )
        return project_count, folder_count, asset_count

    def list_users(
        self,
        *,
        search: str | None,
        page: int,
        page_size: int,
        sort_order: str,
    ) -> tuple[list[dict], int]:
        project_count, folder_count, asset_count = self._user_counts()
        filters = []
        if search:
            escaped = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            filters.append(User.email.ilike(f"%{escaped}%", escape="\\"))
        total = self.db.scalar(select(func.count(User.id)).where(*filters)) or 0
        order = User.created_at.asc() if sort_order == "asc" else User.created_at.desc()
        id_order = User.id.asc() if sort_order == "asc" else User.id.desc()
        rows = self.db.execute(
            select(
                User.id,
                User.email,
                User.is_active,
                User.disabled_at,
                User.disabled_by_admin_id,
                User.created_at,
                project_count.label("project_count"),
                folder_count.label("folder_count"),
                asset_count.label("asset_count"),
            )
            .where(*filters)
            .order_by(order, id_order)
            .offset((page - 1) * page_size)
            .limit(page_size)
        ).mappings()
        return [dict(row) for row in rows], total

    def get_user_detail(self, user_id: UUID) -> dict | None:
        project_count, folder_count, asset_count = self._user_counts()
        row = self.db.execute(
            select(
                User.id,
                User.email,
                User.is_active,
                User.disabled_at,
                User.disabled_by_admin_id,
                User.created_at,
                project_count.label("project_count"),
                folder_count.label("folder_count"),
                asset_count.label("asset_count"),
            ).where(User.id == user_id)
        ).mappings().one_or_none()
        return dict(row) if row else None

    def list_models(self) -> list[tuple[AIModel, ModelProviderCredential | None]]:
        return list(
            self.db.execute(
                select(AIModel, ModelProviderCredential)
                .outerjoin(ModelProviderCredential, ModelProviderCredential.model_id == AIModel.id)
                .order_by(AIModel.display_order, AIModel.display_name, AIModel.id)
            ).all()
        )

    def get_model(self, model_id: UUID) -> AIModel | None:
        return self.db.get(AIModel, model_id)

    def get_model_credential(self, model_id: UUID) -> ModelProviderCredential | None:
        return self.db.get(ModelProviderCredential, model_id)

    def save_model_credential(
        self, model_id: UUID, encrypted_api_key: bytes, last_four: str, admin_id: UUID
    ) -> ModelProviderCredential:
        credential = self.get_model_credential(model_id)
        if credential is None:
            credential = ModelProviderCredential(
                model_id=model_id,
                encrypted_api_key=encrypted_api_key,
                api_key_last_four=last_four,
                created_by_admin_id=admin_id,
                updated_by_admin_id=admin_id,
            )
            self.db.add(credential)
        else:
            credential.encrypted_api_key = encrypted_api_key
            credential.api_key_last_four = last_four
            credential.updated_by_admin_id = admin_id
        return credential

    def get_user_for_update(self, user_id: UUID) -> User | None:
        return self.db.scalar(select(User).where(User.id == user_id).with_for_update())

    def get_model_for_update(self, model_id: UUID) -> AIModel | None:
        return self.db.scalar(select(AIModel).where(AIModel.id == model_id).with_for_update())

    def get_model_group_for_update(self, group_id: UUID) -> ModelGroup | None:
        return self.db.scalar(
            select(ModelGroup).where(ModelGroup.id == group_id).with_for_update()
        )

    def get_model_group_for_share(self, group_id: UUID) -> ModelGroup | None:
        return self.db.scalar(
            select(ModelGroup).where(ModelGroup.id == group_id).with_for_update(read=True)
        )

    def model_has_active_generation(self, model_id: UUID) -> bool:
        return (
            self.db.scalar(
                select(GenerationTask.id)
                .where(
                    GenerationTask.model == str(model_id),
                    GenerationTask.status.in_(("queued", "running")),
                )
                .limit(1)
            )
            is not None
        )

    def model_group_has_models(self, group_id: UUID) -> bool:
        return (
            self.db.scalar(select(AIModel.id).where(AIModel.group_id == group_id).limit(1))
            is not None
        )

    def delete_model(self, model: AIModel) -> None:
        self.db.delete(model)

    def delete_model_group(self, group: ModelGroup) -> None:
        self.db.delete(group)

    def list_model_groups(self) -> list[dict]:
        model_count = (
            select(func.count(AIModel.id))
            .where(AIModel.group_id == ModelGroup.id)
            .correlate(ModelGroup)
            .scalar_subquery()
        )
        rows = self.db.execute(
            select(ModelGroup, model_count.label("model_count")).order_by(
                ModelGroup.display_order, ModelGroup.name, ModelGroup.id
            )
        )
        return [
            {
                "id": group.id,
                "name": group.name,
                "description": group.description,
                "runtime_limits": group.runtime_limits,
                "status": group.status,
                "display_order": group.display_order,
                "created_by_admin_id": group.created_by_admin_id,
                "created_at": group.created_at,
                "updated_at": group.updated_at,
                "model_count": count,
            }
            for group, count in rows
        ]

    def get_model_group(self, group_id: UUID) -> ModelGroup | None:
        return self.db.get(ModelGroup, group_id)

    def add_audit(
        self,
        *,
        admin_id: UUID,
        action: str,
        entity_id: UUID,
        changes: dict,
        entity_type: str = "model",
    ) -> AdminAuditLog:
        log = AdminAuditLog(
            admin_account_id=admin_id,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            changes=_safe_audit_value(changes),
        )
        self.db.add(log)
        return log
