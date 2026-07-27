import uuid
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import AIModel, ModelGroup, UserModelCredential


class ModelCatalogRepository:
    def __init__(self, db: Session):
        self.db = db

    def list_active_groups_with_credentials(
        self, user_id: UUID
    ) -> list[tuple[ModelGroup, UserModelCredential | None]]:
        return list(
            self.db.execute(
                select(ModelGroup, UserModelCredential)
                .outerjoin(
                    UserModelCredential,
                    (UserModelCredential.group_id == ModelGroup.id)
                    & (UserModelCredential.user_id == user_id),
                )
                .where(ModelGroup.status == "active")
                .order_by(ModelGroup.display_order, ModelGroup.name, ModelGroup.id)
            ).all()
        )

    def list_active_models_for_groups(self, group_ids: list[UUID]) -> list[AIModel]:
        if not group_ids:
            return []
        return list(
            self.db.scalars(
                select(AIModel)
                .where(AIModel.group_id.in_(group_ids), AIModel.status == "active")
                .order_by(AIModel.display_order, AIModel.display_name, AIModel.id)
            )
        )

    def list_available_models(self, user_id: UUID) -> list[AIModel]:
        user_credential_exists = (
            select(UserModelCredential.id)
            .where(
                UserModelCredential.group_id == AIModel.group_id,
                UserModelCredential.user_id == user_id,
            )
            .exists()
        )
        return list(
            self.db.scalars(
                select(AIModel)
                .join(ModelGroup, ModelGroup.id == AIModel.group_id)
                .where(
                    AIModel.status == "active",
                    ModelGroup.status == "active",
                    user_credential_exists,
                )
                .order_by(
                    ModelGroup.display_order,
                    AIModel.display_order,
                    AIModel.display_name,
                    AIModel.id,
                )
            )
        )

    def get_active_group(self, group_id: UUID) -> ModelGroup | None:
        return self.db.scalar(
            select(ModelGroup).where(ModelGroup.id == group_id, ModelGroup.status == "active")
        )

    def get_credential(self, user_id: UUID, group_id: UUID) -> UserModelCredential | None:
        return self.db.scalar(
            select(UserModelCredential).where(
                UserModelCredential.user_id == user_id,
                UserModelCredential.group_id == group_id,
            )
        )

    def save_credential(
        self, user_id: UUID, group_id: UUID, encrypted_api_key: bytes, last_four: str
    ) -> UserModelCredential:
        credential = self.get_credential(user_id, group_id)
        if credential is None:
            credential = UserModelCredential(
                id=uuid.uuid4(),
                user_id=user_id,
                group_id=group_id,
                encrypted_api_key=encrypted_api_key,
                api_key_last_four=last_four,
            )
            self.db.add(credential)
        else:
            credential.encrypted_api_key = encrypted_api_key
            credential.api_key_last_four = last_four
        return credential

    def delete_credential(self, user_id: UUID, group_id: UUID) -> bool:
        credential = self.get_credential(user_id, group_id)
        if credential is None:
            return False
        self.db.delete(credential)
        return True

    def get_available_model_with_credentials(
        self, user_id: UUID, model_id: UUID, *, lock_model: bool = False
    ) -> tuple[AIModel, UserModelCredential] | None:
        statement = (
            select(AIModel, UserModelCredential)
            .join(ModelGroup, ModelGroup.id == AIModel.group_id)
            .join(
                UserModelCredential,
                (UserModelCredential.group_id == AIModel.group_id)
                & (UserModelCredential.user_id == user_id),
            )
            .where(
                AIModel.id == model_id,
                AIModel.status == "active",
                ModelGroup.status == "active",
            )
        )
        if lock_model:
            statement = statement.with_for_update(of=AIModel)
        return self.db.execute(statement).one_or_none()
