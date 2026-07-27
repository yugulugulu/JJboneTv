import base64
from contextlib import contextmanager
from uuid import UUID, uuid4

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.config import settings
from app.core.model_credentials import encrypt_api_key
from app.database import SessionLocal
from app.main import app
from app.models import AIModel, ModelGroup, User, UserModelCredential
from app.services import generation_provider


def _node(node_id: str, kind: str, prompt: str = "") -> dict:
    return {
        "id": node_id,
        "type": "prompt" if kind == "text" else kind,
        "position": {"x": 0, "y": 0},
        "size": {"width": 320, "height": 180},
        "data": {"title": f"{kind} node", "prompt": prompt},
    }


def test_media_generation_uses_workflow_text_and_requires_predecessor(monkeypatch, tmp_path):
    """Media generation has no independent prompt; its graph inputs are authoritative."""
    image_bytes = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9ZP9sAAAAASUVORK5CYII="
    )
    provider_requests: list[dict] = []

    class FakeProviderClient:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        @contextmanager
        def stream(self, method, _url, *, headers, json):
            assert method == "POST"
            provider_requests.append({"headers": headers, "json": json})
            yield httpx.Response(
                200,
                json={
                    "data": [
                        {"b64_json": base64.b64encode(image_bytes).decode("ascii")}
                    ]
                },
            )

    monkeypatch.setattr(
        generation_provider, "provider_http_client", lambda _timeout: FakeProviderClient()
    )
    original_storage_root = settings.generated_storage_root
    settings.generated_storage_root = str(tmp_path)

    client = TestClient(app)
    email = f"generation-prompt-contract-{uuid4()}@example.com"
    group_id = uuid4()
    model_id = uuid4()
    workflow_prompt = "A red ceramic cup photographed on a white table"

    try:
        registered = client.post(
            "/api/auth/register", json={"email": email, "password": "Strong123"}
        )
        assert registered.status_code == 201, registered.text
        user_id = registered.json()["user"]["id"]

        with SessionLocal() as db:
            db.add(ModelGroup(id=group_id, name=f"Prompt contract {uuid4()}", status="active"))
            db.flush()
            db.add(
                AIModel(
                    id=model_id,
                    group_id=group_id,
                    key=str(uuid4()),
                    model_name="prompt-contract-image",
                    base_url="https://provider.example/v1",
                    display_name="Prompt contract image",
                    kind="image",
                    status="active",
                    config_schema={
                        "type": "object",
                        "properties": {},
                        "additionalProperties": False,
                    },
                    model_metadata={"protocol": "openai_images"},
                )
            )
            db.flush()
            db.add(
                UserModelCredential(
                    user_id=user_id,
                    group_id=group_id,
                    encrypted_api_key=encrypt_api_key(db, "prompt-contract-key"),
                    api_key_last_four="-key",
                )
            )
            db.commit()

        text_id = str(uuid4())
        linked_image_id = str(uuid4())
        empty_image_id = str(uuid4())
        empty_video_id = str(uuid4())
        empty_audio_id = str(uuid4())
        project = client.post(
            "/api/projects",
            json={
                "name": "Media prompt contract",
                "project_type": "image",
                "workflow": {
                    "schema_version": 1,
                    "viewport": {"x": 0, "y": 0, "zoom": 1},
                    "nodes": [
                        _node(text_id, "text", workflow_prompt),
                        _node(linked_image_id, "image", "stale media-node prompt"),
                        _node(empty_image_id, "image", "must not be used"),
                        _node(empty_video_id, "video", "must not be used"),
                        _node(empty_audio_id, "audio", "must not be used"),
                    ],
                    "edges": [
                        {
                            "id": str(uuid4()),
                            "source_node_id": text_id,
                            "target_node_id": linked_image_id,
                            "source_handle": "right",
                            "target_handle": "left",
                            "data": {},
                        }
                    ],
                },
            },
        )
        assert project.status_code == 201, project.text

        generated = client.post(
            "/api/generations",
            headers={"Idempotency-Key": str(uuid4())},
            json={
                "project_id": project.json()["id"],
                "node_id": linked_image_id,
                "input_node_ids": [text_id],
                "input_revision": 1,
                "kind": "image",
                "model": str(model_id),
                "parameters": {},
            },
        )
        assert generated.status_code == 202, generated.text
        generation_provider.run_generation_task(UUID(generated.json()["id"]))
        assert generated.json()["prompt"] == workflow_prompt
        assert provider_requests[0]["json"]["prompt"] == workflow_prompt

        forged_prompt = client.post(
            "/api/generations",
            headers={"Idempotency-Key": str(uuid4())},
            json={
                "project_id": project.json()["id"],
                "node_id": linked_image_id,
                "input_node_ids": [text_id],
                "input_revision": 1,
                "kind": "image",
                "model": str(model_id),
                "prompt": "caller text must not override the graph",
                "parameters": {},
            },
        )
        assert forged_prompt.status_code == 202, forged_prompt.text
        generation_provider.run_generation_task(UUID(forged_prompt.json()["id"]))
        assert forged_prompt.json()["prompt"] == "caller text must not override the graph"
        assert provider_requests[1]["json"]["prompt"] == "caller text must not override the graph"

        for kind, node_id in (
            ("image", empty_image_id),
            ("video", empty_video_id),
            ("audio", empty_audio_id),
        ):
            no_predecessor = client.post(
                "/api/generations",
                headers={"Idempotency-Key": str(uuid4())},
                json={
                    "project_id": project.json()["id"],
                    "node_id": node_id,
                    "input_node_ids": [],
                    "input_revision": 1,
                    "kind": kind,
                    "model": str(model_id),
                    "parameters": {},
                },
            )
            assert no_predecessor.status_code == 422, no_predecessor.text
            assert "predecessor" in no_predecessor.json()["detail"].lower()
        assert len(provider_requests) == 2
    finally:
        settings.generated_storage_root = original_storage_root
        with SessionLocal() as db:
            user = db.scalar(select(User).where(User.email == email))
            if user is not None:
                db.delete(user)
            db.execute(delete(AIModel).where(AIModel.id == model_id))
            db.execute(delete(ModelGroup).where(ModelGroup.id == group_id))
            db.commit()
