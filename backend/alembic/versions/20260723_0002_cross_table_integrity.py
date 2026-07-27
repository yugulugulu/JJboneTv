"""Enforce tenant and project consistency across redundant ownership columns."""
from alembic import op


revision = "20260723_0002"
down_revision = "20260723_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_unique_constraint("uq_folders_id_owner", "folders", ["id", "owner_id"])
    op.create_unique_constraint("uq_projects_id_owner", "projects", ["id", "owner_id"])
    op.create_foreign_key(
        "fk_projects_folder_owner",
        "projects",
        "folders",
        ["folder_id", "owner_id"],
        ["id", "owner_id"],
    )
    op.create_foreign_key(
        "fk_assets_project_owner",
        "assets",
        "projects",
        ["project_id", "owner_id"],
        ["id", "owner_id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_generations_project_owner",
        "generation_tasks",
        "projects",
        ["project_id", "owner_id"],
        ["id", "owner_id"],
        ondelete="CASCADE",
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION enforce_generation_output_project()
        RETURNS trigger AS $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1
                FROM generation_tasks task
                JOIN assets asset ON asset.id = NEW.asset_id
                WHERE task.id = NEW.generation_task_id
                  AND task.project_id = asset.project_id
                  AND task.owner_id = asset.owner_id
            ) THEN
                RAISE EXCEPTION 'generation output task and asset must share project and owner'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER trg_generation_outputs_project
        BEFORE INSERT OR UPDATE ON generation_outputs
        FOR EACH ROW EXECUTE FUNCTION enforce_generation_output_project();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_generation_outputs_project ON generation_outputs")
    op.execute("DROP FUNCTION IF EXISTS enforce_generation_output_project()")
    op.drop_constraint("fk_generations_project_owner", "generation_tasks", type_="foreignkey")
    op.drop_constraint("fk_assets_project_owner", "assets", type_="foreignkey")
    op.drop_constraint("fk_projects_folder_owner", "projects", type_="foreignkey")
    op.drop_constraint("uq_projects_id_owner", "projects", type_="unique")
    op.drop_constraint("uq_folders_id_owner", "folders", type_="unique")
