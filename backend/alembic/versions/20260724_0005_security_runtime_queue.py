"""Add auth rate limiting and durable generation queue fields."""

import sqlalchemy as sa
from alembic import op


revision = "20260724_0005"
down_revision = "20260723_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "auth_rate_limit_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("scope", sa.String(32), nullable=False),
        sa.Column("bucket_key", sa.String(64), nullable=False),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_auth_rate_limit_events_bucket_time",
        "auth_rate_limit_events",
        ["scope", "bucket_key", sa.text("occurred_at DESC")],
    )

    op.add_column("generation_tasks", sa.Column("idempotency_key", sa.String(200)))
    op.add_column("generation_tasks", sa.Column("request_hash", sa.String(64)))
    op.add_column(
        "generation_tasks",
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "generation_tasks",
        sa.Column("max_attempts", sa.Integer(), server_default="3", nullable=False),
    )
    op.add_column(
        "generation_tasks",
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.add_column("generation_tasks", sa.Column("lease_token", sa.UUID()))
    op.add_column("generation_tasks", sa.Column("lease_owner", sa.String(160)))
    op.add_column("generation_tasks", sa.Column("lease_expires_at", sa.DateTime(timezone=True)))
    op.add_column(
        "generation_tasks",
        sa.Column("provider_request_started_at", sa.DateTime(timezone=True)),
    )
    # Execute as driver SQL so PostgreSQL JSON text such as `:true` is not
    # interpreted by SQLAlchemy's text parser as a named bind parameter.
    op.get_bind().exec_driver_sql(
        """
        UPDATE generation_tasks
        SET idempotency_key = 'legacy:' || id::text,
            request_hash = encode(digest(id::text || ':' || created_at::text, 'sha256'), 'hex'),
            next_attempt_at = COALESCE(created_at, now()),
            status = CASE WHEN status = 'running' THEN 'failed' ELSE status END,
            error = CASE
                WHEN status = 'running' THEN '升级期间发现未完成任务，供应商结果未知，请重新发起'
                ELSE error
            END,
            result = CASE
                WHEN status = 'running' THEN COALESCE(result, '{}'::jsonb) || '{"provider_outcome_unknown":true}'::jsonb
                ELSE result
            END,
            completed_at = CASE WHEN status = 'running' THEN now() ELSE completed_at END
        """
    )
    op.alter_column("generation_tasks", "idempotency_key", nullable=False)
    op.alter_column("generation_tasks", "request_hash", nullable=False)
    op.alter_column("generation_tasks", "next_attempt_at", nullable=False)
    op.create_unique_constraint(
        "uq_generations_owner_idempotency",
        "generation_tasks",
        ["owner_id", "idempotency_key"],
    )
    op.create_check_constraint(
        "ck_generations_attempt_count", "generation_tasks", "attempt_count >= 0"
    )
    op.create_check_constraint(
        "ck_generations_max_attempts", "generation_tasks", "max_attempts >= 1"
    )
    op.create_index(
        "ix_generations_claim",
        "generation_tasks",
        ["status", "next_attempt_at", "created_at"],
    )
    op.create_index(
        "ix_generations_expired_lease",
        "generation_tasks",
        ["status", "lease_expires_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_generations_expired_lease", table_name="generation_tasks")
    op.drop_index("ix_generations_claim", table_name="generation_tasks")
    op.drop_constraint("ck_generations_max_attempts", "generation_tasks", type_="check")
    op.drop_constraint("ck_generations_attempt_count", "generation_tasks", type_="check")
    op.drop_constraint("uq_generations_owner_idempotency", "generation_tasks", type_="unique")
    for column in (
        "provider_request_started_at",
        "lease_expires_at",
        "lease_owner",
        "lease_token",
        "next_attempt_at",
        "max_attempts",
        "attempt_count",
        "request_hash",
        "idempotency_key",
    ):
        op.drop_column("generation_tasks", column)
    op.drop_index("ix_auth_rate_limit_events_bucket_time", table_name="auth_rate_limit_events")
    op.drop_table("auth_rate_limit_events")
