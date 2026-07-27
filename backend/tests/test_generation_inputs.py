from copy import deepcopy
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.core.model_credentials import encrypt_api_key
from app.database import SessionLocal
from app.main import app
from app.models import AIModel, ModelGroup, Project, User, UserModelCredential


def node(node_id: str, kind: str, *, asset_id: str | None = None) -> dict:
    return {
        "id": node_id,
        "type": "prompt" if kind == "text" else kind,
        "position": {"x": 0, "y": 0},
        "size": {"width": 320, "height": 180},
        "data": {
            "title": f"{kind} input",
            "prompt": f"prompt from {kind}",
            "asset_id": asset_id,
        },
    }


def edge(source: str, target: str) -> dict:
    return {
        "id": str(uuid4()),
        "source_node_id": source,
        "target_node_id": target,
        "data": {},
    }


def test_generation_input_graph_validation_and_unsupported_media():
    client = TestClient(app)
    email = f"generation-inputs-{uuid4()}@example.com"
    group_id = uuid4()
    model_id = uuid4()
    registered = client.post(
        "/api/auth/register",
        json={"email": email, "password": "Strong123"},
    )
    assert registered.status_code == 201
    user_id = registered.json()["user"]["id"]

    try:
        with SessionLocal() as db:
            db.add(ModelGroup(id=group_id, name=f"Input test {uuid4()}", status="active"))
            db.flush()
            db.add(
                AIModel(
                    id=model_id,
                    group_id=group_id,
                    key=str(uuid4()),
                    model_name="input-test-image",
                    base_url="https://provider.example/v1",
                    display_name="Input test image",
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
                    encrypted_api_key=encrypt_api_key(db, "input-test-key"),
                    api_key_last_four="-key",
                )
            )
            db.commit()

        cross_project_node_id = str(uuid4())
        cross_project = client.post(
            "/api/projects",
            json={
                "name": "Other graph",
                "workflow": {
                    "schema_version": 1,
                    "viewport": {"x": 0, "y": 0, "zoom": 1},
                    "nodes": [node(cross_project_node_id, "text")],
                    "edges": [],
                },
            },
        )
        assert cross_project.status_code == 201

        target_id = str(uuid4())
        unsupported_target_id = str(uuid4())
        incompatible_target_id = str(uuid4())
        orphan_target_id = str(uuid4())
        blank_target_id = str(uuid4())
        blank_text_id = str(uuid4())
        text_id = str(uuid4())
        non_direct_id = str(uuid4())
        image_id = str(uuid4())
        audio_id = str(uuid4())
        audio_target_id = str(uuid4())
        workflow = {
            "schema_version": 1,
            "viewport": {"x": 0, "y": 0, "zoom": 1},
            "nodes": [
                node(target_id, "image"),
                node(unsupported_target_id, "image"),
                node(incompatible_target_id, "image"),
                node(orphan_target_id, "image"),
                node(blank_target_id, "image"),
                node(blank_text_id, "text"),
                node(text_id, "text"),
                node(non_direct_id, "text"),
                node(image_id, "image"),
                node(audio_id, "audio"),
                node(audio_target_id, "audio"),
            ],
            "edges": [
                edge(text_id, target_id),
                edge(image_id, target_id),
                edge(image_id, unsupported_target_id),
                edge(blank_text_id, blank_target_id),
            ],
        }
        workflow["nodes"][5]["data"]["prompt"] = ""
        project = client.post(
            "/api/projects",
            json={"name": "Input graph", "project_type": "mixed", "workflow": workflow},
        )
        assert project.status_code == 201, project.text
        project_id = project.json()["id"]
        image_asset = client.post(
            "/api/assets",
            json={
                "project_id": project_id,
                "kind": "image",
                "source": "upload",
                "storage_key": f"{user_id}/{project_id}/input.png",
                "mime_type": "image/png",
                "byte_size": 1,
                "status": "ready",
            },
        )
        assert image_asset.status_code == 201, image_asset.text
        for workflow_node in workflow["nodes"]:
            if workflow_node["id"] == image_id:
                workflow_node["data"]["asset_id"] = image_asset.json()["id"]
        saved = client.put(
            f"/api/projects/{project_id}/workflow",
            json={"base_revision": 1, "workflow": workflow},
        )
        assert saved.status_code == 200, saved.text
        with SessionLocal() as db:
            project_row = db.get(Project, UUID(project_id))
            legacy_workflow = deepcopy(project_row.workflow)
            legacy_workflow["edges"].extend(
                [edge(audio_id, incompatible_target_id), edge(audio_id, audio_target_id)]
            )
            project_row.workflow = legacy_workflow
            db.commit()

        def generation(
            target: str,
            kind: str,
            inputs: list[str],
            prompt: str = "make output",
        ):
            return client.post(
                "/api/generations",
                headers={"Idempotency-Key": str(uuid4())},
                json={
                    "project_id": project_id,
                    "node_id": target,
                    "input_node_ids": inputs,
                    "input_revision": 2,
                    "kind": kind,
                    "model": str(model_id),
                    "prompt": prompt,
                    "parameters": {},
                },
            )

        self_reference = generation(target_id, "image", [target_id])
        assert self_reference.status_code == 422
        assert "target node itself" in self_reference.json()["detail"]

        cross_project_input = generation(target_id, "image", [cross_project_node_id])
        assert cross_project_input.status_code == 422
        assert "does not exist" in cross_project_input.json()["detail"]

        non_direct = generation(target_id, "image", [non_direct_id])
        assert non_direct.status_code == 422
        assert "direct predecessor" in non_direct.json()["detail"]

        missing = generation(target_id, "image", [text_id])
        assert missing.status_code == 422
        assert "exactly match every direct predecessor" in missing.json()["detail"]

        orphan = generation(orphan_target_id, "image", [])
        assert orphan.status_code == 422
        assert "at least one direct predecessor" in orphan.json()["detail"]

        blank_prompt = generation(blank_target_id, "image", [blank_text_id], prompt="")
        assert blank_prompt.status_code == 422
        assert blank_prompt.json()["detail"] == (
            "invalid-input: Provider requires a non-empty text predecessor or legacy prompt"
        )

        incompatible = generation(incompatible_target_id, "image", [audio_id])
        assert incompatible.status_code == 422
        assert "audio is not compatible with image" in incompatible.json()["detail"]

        audio_from_audio = generation(audio_target_id, "audio", [audio_id])
        assert audio_from_audio.status_code == 422
        assert "audio is not compatible with audio" in audio_from_audio.json()["detail"]

        target_mismatch = generation(target_id, "video", [text_id, image_id])
        assert target_mismatch.status_code == 422
        assert "target workflow node type" in target_mismatch.json()["detail"]

        unsupported_media = generation(
            unsupported_target_id,
            "image",
            [image_id],
            prompt="forged media-only prompt",
        )
        assert unsupported_media.status_code == 422
        assert unsupported_media.json()["detail"] == (
            "unsupported-input: Selected model does not support predecessor input type: image"
        )
    finally:
        with SessionLocal() as db:
            user = db.scalar(select(User).where(User.email == email))
            if user is not None:
                db.delete(user)
            db.execute(delete(AIModel).where(AIModel.id == model_id))
            db.execute(delete(ModelGroup).where(ModelGroup.id == group_id))
            db.commit()
