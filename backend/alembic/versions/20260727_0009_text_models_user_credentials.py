"""Use user-owned group credentials for text models."""

from alembic import op


revision = "20260727_0009"
down_revision = "20260726_0008"
branch_labels = None
depends_on = None


PLATFORM_TEXT_GROUP_ID = "00000000-0000-0000-0000-000000000002"


def upgrade() -> None:
    op.execute(
        f"""
        INSERT INTO model_groups (id, name, description, status, display_order)
        VALUES (
            '{PLATFORM_TEXT_GROUP_ID}'::uuid,
            '文本模型服务',
            '由管理员维护模型目录，用户在模型设置中配置自己的 API Key',
            'active',
            0
        )
        ON CONFLICT (id) DO UPDATE
        SET name = EXCLUDED.name,
            description = EXCLUDED.description,
            status = 'active',
            updated_at = now()
        """
    )


def downgrade() -> None:
    op.execute(
        f"""
        UPDATE model_groups
        SET description = '由管理员统一配置 Chat Completions API 的文本模型',
            updated_at = now()
        WHERE id = '{PLATFORM_TEXT_GROUP_ID}'::uuid
        """
    )
