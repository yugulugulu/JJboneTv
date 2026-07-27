import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base


EMPTY_WORKFLOW = {
    "schema_version": 1,
    "viewport": {"x": 0, "y": 0, "zoom": 1},
    "nodes": [],
    "edges": [],
}


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    disabled_by_admin_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("admin_accounts.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AuthRateLimitEvent(Base):
    __tablename__ = "auth_rate_limit_events"
    __table_args__ = (
        Index(
            "ix_auth_rate_limit_events_bucket_time",
            "scope",
            "bucket_key",
            text("occurred_at DESC"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    scope: Mapped[str] = mapped_column(String(32), nullable=False)
    bucket_key: Mapped[str] = mapped_column(String(64), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class AdminAccount(Base):
    __tablename__ = "admin_accounts"
    __table_args__ = (
        Index("uq_admin_accounts_email_ci", func.lower(text("email")), unique=True),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ModelGroup(Base):
    __tablename__ = "model_groups"
    __table_args__ = (
        CheckConstraint(
            "status IN ('draft', 'active', 'disabled', 'archived')",
            name="ck_model_groups_status",
        ),
        CheckConstraint("display_order >= 0", name="ck_model_groups_display_order"),
        Index("uq_model_groups_name_ci", func.lower(text("name")), unique=True),
        Index("ix_model_groups_public_order", "status", "display_order", "name"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="draft")
    display_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_by_admin_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("admin_accounts.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class AIModel(Base):
    __tablename__ = "models"
    __table_args__ = (
        CheckConstraint("kind IN ('text', 'image', 'video', 'audio')", name="ck_models_kind"),
        CheckConstraint(
            "status IN ('draft', 'active', 'disabled', 'archived')", name="ck_models_status"
        ),
        CheckConstraint("display_order >= 0", name="ck_models_display_order"),
        Index("ix_models_group_id", "group_id"),
        Index("ix_models_public_order", "status", "display_order", "display_name"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    group_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("model_groups.id", ondelete="RESTRICT"), nullable=False
    )
    key: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    model_name: Mapped[str] = mapped_column(String(160), nullable=False)
    base_url: Mapped[str] = mapped_column(String(512), nullable=False)
    display_name: Mapped[str] = mapped_column(String(160), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="draft")
    display_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    config_schema: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    default_parameters: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    model_metadata: Mapped[dict] = mapped_column("metadata", JSONB, nullable=False, default=dict)
    created_by_admin_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("admin_accounts.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class UserModelCredential(Base):
    __tablename__ = "user_model_credentials"
    __table_args__ = (
        UniqueConstraint("user_id", "group_id", name="uq_user_model_credentials_user_group"),
        Index("ix_user_model_credentials_group", "group_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    group_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("model_groups.id", ondelete="CASCADE"), nullable=False
    )
    encrypted_api_key: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    api_key_last_four: Mapped[str] = mapped_column(String(4), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ModelProviderCredential(Base):
    __tablename__ = "model_provider_credentials"

    model_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("models.id", ondelete="CASCADE"),
        primary_key=True,
    )
    encrypted_api_key: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    api_key_last_four: Mapped[str] = mapped_column(String(4), nullable=False)
    created_by_admin_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("admin_accounts.id", ondelete="SET NULL"), nullable=True
    )
    updated_by_admin_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("admin_accounts.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class AdminAuditLog(Base):
    __tablename__ = "admin_audit_logs"
    __table_args__ = (
        Index("ix_admin_audit_logs_entity", "entity_type", "entity_id", "created_at"),
        Index("ix_admin_audit_logs_admin_created", "admin_account_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    admin_account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("admin_accounts.id", ondelete="SET NULL"), nullable=True
    )
    action: Mapped[str] = mapped_column(String(80), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(80), nullable=False)
    entity_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    changes: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Folder(Base):
    __tablename__ = "folders"
    __table_args__ = (
        UniqueConstraint("id", "owner_id", name="uq_folders_id_owner"),
        Index(
            "uq_folders_owner_active_name_ci",
            "owner_id",
            func.lower(text("name")),
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    trash_batch_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)


class Project(Base):
    __tablename__ = "projects"
    __table_args__ = (
        UniqueConstraint("id", "owner_id", name="uq_projects_id_owner"),
        ForeignKeyConstraint(
            ["folder_id", "owner_id"],
            ["folders.id", "folders.owner_id"],
            name="fk_projects_folder_owner",
        ),
        CheckConstraint("project_type IN ('image', 'video', 'mixed')", name="ck_projects_type"),
        Index(
            "ix_projects_owner_folder_updated_active",
            "owner_id",
            "folder_id",
            text("updated_at DESC"),
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index(
            "ix_projects_owner_updated_active",
            "owner_id",
            text("updated_at DESC"),
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index("ix_projects_owner_deleted", "owner_id", "deleted_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    folder_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("folders.id", ondelete="SET NULL"), nullable=True
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    project_type: Mapped[str] = mapped_column(String(16), nullable=False, default="mixed")
    cover_asset_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "assets.id", ondelete="SET NULL", use_alter=True, name="fk_projects_cover_asset_id_assets"
        ),
        nullable=True,
    )
    workflow: Mapped[dict] = mapped_column(JSONB, nullable=False, default=lambda: dict(EMPTY_WORKFLOW))
    workflow_schema_version: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=1)
    current_revision: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    trash_batch_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)


class ProjectVersion(Base):
    __tablename__ = "project_versions"
    __table_args__ = (
        UniqueConstraint("project_id", "version_no", name="uq_project_versions_number"),
        CheckConstraint(
            "reason IN ('initial', 'interval', 'generation_completed', 'leave', 'manual', 'restore')",
            name="ck_project_versions_reason",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    version_no: Mapped[int] = mapped_column(BigInteger, nullable=False)
    schema_version: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)
    snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    reason: Mapped[str] = mapped_column(String(32), nullable=False)
    restored_from_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("project_versions.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Asset(Base):
    __tablename__ = "assets"
    __table_args__ = (
        ForeignKeyConstraint(
            ["project_id", "owner_id"],
            ["projects.id", "projects.owner_id"],
            name="fk_assets_project_owner",
            ondelete="CASCADE",
        ),
        CheckConstraint("kind IN ('text', 'image', 'video', 'audio')", name="ck_assets_kind"),
        CheckConstraint("source IN ('user_input', 'upload', 'generation')", name="ck_assets_source"),
        CheckConstraint(
            "status IN ('uploading', 'ready', 'failed', 'deleting')", name="ck_assets_status"
        ),
        CheckConstraint(
            "(kind = 'text' AND text_content IS NOT NULL AND storage_key IS NULL) OR "
            "(kind <> 'text' AND text_content IS NULL AND storage_key IS NOT NULL)",
            name="ck_assets_content_location",
        ),
        CheckConstraint("byte_size >= 0", name="ck_assets_byte_size"),
        CheckConstraint("width IS NULL OR width > 0", name="ck_assets_width"),
        CheckConstraint("height IS NULL OR height > 0", name="ck_assets_height"),
        CheckConstraint("duration_ms IS NULL OR duration_ms >= 0", name="ck_assets_duration"),
        UniqueConstraint("storage_key", name="uq_assets_storage_key"),
        Index("ix_assets_project_created", "project_id", text("created_at DESC")),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    source: Mapped[str] = mapped_column(String(24), nullable=False)
    text_content: Mapped[str | None] = mapped_column(Text, nullable=True)
    original_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    storage_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    mime_type: Mapped[str] = mapped_column(String(120), nullable=False)
    byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    checksum_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="ready")
    asset_metadata: Mapped[dict] = mapped_column("metadata", JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AssetEditRequest(Base):
    __tablename__ = "asset_edit_requests"
    __table_args__ = (
        UniqueConstraint(
            "owner_id", "idempotency_key", name="uq_asset_edit_requests_owner_idempotency"
        ),
        Index("ix_asset_edit_requests_project_created", "project_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    node_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    source_asset_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("assets.id", ondelete="SET NULL"), nullable=True
    )
    output_asset_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("assets.id", ondelete="SET NULL"), nullable=True
    )
    project_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class StorageDeletionJob(Base):
    __tablename__ = "storage_deletion_jobs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed')",
            name="ck_storage_deletion_jobs_status",
        ),
        CheckConstraint(
            "reason IN ('asset_deleted', 'project_purged', 'folder_purged', 'upload_compensation')",
            name="ck_storage_deletion_jobs_reason",
        ),
        CheckConstraint("attempt_count >= 0", name="ck_storage_deletion_jobs_attempt_count"),
        CheckConstraint("max_attempts >= 1", name="ck_storage_deletion_jobs_max_attempts"),
        UniqueConstraint("idempotency_key", name="uq_storage_deletion_jobs_idempotency"),
        Index(
            "ix_storage_deletion_jobs_claim", "status", "next_attempt_at", "created_at"
        ),
        Index(
            "ix_storage_deletion_jobs_expired_lease", "status", "lease_expires_at"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    asset_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("assets.id", ondelete="SET NULL"), nullable=True
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    storage_backend: Mapped[str] = mapped_column(String(16), nullable=False)
    storage_bucket: Mapped[str | None] = mapped_column(String(255), nullable=True)
    storage_environment: Mapped[str] = mapped_column(String(32), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(512), nullable=False)
    reason: Mapped[str] = mapped_column(String(32), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="queued")
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    lease_token: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    lease_owner: Mapped[str | None] = mapped_column(String(160), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class GenerationTask(Base):
    __tablename__ = "generation_tasks"
    __table_args__ = (
        ForeignKeyConstraint(
            ["project_id", "owner_id"],
            ["projects.id", "projects.owner_id"],
            name="fk_generations_project_owner",
            ondelete="CASCADE",
        ),
        CheckConstraint("kind IN ('text', 'image', 'video', 'audio')", name="ck_generations_kind"),
        CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')",
            name="ck_generations_status",
        ),
        Index("ix_generations_project_node_created", "project_id", "node_id", text("created_at DESC")),
        Index("ix_generations_claim", "status", "next_attempt_at", "created_at"),
        Index("ix_generations_expired_lease", "status", "lease_expires_at"),
        UniqueConstraint("owner_id", "idempotency_key", name="uq_generations_owner_idempotency"),
        CheckConstraint("attempt_count >= 0", name="ck_generations_attempt_count"),
        CheckConstraint("max_attempts >= 1", name="ck_generations_max_attempts"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    node_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    input_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    model: Mapped[str] = mapped_column(String(80), nullable=False)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    parameters: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="queued")
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    lease_token: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    lease_owner: Mapped[str | None] = mapped_column(String(160), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    provider_request_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    provider_task_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    result: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class GenerationOutput(Base):
    __tablename__ = "generation_outputs"
    __table_args__ = (
        UniqueConstraint("generation_task_id", "asset_id", name="uq_generation_outputs_asset"),
        CheckConstraint("ordinal >= 0", name="ck_generation_outputs_ordinal"),
    )

    generation_task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("generation_tasks.id", ondelete="CASCADE"),
        primary_key=True,
    )
    ordinal: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    asset_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("assets.id", ondelete="CASCADE"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
