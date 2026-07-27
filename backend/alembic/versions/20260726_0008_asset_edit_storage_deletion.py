"""Add image edit idempotency and durable storage deletion jobs."""

import sqlalchemy as sa
from alembic import op


revision = "20260726_0008"
down_revision = "20260726_0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "asset_edit_requests",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("owner_id", sa.UUID(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("project_id", sa.UUID(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("node_id", sa.UUID(), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("source_asset_id", sa.UUID(), sa.ForeignKey("assets.id", ondelete="SET NULL")),
        sa.Column("output_asset_id", sa.UUID(), sa.ForeignKey("assets.id", ondelete="SET NULL")),
        sa.Column("project_revision", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint(
            "owner_id", "idempotency_key", name="uq_asset_edit_requests_owner_idempotency"
        ),
    )
    op.create_index(
        "ix_asset_edit_requests_project_created",
        "asset_edit_requests",
        ["project_id", "created_at"],
    )

    op.create_table(
        "storage_deletion_jobs",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("asset_id", sa.UUID(), sa.ForeignKey("assets.id", ondelete="SET NULL")),
        sa.Column("owner_id", sa.UUID(), nullable=False),
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("storage_backend", sa.String(16), nullable=False),
        sa.Column("storage_bucket", sa.String(255)),
        sa.Column("storage_environment", sa.String(32), nullable=False),
        sa.Column("storage_key", sa.String(512), nullable=False),
        sa.Column("reason", sa.String(32), nullable=False),
        sa.Column("idempotency_key", sa.String(200), nullable=False),
        sa.Column("status", sa.String(24), server_default="queued", nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("max_attempts", sa.Integer(), server_default="10", nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("lease_token", sa.UUID()),
        sa.Column("lease_owner", sa.String(160)),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed')",
            name="ck_storage_deletion_jobs_status",
        ),
        sa.CheckConstraint(
            "reason IN ('asset_deleted', 'project_purged', 'folder_purged', 'upload_compensation')",
            name="ck_storage_deletion_jobs_reason",
        ),
        sa.CheckConstraint("attempt_count >= 0", name="ck_storage_deletion_jobs_attempt_count"),
        sa.CheckConstraint("max_attempts >= 1", name="ck_storage_deletion_jobs_max_attempts"),
        sa.UniqueConstraint("idempotency_key", name="uq_storage_deletion_jobs_idempotency"),
    )
    op.create_index(
        "ix_storage_deletion_jobs_claim",
        "storage_deletion_jobs",
        ["status", "next_attempt_at", "created_at"],
    )
    op.create_index(
        "ix_storage_deletion_jobs_expired_lease",
        "storage_deletion_jobs",
        ["status", "lease_expires_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_storage_deletion_jobs_expired_lease", table_name="storage_deletion_jobs")
    op.drop_index("ix_storage_deletion_jobs_claim", table_name="storage_deletion_jobs")
    op.drop_table("storage_deletion_jobs")
    op.drop_index("ix_asset_edit_requests_project_created", table_name="asset_edit_requests")
    op.drop_table("asset_edit_requests")
