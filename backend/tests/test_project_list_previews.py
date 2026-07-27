from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import delete, event, select

from app.database import SessionLocal, engine
from app.main import app
from app.models import Asset, Project, User
from app.repositories.project_domain import ProjectDomainRepository


def register(client: TestClient, prefix: str) -> str:
    email = f"{prefix}-{uuid4()}@example.com"
    response = client.post(
        "/api/auth/register", json={"email": email, "password": "Strong123"}
    )
    assert response.status_code == 201, response.text
    return email


def create_project(client: TestClient, name: str) -> UUID:
    response = client.post("/api/projects", json={"name": name})
    assert response.status_code == 201, response.text
    assert "preview_asset_id" not in response.json()
    return UUID(response.json()["id"])


def image_asset(
    *,
    owner_id: UUID,
    project_id: UUID,
    created_at: datetime,
    asset_id: UUID | None = None,
    status: str = "ready",
    storage_key: str | None = None,
    deleted_at: datetime | None = None,
) -> Asset:
    return Asset(
        id=asset_id or uuid4(),
        owner_id=owner_id,
        project_id=project_id,
        kind="image",
        source="generation",
        storage_key=storage_key if storage_key is not None else f"tests/{uuid4()}.png",
        mime_type="image/png",
        byte_size=128,
        status=status,
        created_at=created_at,
        deleted_at=deleted_at,
    )


def test_project_list_selects_first_eligible_image_and_preserves_cover():
    owner = TestClient(app)
    other = TestClient(app)
    owner_email = register(owner, "preview-owner")
    other_email = register(other, "preview-other")

    try:
        first_project_id = create_project(owner, "First preview")
        empty_project_id = create_project(owner, "No preview")
        third_project_id = create_project(owner, "Third preview")
        other_project_id = create_project(other, "Other user's preview")
        base_time = datetime(2026, 7, 24, 8, 0, tzinfo=timezone.utc)
        tie_ids = sorted((uuid4(), uuid4()))

        with SessionLocal.begin() as db:
            owner_user = db.scalar(select(User).where(User.email == owner_email))
            other_user = db.scalar(select(User).where(User.email == other_email))
            assert owner_user is not None
            assert other_user is not None

            db.add_all(
                [
                    Asset(
                        owner_id=owner_user.id,
                        project_id=first_project_id,
                        kind="text",
                        source="user_input",
                        text_content="not an image",
                        mime_type="text/plain",
                        byte_size=12,
                        status="ready",
                        created_at=base_time - timedelta(minutes=6),
                    ),
                    Asset(
                        owner_id=owner_user.id,
                        project_id=first_project_id,
                        kind="video",
                        source="generation",
                        storage_key=f"tests/{uuid4()}.mp4",
                        mime_type="video/mp4",
                        byte_size=128,
                        status="ready",
                        created_at=base_time - timedelta(minutes=5),
                    ),
                    image_asset(
                        owner_id=owner_user.id,
                        project_id=first_project_id,
                        status="uploading",
                        created_at=base_time - timedelta(minutes=4),
                    ),
                    image_asset(
                        owner_id=owner_user.id,
                        project_id=first_project_id,
                        storage_key="   ",
                        created_at=base_time - timedelta(minutes=3),
                    ),
                    image_asset(
                        owner_id=owner_user.id,
                        project_id=first_project_id,
                        deleted_at=base_time,
                        created_at=base_time - timedelta(minutes=2),
                    ),
                    image_asset(
                        owner_id=owner_user.id,
                        project_id=first_project_id,
                        asset_id=tie_ids[1],
                        created_at=base_time,
                    ),
                    image_asset(
                        owner_id=owner_user.id,
                        project_id=first_project_id,
                        asset_id=tie_ids[0],
                        created_at=base_time,
                    ),
                    image_asset(
                        owner_id=owner_user.id,
                        project_id=empty_project_id,
                        status="failed",
                        created_at=base_time,
                    ),
                    image_asset(
                        owner_id=owner_user.id,
                        project_id=third_project_id,
                        created_at=base_time + timedelta(minutes=1),
                    ),
                    image_asset(
                        owner_id=other_user.id,
                        project_id=other_project_id,
                        created_at=base_time - timedelta(days=1),
                    ),
                ]
            )
            first_project = db.get(Project, first_project_id)
            assert first_project is not None
            first_project.cover_asset_id = tie_ids[1]

        response = owner.get("/api/projects")
        assert response.status_code == 200, response.text
        projects = {UUID(item["id"]): item for item in response.json()["items"]}

        assert UUID(projects[first_project_id]["preview_asset_id"]) == tie_ids[0]
        assert UUID(projects[first_project_id]["cover_asset_id"]) == tie_ids[1]
        assert projects[empty_project_id]["preview_asset_id"] is None
        assert projects[third_project_id]["preview_asset_id"] is not None
        assert other_project_id not in projects
    finally:
        with SessionLocal.begin() as db:
            users = list(
                db.scalars(select(User).where(User.email.in_([owner_email, other_email])))
            )
            for user in users:
                db.delete(user)


def test_project_list_batches_previews_after_limit_truncation(monkeypatch):
    client = TestClient(app)
    email = register(client, "preview-page")
    asset_selects: list[str] = []
    preview_calls: list[list[UUID]] = []
    original_preview_query = ProjectDomainRepository.first_ready_image_asset_ids

    def capture_preview_query(
        repository: ProjectDomainRepository, owner_id: UUID, project_ids: list[UUID]
    ) -> dict[UUID, UUID]:
        preview_calls.append(list(project_ids))
        return original_preview_query(repository, owner_id, project_ids)

    def capture_sql(_connection, _cursor, statement, _parameters, _context, _executemany):
        normalized = statement.lower()
        if normalized.lstrip().startswith("select") and "assets." in normalized:
            asset_selects.append(statement)

    monkeypatch.setattr(
        ProjectDomainRepository,
        "first_ready_image_asset_ids",
        capture_preview_query,
    )

    try:
        project_ids = [create_project(client, f"Paged {index}") for index in range(6)]
        base_time = datetime(2026, 7, 24, 9, 0, tzinfo=timezone.utc)
        with SessionLocal.begin() as db:
            user = db.scalar(select(User).where(User.email == email))
            assert user is not None
            for index, project_id in enumerate(project_ids):
                project = db.get(Project, project_id)
                assert project is not None
                project.updated_at = base_time + timedelta(minutes=index)
                db.add(
                    image_asset(
                        owner_id=user.id,
                        project_id=project_id,
                        created_at=base_time + timedelta(seconds=index),
                    )
                )

        event.listen(engine, "before_cursor_execute", capture_sql)
        try:
            response = client.get("/api/projects?limit=4")
        finally:
            event.remove(engine, "before_cursor_execute", capture_sql)

        assert response.status_code == 200, response.text
        payload = response.json()
        expected_page = list(reversed(project_ids[-4:]))
        returned_ids = [UUID(item["id"]) for item in payload["items"]]
        assert returned_ids == expected_page
        assert payload["next_cursor"] is not None
        assert all(item["preview_asset_id"] is not None for item in payload["items"])
        assert preview_calls == [expected_page]
        assert len(asset_selects) == 1
    finally:
        with SessionLocal.begin() as db:
            db.execute(delete(User).where(User.email == email))
