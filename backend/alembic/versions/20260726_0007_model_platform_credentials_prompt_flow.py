"""Add encrypted model-level provider credentials for platform text models."""

import sqlalchemy as sa
from alembic import op


revision = "20260726_0007"
down_revision = "20260726_0006"
branch_labels = None
depends_on = None


PLATFORM_TEXT_GROUP_ID = "00000000-0000-0000-0000-000000000002"


def upgrade() -> None:
    op.create_table(
        "model_provider_credentials",
        sa.Column(
            "model_id",
            sa.UUID(),
            sa.ForeignKey("models.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("encrypted_api_key", sa.LargeBinary(), nullable=False),
        sa.Column("api_key_last_four", sa.String(4), nullable=False),
        sa.Column(
            "created_by_admin_id",
            sa.UUID(),
            sa.ForeignKey("admin_accounts.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "updated_by_admin_id",
            sa.UUID(),
            sa.ForeignKey("admin_accounts.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.execute(
        f"""
        INSERT INTO model_groups (id, name, description, status, display_order)
        VALUES (
            '{PLATFORM_TEXT_GROUP_ID}'::uuid,
            '平台文本模型',
            '由管理员统一配置 Chat Completions API 的文本模型',
            'active',
            0
        )
        ON CONFLICT (id) DO NOTHING
        """
    )


def downgrade() -> None:
    op.drop_table("model_provider_credentials")
    op.execute(
        f"DELETE FROM model_groups WHERE id = '{PLATFORM_TEXT_GROUP_ID}'::uuid AND NOT EXISTS (SELECT 1 FROM models WHERE group_id = '{PLATFORM_TEXT_GROUP_ID}'::uuid)"
    )
