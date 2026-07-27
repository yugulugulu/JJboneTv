"""Add model groups and per-user encrypted provider credentials."""

import sqlalchemy as sa
from alembic import op


revision = "20260723_0004"
down_revision = "20260723_0003"
branch_labels = None
depends_on = None


LEGACY_GROUP_ID = "00000000-0000-0000-0000-000000000001"


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
    op.create_table(
        "model_groups",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("description", sa.Text(), server_default="", nullable=False),
        sa.Column("status", sa.String(16), server_default="active", nullable=False),
        sa.Column("display_order", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "created_by_admin_id",
            sa.UUID(),
            sa.ForeignKey("admin_accounts.id", ondelete="SET NULL"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "status IN ('draft','active','disabled','archived')",
            name="ck_model_groups_status",
        ),
        sa.CheckConstraint("display_order >= 0", name="ck_model_groups_display_order"),
    )
    op.execute("CREATE UNIQUE INDEX uq_model_groups_name_ci ON model_groups (lower(name))")
    op.create_index(
        "ix_model_groups_public_order",
        "model_groups",
        ["status", "display_order", "name"],
    )
    op.execute(
        f"""
        INSERT INTO model_groups (id, name, description, status, display_order)
        VALUES ('{LEGACY_GROUP_ID}'::uuid, '默认模型服务', '迁移前模型的默认分组', 'active', 0)
        ON CONFLICT (id) DO NOTHING
        """
    )

    op.add_column("models", sa.Column("group_id", sa.UUID(), nullable=True))
    op.execute(
        f"UPDATE models SET group_id = '{LEGACY_GROUP_ID}'::uuid WHERE group_id IS NULL"
    )
    op.alter_column("models", "group_id", nullable=False)
    op.create_foreign_key(
        "fk_models_group_id_model_groups",
        "models",
        "model_groups",
        ["group_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index("ix_models_group_id", "models", ["group_id"])

    op.create_table(
        "user_model_credentials",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column(
            "user_id",
            sa.UUID(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "group_id",
            sa.UUID(),
            sa.ForeignKey("model_groups.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("encrypted_api_key", sa.LargeBinary(), nullable=False),
        sa.Column("api_key_last_four", sa.String(4), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("user_id", "group_id", name="uq_user_model_credentials_user_group"),
    )
    op.create_index(
        "ix_user_model_credentials_group", "user_model_credentials", ["group_id"]
    )


def downgrade() -> None:
    op.drop_table("user_model_credentials")
    op.drop_index("ix_models_group_id", table_name="models")
    op.drop_constraint("fk_models_group_id_model_groups", "models", type_="foreignkey")
    op.drop_column("models", "group_id")
    op.drop_table("model_groups")
