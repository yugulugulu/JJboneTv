"""Add user account disable state and administrator attribution."""

import sqlalchemy as sa
from alembic import op


revision = "20260726_0006"
down_revision = "20260724_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
    )
    op.add_column(
        "users",
        sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "users",
        sa.Column("disabled_by_admin_id", sa.UUID(), nullable=True),
    )
    op.create_foreign_key(
        "fk_users_disabled_by_admin_id",
        "users",
        "admin_accounts",
        ["disabled_by_admin_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_users_disabled_by_admin_id", "users", type_="foreignkey")
    op.drop_column("users", "disabled_by_admin_id")
    op.drop_column("users", "disabled_at")
    op.drop_column("users", "is_active")
