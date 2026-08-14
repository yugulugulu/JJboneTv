import uuid
from datetime import datetime, timezone
from uuid import UUID

from email_validator import EmailNotValidError, validate_email
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..admin_schemas import (
    AdminModelCreate,
    AdminModelGroupCreate,
    AdminModelGroupUpdate,
    AdminModelUpdate,
    AdminTextModelCreate,
    AdminTextModelUpdate,
    AdminUserStatusUpdate,
)
from ..core.security import hash_password, verify_password
from ..exceptions import (
    ArchivedModelError,
    DuplicateModelGroupNameError,
    DuplicateModelKeyError,
    DomainValidationError,
    InvalidCredentialsError,
    ModelGroupNotActiveError,
    ModelGroupNotEmptyError,
    ModelInUseError,
    ModelKindMismatchError,
    ResourceNotFoundError,
)
from ..models import AdminAccount, AIModel, ModelGroup, ModelProviderCredential, User
from ..repositories.admin import AdminRepository
from .image_quality_options import (
    IMAGE_QUALITY_OPTIONS_KEY,
    normalize_image_quality_metadata,
)
from .image_size_matrix import IMAGE_SIZE_MATRIX_KEY, normalize_model_metadata
from .video_model_options import (
    VIDEO_OPTIONS_KEY,
    extract_normalized_provider_config,
    normalize_video_options,
)


MODEL_FIELDS = (
    "group_id",
    "model_name",
    "base_url",
    "display_name",
    "kind",
    "description",
    "status",
    "display_order",
    "config_schema",
    "default_parameters",
    "metadata",
)

MODEL_GROUP_FIELDS = ("name", "description", "runtime_limits", "status", "display_order")


def validate_bootstrap_password(password: str) -> None:
    if len(password) < 16:
        raise RuntimeError("ADMIN_BOOTSTRAP_PASSWORD must contain at least 16 characters")
    if not any(character.isupper() for character in password):
        raise RuntimeError("ADMIN_BOOTSTRAP_PASSWORD must contain an uppercase letter")
    if not any(character.islower() for character in password):
        raise RuntimeError("ADMIN_BOOTSTRAP_PASSWORD must contain a lowercase letter")
    if not any(character.isdigit() for character in password):
        raise RuntimeError("ADMIN_BOOTSTRAP_PASSWORD must contain a number")


def normalize_admin_email(email: str) -> str:
    candidate = email.strip()
    local_domain = candidate.rsplit("@", 1)
    validation_candidate = candidate
    if len(local_domain) == 2 and local_domain[1].lower().endswith(".local"):
        domain = local_domain[1][:-6]
        validation_candidate = f"{local_domain[0]}@{domain}.com"
    try:
        validate_email(validation_candidate, check_deliverability=False)
    except EmailNotValidError as error:
        raise RuntimeError("ADMIN_BOOTSTRAP_EMAIL must be a valid email address") from error
    return candidate.lower()


class AdminService:
    def __init__(self, db: Session):
        self.db = db
        self.repo = AdminRepository(db)

    def bootstrap(self, email: str, password: str) -> AdminAccount | None:
        if not email and not password:
            return None
        if not email or not password:
            raise RuntimeError(
                "ADMIN_BOOTSTRAP_EMAIL and ADMIN_BOOTSTRAP_PASSWORD must be configured together"
            )
        normalized_email = normalize_admin_email(email)
        validate_bootstrap_password(password)
        existing = self.repo.get_admin_by_email(normalized_email)
        if existing is not None:
            return existing
        admin = self.repo.create_admin(normalized_email, hash_password(password))
        try:
            self.db.commit()
        except IntegrityError:
            self.db.rollback()
            existing = self.repo.get_admin_by_email(normalized_email)
            if existing is None:
                raise
            return existing
        self.db.refresh(admin)
        return admin

    def login(self, email: str, password: str) -> AdminAccount:
        admin = self.repo.get_admin_by_email(email.strip().lower())
        if admin is None or not admin.is_active or not verify_password(password, admin.password_hash):
            raise InvalidCredentialsError
        return admin

    def list_users(
        self, *, search: str | None, page: int, page_size: int, sort_order: str
    ) -> tuple[list[dict], int]:
        return self.repo.list_users(
            search=search.strip() if search else None,
            page=page,
            page_size=page_size,
            sort_order=sort_order,
        )

    def get_user(self, user_id: UUID) -> dict:
        user = self.repo.get_user_detail(user_id)
        if user is None:
            raise ResourceNotFoundError
        return user

    def update_user_status(
        self, admin_id: UUID, user_id: UUID, payload: AdminUserStatusUpdate
    ) -> dict:
        user = self.repo.get_user_for_update(user_id)
        if user is None:
            raise ResourceNotFoundError
        if user.is_active == payload.is_active:
            return self.get_user(user_id)
        before = self._user_snapshot(user)
        if payload.is_active:
            user.is_active = True
            user.disabled_at = None
            user.disabled_by_admin_id = None
        else:
            user.is_active = False
            user.disabled_at = datetime.now(timezone.utc)
            user.disabled_by_admin_id = admin_id
        self.repo.add_audit(
            admin_id=admin_id,
            action="user.status.update",
            entity_type="user",
            entity_id=user.id,
            changes={"before": before, "after": self._user_snapshot(user)},
        )
        self.db.commit()
        return self.get_user(user_id)

    @staticmethod
    def _model_read(
        model: AIModel, credential: ModelProviderCredential | None
    ) -> AIModel:
        # These response-only attributes are deliberately not persisted on the model table.
        # Pydantic reads them through ``from_attributes`` while the encrypted credential
        # remains isolated in ``model_provider_credentials``.
        model.credential_configured = credential is not None
        model.api_key_last_four = credential.api_key_last_four if credential else None
        model.protocol = "chat_completions" if model.kind == "text" else None
        return model

    def _get_model_credential(self, model_id: UUID) -> ModelProviderCredential | None:
        # Focused unit tests may replace the repository with a minimal fake that predates
        # model-level credentials. Production repositories always implement this method.
        getter = getattr(self.repo, "get_model_credential", None)
        return getter(model_id) if getter is not None else None

    def list_models(self) -> list[AIModel]:
        return [self._model_read(model, credential) for model, credential in self.repo.list_models()]

    def list_model_groups(self) -> list[dict]:
        return self.repo.list_model_groups()

    def get_model_group(self, group_id: UUID) -> ModelGroup:
        group = self.repo.get_model_group(group_id)
        if group is None:
            raise ResourceNotFoundError
        return group

    def _get_model_entity(self, model_id: UUID) -> AIModel:
        model = self.repo.get_model(model_id)
        if model is None:
            raise ResourceNotFoundError
        return model

    def get_model(self, model_id: UUID) -> AIModel:
        model = self._get_model_entity(model_id)
        return self._model_read(model, self._get_model_credential(model.id))

    def _commit_model_write(self) -> None:
        try:
            self.db.commit()
        except IntegrityError as error:
            self.db.rollback()
            if "uq_models_key" in str(error.orig) or "models_key_key" in str(error.orig):
                raise DuplicateModelKeyError from error
            raise

    def create_model(
        self, admin_id: UUID, payload: AdminModelCreate
    ) -> AIModel:
        self._require_writable_group(payload.group_id)
        metadata = normalize_model_metadata(
            kind=payload.kind,
            config_schema=payload.config_schema,
            default_parameters=payload.default_parameters,
            metadata=payload.metadata,
            matrix_explicitly_submitted=IMAGE_SIZE_MATRIX_KEY in payload.metadata,
        )
        metadata = normalize_image_quality_metadata(
            kind=payload.kind,
            config_schema=payload.config_schema,
            default_parameters=payload.default_parameters,
            metadata=metadata,
            explicitly_submitted=IMAGE_QUALITY_OPTIONS_KEY in payload.metadata,
        )
        metadata = normalize_video_options(
            kind=payload.kind,
            config_schema=payload.config_schema,
            default_parameters=payload.default_parameters,
            metadata=metadata,
            explicitly_submitted=VIDEO_OPTIONS_KEY in payload.metadata,
        )
        metadata, config_schema, default_parameters = extract_normalized_provider_config(
            metadata, payload.config_schema, payload.default_parameters
        )
        values = payload.model_dump(exclude={"metadata"})
        values["config_schema"] = config_schema
        values["default_parameters"] = default_parameters
        model = AIModel(
            id=uuid.uuid4(),
            key=str(uuid.uuid4()),
            **values,
            model_metadata=metadata,
            created_by_admin_id=admin_id,
        )
        self.db.add(model)
        self.repo.add_audit(
            admin_id=admin_id,
            action="model.create",
            entity_id=model.id,
            changes={"after": self._model_snapshot(model)},
        )
        self._commit_model_write()
        self.db.refresh(model)
        return self._model_read(model, self._get_model_credential(model.id))

    def create_text_model(self, admin_id: UUID, payload: AdminTextModelCreate) -> AIModel:
        self._require_active_group(payload.group_id)
        model = AIModel(
            id=uuid.uuid4(),
            key=str(uuid.uuid4()),
            group_id=payload.group_id,
            model_name=payload.model_name,
            base_url=payload.base_url,
            display_name=payload.model_name,
            kind="text",
            description="",
            status="active",
            display_order=0,
            config_schema={},
            default_parameters={},
            model_metadata={"protocol": "chat_completions"},
            created_by_admin_id=admin_id,
        )
        self.db.add(model)
        self.repo.add_audit(
            admin_id=admin_id,
            action="model.text.create",
            entity_id=model.id,
            changes={"after": self._model_snapshot(model)},
        )
        self._commit_model_write()
        self.db.refresh(model)
        return self._model_read(model, None)

    def update_text_model(
        self, admin_id: UUID, model_id: UUID, payload: AdminTextModelUpdate
    ) -> AIModel:
        model = self.repo.get_model_for_update(model_id)
        if model is None:
            raise ResourceNotFoundError
        if model.kind != "text":
            raise ModelKindMismatchError
        if model.status == "archived":
            raise ArchivedModelError
        self._require_active_group(payload.group_id)

        before = self._model_snapshot(model)
        model.group_id = payload.group_id
        model.base_url = payload.base_url
        model.model_name = payload.model_name
        model.display_name = payload.model_name
        metadata = dict(model.model_metadata or {})
        metadata["protocol"] = "chat_completions"
        model.model_metadata = metadata
        after = self._model_snapshot(model)
        self.repo.add_audit(
            admin_id=admin_id,
            action="model.text.update",
            entity_id=model.id,
            changes={"before": before, "after": after},
        )
        self._commit_model_write()
        self.db.refresh(model)
        return self._model_read(model, self._get_model_credential(model.id))

    def update_model(
        self, admin_id: UUID, model_id: UUID, payload: AdminModelUpdate
    ) -> AIModel:
        model = self._get_model_entity(model_id)
        if model.status == "archived":
            raise ArchivedModelError
        if "group_id" in payload.model_fields_set and payload.group_id is not None:
            self._require_writable_group(payload.group_id)
        before = self._model_snapshot(model)
        effective_kind = payload.kind if "kind" in payload.model_fields_set else model.kind
        effective_config_schema = (
            payload.config_schema
            if "config_schema" in payload.model_fields_set
            else model.config_schema
        )
        effective_default_parameters = (
            payload.default_parameters
            if "default_parameters" in payload.model_fields_set
            else model.default_parameters
        )
        effective_metadata = (
            payload.metadata
            if "metadata" in payload.model_fields_set
            else model.model_metadata
        )
        matrix_explicitly_submitted = (
            "metadata" in payload.model_fields_set
            and isinstance(payload.metadata, dict)
            and IMAGE_SIZE_MATRIX_KEY in payload.metadata
        )
        normalized_metadata = normalize_model_metadata(
            kind=effective_kind,
            config_schema=effective_config_schema,
            default_parameters=effective_default_parameters,
            metadata=effective_metadata,
            matrix_explicitly_submitted=matrix_explicitly_submitted,
        )
        normalized_metadata = normalize_image_quality_metadata(
            kind=effective_kind,
            config_schema=effective_config_schema,
            default_parameters=effective_default_parameters,
            metadata=normalized_metadata,
            explicitly_submitted=(
                "metadata" in payload.model_fields_set
                and isinstance(payload.metadata, dict)
                and IMAGE_QUALITY_OPTIONS_KEY in payload.metadata
            ),
        )
        normalized_metadata = normalize_video_options(
            kind=effective_kind,
            config_schema=effective_config_schema,
            default_parameters=effective_default_parameters,
            metadata=normalized_metadata,
            explicitly_submitted=(
                "metadata" in payload.model_fields_set
                and isinstance(payload.metadata, dict)
                and VIDEO_OPTIONS_KEY in payload.metadata
            ),
        )
        normalized_metadata, effective_config_schema, effective_default_parameters = extract_normalized_provider_config(
            normalized_metadata,
            effective_config_schema,
            effective_default_parameters,
        )
        for field in payload.model_fields_set:
            value = getattr(payload, field)
            setattr(model, "model_metadata" if field == "metadata" else field, value)
        model.model_metadata = normalized_metadata
        model.config_schema = effective_config_schema
        model.default_parameters = effective_default_parameters
        credential = self._get_model_credential(model.id)
        after = self._model_snapshot(model)
        self.repo.add_audit(
            admin_id=admin_id,
            action="model.update",
            entity_id=model.id,
            changes={"before": before, "after": after},
        )
        self._commit_model_write()
        self.db.refresh(model)
        return self._model_read(model, credential)

    def _require_writable_group(self, group_id: UUID) -> ModelGroup:
        group = self.repo.get_model_group_for_share(group_id)
        if group is None:
            raise ResourceNotFoundError
        if group.status == "archived":
            raise ArchivedModelError
        return group

    def _require_active_group(self, group_id: UUID) -> ModelGroup:
        group = self.repo.get_model_group_for_share(group_id)
        if group is None:
            raise ResourceNotFoundError
        if group.status != "active":
            raise ModelGroupNotActiveError
        return group

    def create_model_group(
        self, admin_id: UUID, payload: AdminModelGroupCreate
    ) -> ModelGroup:
        group = ModelGroup(
            id=uuid.uuid4(),
            **payload.model_dump(),
            created_by_admin_id=admin_id,
        )
        self.db.add(group)
        self.repo.add_audit(
            admin_id=admin_id,
            action="model_group.create",
            entity_type="model_group",
            entity_id=group.id,
            changes={"after": payload.model_dump(mode="json")},
        )
        self._commit_group_write()
        self.db.refresh(group)
        return group

    def update_model_group(
        self, admin_id: UUID, group_id: UUID, payload: AdminModelGroupUpdate
    ) -> ModelGroup:
        group = self.get_model_group(group_id)
        if group.status == "archived":
            raise ArchivedModelError
        before = self._group_snapshot(group)
        for field in payload.model_fields_set:
            setattr(group, field, getattr(payload, field))
        self.repo.add_audit(
            admin_id=admin_id,
            action="model_group.update",
            entity_type="model_group",
            entity_id=group.id,
            changes={"before": before, "after": self._group_snapshot(group)},
        )
        self._commit_group_write()
        self.db.refresh(group)
        return group

    def delete_model_group(self, admin_id: UUID, group_id: UUID) -> None:
        group = self.repo.get_model_group_for_update(group_id)
        if group is None:
            raise ResourceNotFoundError
        if self.repo.model_group_has_models(group.id):
            raise ModelGroupNotEmptyError
        before = self._group_snapshot(group)
        self.repo.add_audit(
            admin_id=admin_id,
            action="model_group.delete",
            entity_type="model_group",
            entity_id=group.id,
            changes={"before": before},
        )
        self.repo.delete_model_group(group)
        try:
            self.db.commit()
        except IntegrityError as error:
            self.db.rollback()
            raise ModelGroupNotEmptyError from error

    def _commit_group_write(self) -> None:
        try:
            self.db.commit()
        except IntegrityError as error:
            self.db.rollback()
            if "uq_model_groups_name_ci" in str(error.orig):
                raise DuplicateModelGroupNameError from error
            raise

    @staticmethod
    def _group_snapshot(group: ModelGroup) -> dict:
        return {field: getattr(group, field) for field in MODEL_GROUP_FIELDS}

    def delete_model(self, admin_id: UUID, model_id: UUID) -> None:
        model = self.repo.get_model_for_update(model_id)
        if model is None:
            raise ResourceNotFoundError
        if self.repo.model_has_active_generation(model.id):
            raise ModelInUseError
        before = self._model_snapshot(model)
        self.repo.add_audit(
            admin_id=admin_id,
            action="model.delete",
            entity_id=model.id,
            changes={"before": before},
        )
        self.repo.delete_model(model)
        self._commit_model_write()

    @staticmethod
    def _user_snapshot(user: User) -> dict:
        return {
            "id": user.id,
            "email": user.email,
            "is_active": user.is_active,
            "disabled_at": user.disabled_at,
            "disabled_by_admin_id": user.disabled_by_admin_id,
            "created_at": user.created_at,
        }

    @staticmethod
    def _model_snapshot(model: AIModel) -> dict:
        snapshot = {
            field: getattr(model, "model_metadata" if field == "metadata" else field)
            for field in MODEL_FIELDS
        }
        snapshot["group_id"] = str(snapshot["group_id"])
        return snapshot
