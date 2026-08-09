"""Add durable phases for concurrent image and video workers."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260808_0010"
down_revision = "20260727_0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "model_groups",
        sa.Column(
            "runtime_limits",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.add_column(
        "generation_tasks",
        sa.Column("execution_phase", sa.String(length=48), nullable=False, server_default="queued"),
    )
    op.add_column("generation_tasks", sa.Column("provider_submitted_at", sa.DateTime(timezone=True)))
    op.add_column(
        "generation_tasks", sa.Column("provider_slot_reserved_at", sa.DateTime(timezone=True))
    )
    op.add_column("generation_tasks", sa.Column("provider_deadline_at", sa.DateTime(timezone=True)))
    op.add_column("generation_tasks", sa.Column("next_poll_at", sa.DateTime(timezone=True)))
    op.add_column(
        "generation_tasks",
        sa.Column("poll_error_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("generation_tasks", sa.Column("last_provider_error", sa.Text()))
    op.add_column("generation_tasks", sa.Column("temporary_result_path", sa.String(length=1024)))
    op.create_index(
        "ix_generations_phase_poll",
        "generation_tasks",
        ["execution_phase", "next_poll_at", "created_at"],
    )
    op.execute(
        """
        UPDATE generation_tasks
        SET execution_phase = CASE
            WHEN status = 'succeeded' THEN 'succeeded'
            WHEN status IN ('failed', 'cancelled') THEN 'failed'
            WHEN kind = 'video' AND provider_task_id IS NOT NULL THEN 'video_provider_processing'
            ELSE 'queued'
        END,
        provider_submitted_at = CASE
            WHEN kind = 'video' AND provider_task_id IS NOT NULL
            THEN COALESCE(provider_request_started_at, updated_at, created_at)
            ELSE provider_submitted_at
        END,
        provider_deadline_at = CASE
            WHEN kind = 'video' AND provider_task_id IS NOT NULL
            THEN COALESCE(provider_request_started_at, updated_at, created_at) + interval '60 minutes'
            ELSE provider_deadline_at
        END,
        next_poll_at = CASE
            WHEN kind = 'video' AND provider_task_id IS NOT NULL THEN now()
            ELSE next_poll_at
        END
        """
    )
    op.execute(
        """
        UPDATE generation_tasks
        SET status = 'failed',
            execution_phase = 'provider_result_unknown',
            result = COALESCE(result, '{}'::jsonb)
                || '{"provider_outcome_unknown": true}'::jsonb,
            error = '旧视频任务缺少供应商任务 ID，供应商结果未知，请重新发起',
            completed_at = now(),
            lease_token = NULL,
            lease_owner = NULL,
            lease_expires_at = NULL
        WHERE kind = 'video'
          AND status = 'running'
          AND provider_task_id IS NULL
        """
    )


def downgrade() -> None:
    op.drop_index("ix_generations_phase_poll", table_name="generation_tasks")
    for column in (
        "temporary_result_path",
        "last_provider_error",
        "poll_error_count",
        "next_poll_at",
        "provider_deadline_at",
        "provider_submitted_at",
        "provider_slot_reserved_at",
        "execution_phase",
    ):
        op.drop_column("generation_tasks", column)
    op.drop_column("model_groups", "runtime_limits")
