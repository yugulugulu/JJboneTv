"""Add administrator accounts, audit logs, and managed models."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision = "20260723_0003"
down_revision = "20260723_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "admin_accounts",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_admin_accounts_email_ci ON admin_accounts (lower(email))"
    )

    op.create_table(
        "models",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("key", sa.String(120), nullable=False),
        sa.Column("model_name", sa.String(160), nullable=False),
        sa.Column("base_url", sa.String(512), nullable=False),
        sa.Column("display_name", sa.String(160), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("description", sa.Text(), server_default="", nullable=False),
        sa.Column("status", sa.String(16), server_default="draft", nullable=False),
        sa.Column("display_order", sa.Integer(), server_default="0", nullable=False),
        sa.Column("config_schema", postgresql.JSONB(), server_default="{}", nullable=False),
        sa.Column("default_parameters", postgresql.JSONB(), server_default="{}", nullable=False),
        sa.Column("metadata", postgresql.JSONB(), server_default="{}", nullable=False),
        sa.Column(
            "created_by_admin_id",
            sa.UUID(),
            sa.ForeignKey("admin_accounts.id", ondelete="SET NULL"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("key", name="uq_models_key"),
        sa.CheckConstraint("kind IN ('text','image','video','audio')", name="ck_models_kind"),
        sa.CheckConstraint(
            "status IN ('draft','active','disabled','archived')", name="ck_models_status"
        ),
        sa.CheckConstraint("display_order >= 0", name="ck_models_display_order"),
    )
    op.create_index(
        "ix_models_public_order", "models", ["status", "display_order", "display_name"]
    )

    op.create_table(
        "admin_audit_logs",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column(
            "admin_account_id",
            sa.UUID(),
            sa.ForeignKey("admin_accounts.id", ondelete="SET NULL"),
        ),
        sa.Column("action", sa.String(80), nullable=False),
        sa.Column("entity_type", sa.String(80), nullable=False),
        sa.Column("entity_id", sa.UUID()),
        sa.Column("changes", postgresql.JSONB(), server_default="{}", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index(
        "ix_admin_audit_logs_entity",
        "admin_audit_logs",
        ["entity_type", "entity_id", "created_at"],
    )
    op.create_index(
        "ix_admin_audit_logs_admin_created",
        "admin_audit_logs",
        ["admin_account_id", "created_at"],
    )

    op.execute(
        """
        INSERT INTO models (
            id, key, model_name, base_url, display_name, kind, description,
            status, display_order, config_schema, default_parameters, metadata
        )
        VALUES
            (gen_random_uuid(), 'nano-image-2k', 'nano-image-2k', '', 'Nano Image 2K', 'image', '', 'active', 10, '{}'::jsonb, '{}'::jsonb, '{}'::jsonb),
            (gen_random_uuid(), 'flux-vision-pro', 'flux-vision-pro', '', 'Flux Vision Pro', 'image', '', 'active', 20, '{}'::jsonb, '{}'::jsonb, '{}'::jsonb),
            (gen_random_uuid(), 'sd2-xh', 'sd2-xh', '', 'SD2 XH', 'video', '', 'active', 30, '{}'::jsonb, '{}'::jsonb, '{}'::jsonb),
            (gen_random_uuid(), 'cinema-director', 'cinema-director', '', 'Cinema Director', 'video', '', 'active', 40, '{}'::jsonb, '{}'::jsonb, '{}'::jsonb),
            (gen_random_uuid(), 'portrait-studio', 'portrait-studio', '', 'Portrait Studio', 'image', '', 'active', 50, '{}'::jsonb, '{}'::jsonb, '{}'::jsonb)
        ON CONFLICT (key) DO NOTHING
        """
    )


def downgrade() -> None:
    op.drop_table("admin_audit_logs")
    op.drop_table("models")
    op.drop_table("admin_accounts")
