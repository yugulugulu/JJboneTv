"""Add owned projects, folders, versions, assets, and generation outputs.

This revision can upgrade both a new database and the pre-Alembic ThinkTV schema.
Legacy rows require explicit environment-provided ownership mapping.
"""
import os
from uuid import UUID

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect, text
from sqlalchemy.dialects import postgresql


revision = "20260723_0001"
down_revision = None
branch_labels = None
depends_on = None


def _columns(inspector, table: str) -> set[str]:
    return {column["name"] for column in inspector.get_columns(table)}


def _legacy_owner(connection, table: str) -> str | None:
    count = connection.scalar(text(f"SELECT count(*) FROM {table}"))
    if not count:
        return None
    raw = os.getenv("THINKTV_LEGACY_OWNER_ID")
    if not raw:
        raise RuntimeError(
            f"{table} contains legacy rows. Set THINKTV_LEGACY_OWNER_ID to an existing user UUID."
        )
    owner_id = str(UUID(raw))
    exists = connection.scalar(text("SELECT EXISTS (SELECT 1 FROM users WHERE id=:id)"), {"id": owner_id})
    if not exists:
        raise RuntimeError("THINKTV_LEGACY_OWNER_ID does not identify an existing user")
    return owner_id


def upgrade() -> None:
    connection = op.get_bind()
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
    inspector = inspect(connection)
    tables = set(inspector.get_table_names())

    if "users" not in tables:
        op.create_table(
            "users",
            sa.Column("id", sa.UUID(), primary_key=True),
            sa.Column("email", sa.String(320), nullable=False),
            sa.Column("password_hash", sa.String(255), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.UniqueConstraint("email", name="uq_users_email"),
        )

    if "folders" not in tables:
        op.create_table(
            "folders",
            sa.Column("id", sa.UUID(), primary_key=True),
            sa.Column("owner_id", sa.UUID(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("name", sa.String(120), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("deleted_at", sa.DateTime(timezone=True)),
            sa.Column("trash_batch_id", sa.UUID()),
        )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_folders_owner_active_name_ci "
        "ON folders (owner_id, lower(name)) WHERE deleted_at IS NULL"
    )

    if "projects" not in tables:
        op.create_table(
            "projects",
            sa.Column("id", sa.UUID(), primary_key=True),
            sa.Column("owner_id", sa.UUID(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("folder_id", sa.UUID(), sa.ForeignKey("folders.id", ondelete="SET NULL")),
            sa.Column("name", sa.String(120), nullable=False),
            sa.Column("project_type", sa.String(16), server_default="mixed", nullable=False),
            sa.Column("workflow", postgresql.JSONB(), server_default="{}", nullable=False),
            sa.Column("workflow_schema_version", sa.SmallInteger(), server_default="1", nullable=False),
            sa.Column("current_revision", sa.BigInteger(), server_default="0", nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("deleted_at", sa.DateTime(timezone=True)),
            sa.Column("trash_batch_id", sa.UUID()),
            sa.CheckConstraint("project_type IN ('image', 'video', 'mixed')", name="ck_projects_type"),
        )
    else:
        cols = _columns(inspect(connection), "projects")
        legacy_owner = _legacy_owner(connection, "projects")
        additions = [
            ("owner_id", sa.UUID(), True),
            ("folder_id", sa.UUID(), True),
            ("project_type", sa.String(16), True),
            ("workflow_schema_version", sa.SmallInteger(), True),
            ("current_revision", sa.BigInteger(), True),
            ("deleted_at", sa.DateTime(timezone=True), True),
            ("trash_batch_id", sa.UUID(), True),
        ]
        for name, type_, nullable in additions:
            if name not in cols:
                op.add_column("projects", sa.Column(name, type_, nullable=nullable))
        if legacy_owner:
            connection.execute(text("UPDATE projects SET owner_id=:owner WHERE owner_id IS NULL"), {"owner": legacy_owner})
        op.execute("UPDATE projects SET project_type='mixed' WHERE project_type IS NULL")
        connection.exec_driver_sql(
            "UPDATE projects SET workflow='{" 
            "\"schema_version\":1,\"viewport\":{\"x\":0,\"y\":0,\"zoom\":1},"
            "\"nodes\":[],\"edges\":[]}'::jsonb WHERE workflow='{}'::jsonb"
        )
        op.execute("UPDATE projects SET workflow_schema_version=1 WHERE workflow_schema_version IS NULL")
        op.execute("UPDATE projects SET current_revision=0 WHERE current_revision IS NULL")
        op.alter_column("projects", "owner_id", nullable=False)
        op.alter_column("projects", "project_type", nullable=False, server_default="mixed")
        op.alter_column("projects", "workflow_schema_version", nullable=False, server_default="1")
        op.alter_column("projects", "current_revision", nullable=False, server_default="0")
        op.create_foreign_key("fk_projects_owner_id_users", "projects", "users", ["owner_id"], ["id"], ondelete="CASCADE")
        op.create_foreign_key("fk_projects_folder_id_folders", "projects", "folders", ["folder_id"], ["id"], ondelete="SET NULL")
        op.create_check_constraint("ck_projects_type", "projects", "project_type IN ('image', 'video', 'mixed')")

    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_projects_owner_folder_updated_active "
        "ON projects (owner_id, folder_id, updated_at DESC) WHERE deleted_at IS NULL"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_projects_owner_updated_active "
        "ON projects (owner_id, updated_at DESC) WHERE deleted_at IS NULL"
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_projects_owner_deleted ON projects (owner_id, deleted_at)")

    if "project_versions" not in tables:
        op.create_table(
            "project_versions",
            sa.Column("id", sa.UUID(), primary_key=True),
            sa.Column("project_id", sa.UUID(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
            sa.Column("created_by_user_id", sa.UUID(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("version_no", sa.BigInteger(), nullable=False),
            sa.Column("schema_version", sa.SmallInteger(), nullable=False),
            sa.Column("snapshot", postgresql.JSONB(), nullable=False),
            sa.Column("snapshot_hash", sa.String(64), nullable=False),
            sa.Column("reason", sa.String(32), nullable=False),
            sa.Column("restored_from_version_id", sa.UUID()),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.UniqueConstraint("project_id", "version_no", name="uq_project_versions_number"),
            sa.CheckConstraint(
                "reason IN ('initial','interval','generation_completed','leave','manual','restore')",
                name="ck_project_versions_reason",
            ),
        )
        op.create_foreign_key(
            "fk_project_versions_restored_from",
            "project_versions",
            "project_versions",
            ["restored_from_version_id"],
            ["id"],
            ondelete="SET NULL",
        )
        op.create_index("ix_project_versions_project_id", "project_versions", ["project_id"])

    if "assets" not in tables:
        op.create_table(
            "assets",
            sa.Column("id", sa.UUID(), primary_key=True),
            sa.Column("owner_id", sa.UUID(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("project_id", sa.UUID(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
            sa.Column("kind", sa.String(16), nullable=False),
            sa.Column("source", sa.String(24), nullable=False),
            sa.Column("text_content", sa.Text()),
            sa.Column("original_name", sa.String(255)),
            sa.Column("storage_key", sa.String(512)),
            sa.Column("mime_type", sa.String(120), nullable=False),
            sa.Column("byte_size", sa.BigInteger(), nullable=False),
            sa.Column("width", sa.Integer()),
            sa.Column("height", sa.Integer()),
            sa.Column("duration_ms", sa.BigInteger()),
            sa.Column("checksum_sha256", sa.String(64)),
            sa.Column("status", sa.String(24), nullable=False),
            sa.Column("metadata", postgresql.JSONB(), server_default="{}", nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("deleted_at", sa.DateTime(timezone=True)),
            sa.UniqueConstraint("storage_key", name="uq_assets_storage_key"),
            sa.CheckConstraint("kind IN ('text','image','video','audio')", name="ck_assets_kind"),
            sa.CheckConstraint("source IN ('user_input','upload','generation')", name="ck_assets_source"),
            sa.CheckConstraint("status IN ('uploading','ready','failed','deleting')", name="ck_assets_status"),
            sa.CheckConstraint(
                "(kind='text' AND text_content IS NOT NULL AND storage_key IS NULL) OR "
                "(kind<>'text' AND text_content IS NULL AND storage_key IS NOT NULL)",
                name="ck_assets_content_location",
            ),
            sa.CheckConstraint("byte_size >= 0", name="ck_assets_byte_size"),
            sa.CheckConstraint("width IS NULL OR width > 0", name="ck_assets_width"),
            sa.CheckConstraint("height IS NULL OR height > 0", name="ck_assets_height"),
            sa.CheckConstraint("duration_ms IS NULL OR duration_ms >= 0", name="ck_assets_duration"),
        )
        op.create_index("ix_assets_project_created", "assets", ["project_id", sa.text("created_at DESC")])

    project_cols = _columns(inspect(connection), "projects")
    if "cover_asset_id" not in project_cols:
        op.add_column("projects", sa.Column("cover_asset_id", sa.UUID()))
        op.create_foreign_key(
            "fk_projects_cover_asset_id_assets",
            "projects",
            "assets",
            ["cover_asset_id"],
            ["id"],
            ondelete="SET NULL",
        )

    inspector = inspect(connection)
    tables = set(inspector.get_table_names())
    if "generation_tasks" not in tables:
        op.create_table(
            "generation_tasks",
            sa.Column("id", sa.UUID(), primary_key=True),
            sa.Column("owner_id", sa.UUID(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("project_id", sa.UUID(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
            sa.Column("node_id", sa.UUID(), nullable=False),
            sa.Column("input_revision", sa.BigInteger(), nullable=False),
            sa.Column("kind", sa.String(16), nullable=False),
            sa.Column("model", sa.String(80), nullable=False),
            sa.Column("prompt", sa.Text(), nullable=False),
            sa.Column("parameters", postgresql.JSONB(), server_default="{}", nullable=False),
            sa.Column("status", sa.String(24), nullable=False),
            sa.Column("provider_task_id", sa.String(160)),
            sa.Column("result", sa.dialects.postgresql.JSONB(), server_default="{}", nullable=False),
            sa.Column("error", sa.Text()),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("completed_at", sa.DateTime(timezone=True)),
            sa.CheckConstraint("kind IN ('text','image','video','audio')", name="ck_generations_kind"),
            sa.CheckConstraint(
                "status IN ('queued','running','succeeded','failed','cancelled')",
                name="ck_generations_status",
            ),
        )
    else:
        cols = _columns(inspect(connection), "generation_tasks")
        legacy_owner = _legacy_owner(connection, "generation_tasks")
        legacy_project = os.getenv("THINKTV_LEGACY_PROJECT_ID")
        if legacy_owner and not legacy_project:
            raise RuntimeError("Legacy generation tasks require THINKTV_LEGACY_PROJECT_ID")
        additions = [
            ("owner_id", sa.UUID()),
            ("project_id", sa.UUID()),
            ("node_id", sa.UUID()),
            ("input_revision", sa.BigInteger()),
            ("parameters", postgresql.JSONB()),
            ("completed_at", sa.DateTime(timezone=True)),
        ]
        for name, type_ in additions:
            if name not in cols:
                op.add_column("generation_tasks", sa.Column(name, type_, nullable=True))
        if legacy_owner:
            project_id = str(UUID(legacy_project))
            project_ok = connection.scalar(
                text("SELECT EXISTS (SELECT 1 FROM projects WHERE id=:id AND owner_id=:owner)"),
                {"id": project_id, "owner": legacy_owner},
            )
            if not project_ok:
                raise RuntimeError("THINKTV_LEGACY_PROJECT_ID is not owned by THINKTV_LEGACY_OWNER_ID")
            connection.execute(
                text(
                    "UPDATE generation_tasks SET owner_id=:owner, project_id=:project, "
                    "node_id=gen_random_uuid(), input_revision=0 WHERE owner_id IS NULL"
                ),
                {"owner": legacy_owner, "project": project_id},
            )
        op.execute("UPDATE generation_tasks SET parameters='{}'::jsonb WHERE parameters IS NULL")
        for column in ["owner_id", "project_id", "node_id", "input_revision", "parameters"]:
            op.alter_column("generation_tasks", column, nullable=False)
        op.create_foreign_key("fk_generations_owner_id_users", "generation_tasks", "users", ["owner_id"], ["id"], ondelete="CASCADE")
        op.create_foreign_key("fk_generations_project_id_projects", "generation_tasks", "projects", ["project_id"], ["id"], ondelete="CASCADE")
        op.create_check_constraint("ck_generations_kind", "generation_tasks", "kind IN ('text','image','video','audio')")
        op.create_check_constraint("ck_generations_status", "generation_tasks", "status IN ('queued','running','succeeded','failed','cancelled')")

    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_generations_project_node_created "
        "ON generation_tasks (project_id, node_id, created_at DESC)"
    )
    if "generation_outputs" not in tables:
        op.create_table(
            "generation_outputs",
            sa.Column("generation_task_id", sa.UUID(), sa.ForeignKey("generation_tasks.id", ondelete="CASCADE"), primary_key=True),
            sa.Column("ordinal", sa.SmallInteger(), primary_key=True),
            sa.Column("asset_id", sa.UUID(), sa.ForeignKey("assets.id", ondelete="CASCADE"), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.UniqueConstraint("generation_task_id", "asset_id", name="uq_generation_outputs_asset"),
            sa.CheckConstraint("ordinal >= 0", name="ck_generation_outputs_ordinal"),
        )


def downgrade() -> None:
    raise RuntimeError(
        "This migration consolidates both fresh and legacy schemas and is intentionally irreversible. "
        "Restore a database backup to roll back."
    )
