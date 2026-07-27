import base64
from contextlib import contextmanager
import json
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.config import settings
from app.core.model_credentials import encrypt_api_key
from app.database import SessionLocal
from app.exceptions import DomainValidationError
from app.main import app
from app.models import (
    AIModel,
    GenerationTask,
    ModelGroup,
    Project,
    User,
    UserModelCredential,
)
from app.services import generation_provider
from app.services.asset_storage import LocalAssetStorage


PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9ZP9sAAAAASUVORK5CYII="
)


def test_image_reference_tokens_allow_arbitrary_order_and_repeated_mentions():
    first_id = str(uuid4())
    second_id = str(uuid4())
    references = [
        {"kind": "image", "node_id": first_id},
        {"kind": "image", "node_id": second_id},
    ]

    compiled = generation_provider.compile_image_reference_tokens(
        (
            f"Use @{{image:{second_id}}} as the background, "
            f"keep @{{image:{first_id}}}, and reuse @{{image:{second_id}}}"
        ),
        references,
    )

    assert compiled == "Use 图片2 as the background, keep 图片1, and reuse 图片2"


@pytest.mark.parametrize(
    ("prompt", "expected_detail"),
    [
        ("Use @{image:not-a-uuid}", "malformed"),
        (f"Use @{{image:{uuid4()}}}", "direct image predecessor"),
    ],
)
def test_image_reference_tokens_reject_malformed_or_non_predecessor_tokens(
    prompt, expected_detail
):
    predecessor_id = str(uuid4())

    with pytest.raises(DomainValidationError) as exc_info:
        generation_provider.compile_image_reference_tokens(
            prompt,
            [{"kind": "image", "node_id": predecessor_id}],
        )

    assert expected_detail in str(exc_info.value)


def _node(node_id: str, kind: str, *, prompt: str = "", asset_id: str | None = None):
    return {
        "id": node_id,
        "type": "prompt" if kind == "text" else kind,
        "position": {"x": 0, "y": 0},
        "size": {"width": 320, "height": 180},
        "data": {
            "title": f"{kind} node",
            "prompt": prompt,
            "asset_id": asset_id,
        },
    }


def _edge(source: str, target: str):
    return {
        "id": str(uuid4()),
        "source_node_id": source,
        "target_node_id": target,
        "source_handle": "right",
        "target_handle": "left",
        "data": {},
    }


def _install_edit_model(
    user_id: str,
    group_id,
    model_id,
    *,
    edit_max_input_images: int | None = None,
    edit_image_field: str | None = None,
    with_size_matrix: bool = False,
) -> None:
    with SessionLocal() as db:
        db.add(ModelGroup(id=group_id, name=f"Independent edit {uuid4()}", status="active"))
        db.flush()
        db.add(
            AIModel(
                id=model_id,
                group_id=group_id,
                key=str(uuid4()),
                model_name="gpt-image-edit-independent",
                base_url="https://provider.example/v1",
                display_name="Independent image edit",
                kind="image",
                status="active",
                config_schema={
                    "type": "object",
                    "properties": {
                        "quality": {"type": "string"},
                        "n": {"type": "integer"},
                        **(
                            {
                                "size": {
                                    "type": "string",
                                    "enum": ["1024x1024"],
                                }
                            }
                            if with_size_matrix
                            else {}
                        ),
                    },
                    "additionalProperties": False,
                },
                default_parameters={
                    "quality": "high",
                    "n": 1,
                    **({"size": "1024x1024"} if with_size_matrix else {}),
                },
                model_metadata={
                    "protocol": "openai_images",
                    "supported_input_kinds": ["image"],
                    "edit_protocol": "openai_images_multipart",
                    "edit_endpoint_path": "/images/edits",
                    **(
                        {
                            "image_size_matrix": {
                                "schema_version": 1,
                                "default_resolution_key": "1k",
                                "default_aspect_ratio_key": "1:1",
                                "resolutions": [
                                    {
                                        "key": "1k",
                                        "display_name": "1K",
                                        "ratios": [
                                            {
                                                "key": "1:1",
                                                "display_name": "1:1",
                                                "size": "1024x1024",
                                            }
                                        ],
                                    }
                                ],
                            }
                        }
                        if with_size_matrix
                        else {}
                    ),
                    **(
                        {
                            "edit_max_input_images": edit_max_input_images,
                            "edit_image_field": edit_image_field,
                        }
                        if edit_max_input_images is not None
                        or edit_image_field is not None
                        else {}
                    ),
                },
            )
        )
        db.flush()
        db.add(
            UserModelCredential(
                user_id=user_id,
                group_id=group_id,
                encrypted_api_key=encrypt_api_key(db, "independent-edit-secret"),
                api_key_last_four="cret",
            )
        )
        db.commit()


@pytest.mark.parametrize("mode", ["text_only", "image_only", "image_and_text"])
def test_image_generation_modes_use_exact_provider_contract_and_new_asset(
    mode, monkeypatch, tmp_path
):
    provider_requests: list[dict] = []

    class FakeProviderClient:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        @contextmanager
        def stream(self, method, url, **kwargs):
            assert method == "POST"
            if "files" in kwargs:
                kwargs["files"] = [
                    (field, (filename, file.read(), mime_type))
                    for field, (filename, file, mime_type) in kwargs["files"]
                ]
            provider_requests.append({"url": url, **kwargs})
            yield httpx.Response(
                200,
                json={"data": [{"b64_json": base64.b64encode(PNG_BYTES).decode()}]},
            )

    monkeypatch.setattr(
        generation_provider, "provider_http_client", lambda _timeout: FakeProviderClient()
    )
    monkeypatch.setattr(settings, "generated_storage_root", str(tmp_path))
    monkeypatch.setattr(settings, "asset_storage_backend", "local")

    client = TestClient(app)
    email = f"edit-{mode[:3]}-{uuid4()}@example.com"
    group_id = uuid4()
    model_id = uuid4()
    input_asset = None
    input_key = None

    try:
        registered = client.post(
            "/api/auth/register", json={"email": email, "password": "Strong123"}
        )
        assert registered.status_code == 201, registered.text
        user_id = registered.json()["user"]["id"]
        _install_edit_model(
            user_id, group_id, model_id, with_size_matrix=mode == "text_only"
        )

        project = client.post("/api/projects", json={"name": f"Independent {mode}"})
        assert project.status_code == 201, project.text
        project_id = project.json()["id"]

        target_id = str(uuid4())
        image_id = str(uuid4())
        text_id = str(uuid4())
        nodes = [_node(target_id, "image")]
        edges = []
        input_ids = []
        expected_prompt = "forged client prompt must be ignored"

        if mode in {"image_only", "image_and_text"}:
            input_key = f"{user_id}/{project_id}/source.png"
            LocalAssetStorage().put_object(input_key, PNG_BYTES, "image/png")
            input_asset_response = client.post(
                "/api/assets",
                json={
                    "project_id": project_id,
                    "kind": "image",
                    "source": "upload",
                    "storage_key": input_key,
                    "original_name": "source.png",
                    "mime_type": "image/png",
                    "byte_size": len(PNG_BYTES),
                    "status": "ready",
                },
            )
            assert input_asset_response.status_code == 201, input_asset_response.text
            input_asset = input_asset_response.json()
            assert input_asset["metadata"] == {}
            nodes.append(_node(image_id, "image", asset_id=input_asset["id"]))
            edges.append(_edge(image_id, target_id))
            input_ids.append(image_id)

        if mode in {"text_only", "image_and_text"}:
            nodes.append(_node(text_id, "text", prompt=expected_prompt))
            edges.append(_edge(text_id, target_id))
            input_ids.append(text_id)

        workflow = {
            "schema_version": 1,
            "viewport": {"x": 0, "y": 0, "zoom": 1},
            "nodes": nodes,
            "edges": edges,
        }
        saved = client.put(
            f"/api/projects/{project_id}/workflow",
            json={"base_revision": 0, "workflow": workflow},
        )
        assert saved.status_code == 200, saved.text

        started = client.post(
            "/api/generations",
            headers={"Idempotency-Key": str(uuid4())},
            json={
                "project_id": project_id,
                "node_id": target_id,
                "input_node_ids": input_ids,
                "input_revision": 1,
                "kind": "image",
                "model": str(model_id),
                "prompt": "forged client prompt must be ignored",
                "parameters": {"quality": "high"},
            },
        )
        assert started.status_code == 202, started.text
        if mode == "text_only":
            with SessionLocal() as db:
                task = db.get(GenerationTask, UUID(started.json()["id"]))
                task.parameters = {
                    **task.parameters,
                    "resolution": "1k",
                    "aspect_ratio": "1:1",
                }
                db.commit()
        generation_provider.run_generation_task(UUID(started.json()["id"]))
        completed_response = client.get(f"/api/generations/{started.json()['id']}")
        assert completed_response.status_code == 200, completed_response.text
        completed = completed_response.json()
        assert completed["status"] == "succeeded", completed
        assert completed["prompt"] == expected_prompt
        assert "independent-edit-secret" not in json.dumps(completed)
        assert not {
            "resolution",
            "aspect_ratio",
            "resolution_key",
            "aspect_ratio_key",
        }.intersection(completed["parameters"])
        assert len(provider_requests) == 1

        request = provider_requests[0]
        if mode == "text_only":
            assert request == {
                "url": "https://provider.example/v1/images/generations",
                "headers": {
                    "Authorization": "Bearer independent-edit-secret",
                    "Content-Type": "application/json",
                },
                "json": {
                    "model": "gpt-image-edit-independent",
                    "prompt": expected_prompt,
                    "quality": "high",
                    "n": 1,
                    "size": "1024x1024",
                },
            }
        else:
            assert request == {
                "url": "https://provider.example/v1/images/edits",
                "headers": {"Authorization": "Bearer independent-edit-secret"},
                "data": {
                    "model": "gpt-image-edit-independent",
                    "prompt": expected_prompt,
                    "quality": "high",
                    "n": "1",
                },
                "files": [("image", ("input-1.png", PNG_BYTES, "image/png"))],
            }

        output_asset = completed["outputs"][0]["asset"]
        assert output_asset["source"] == "generation"
        assert output_asset["id"] != (input_asset or {}).get("id")
        assert output_asset["storage_key"] != input_key
        if input_asset is not None:
            unchanged = client.get(f"/api/assets/{input_asset['id']}")
            assert unchanged.status_code == 200
            assert unchanged.json()["status"] == "ready"
            assert LocalAssetStorage().read_object(input_key, len(PNG_BYTES)) == PNG_BYTES
    finally:
        with SessionLocal() as db:
            user = db.scalar(select(User).where(User.email == email))
            if user is not None:
                db.delete(user)
            db.execute(delete(AIModel).where(AIModel.id == model_id))
            db.execute(delete(ModelGroup).where(ModelGroup.id == group_id))
            db.commit()


def test_image_generation_rejects_ambiguous_and_unready_inputs(monkeypatch, tmp_path):
    provider_calls = 0

    class UnexpectedProviderClient:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def post(self, *_args, **_kwargs):
            nonlocal provider_calls
            provider_calls += 1
            return httpx.Response(
                200,
                json={"data": [{"b64_json": base64.b64encode(PNG_BYTES).decode()}]},
            )

    monkeypatch.setattr(
        generation_provider,
        "provider_http_client",
        lambda _timeout: UnexpectedProviderClient(),
    )
    monkeypatch.setattr(settings, "generated_storage_root", str(tmp_path))
    monkeypatch.setattr(settings, "asset_storage_backend", "local")

    client = TestClient(app)
    email = f"independent-invalid-edit-{uuid4()}@example.com"
    group_id = uuid4()
    model_id = uuid4()
    try:
        registered = client.post(
            "/api/auth/register", json={"email": email, "password": "Strong123"}
        )
        user_id = registered.json()["user"]["id"]
        _install_edit_model(user_id, group_id, model_id)
        project = client.post("/api/projects", json={"name": "Invalid image inputs"})
        project_id = project.json()["id"]

        image_nodes = [str(uuid4()), str(uuid4())]
        text_nodes = [str(uuid4()), str(uuid4())]
        blank_text = str(uuid4())
        pending_image = str(uuid4())
        targets = {name: str(uuid4()) for name in ("images", "texts", "text_only", "blank", "pending")}
        nodes = [_node(target, "image") for target in targets.values()]

        for index, image_node in enumerate(image_nodes):
            key = f"{user_id}/{project_id}/input-{index}.png"
            LocalAssetStorage().put_object(key, PNG_BYTES, "image/png")
            asset = client.post(
                "/api/assets",
                json={
                    "project_id": project_id,
                    "kind": "image",
                    "source": "upload",
                    "storage_key": key,
                    "mime_type": "image/png",
                    "byte_size": len(PNG_BYTES),
                    "status": "ready",
                },
            ).json()
            nodes.append(_node(image_node, "image", asset_id=asset["id"]))

        pending_asset = client.post(
            "/api/assets",
            json={
                "project_id": project_id,
                "kind": "image",
                "source": "upload",
                "storage_key": f"{user_id}/{project_id}/pending.png",
                "mime_type": "image/png",
                "byte_size": len(PNG_BYTES),
                "status": "uploading",
            },
        ).json()
        nodes.append(_node(pending_image, "image", asset_id=pending_asset["id"]))
        nodes.extend(
            [
                _node(text_nodes[0], "text", prompt="first"),
                _node(text_nodes[1], "text", prompt="second"),
                _node(blank_text, "text", prompt=""),
            ]
        )
        edges = [
            _edge(image_nodes[0], targets["images"]),
            _edge(image_nodes[1], targets["images"]),
            _edge(image_nodes[0], targets["texts"]),
            _edge(text_nodes[0], targets["texts"]),
            _edge(text_nodes[1], targets["texts"]),
            _edge(text_nodes[0], targets["text_only"]),
            _edge(text_nodes[1], targets["text_only"]),
            _edge(image_nodes[0], targets["blank"]),
            _edge(blank_text, targets["blank"]),
            _edge(pending_image, targets["pending"]),
        ]
        workflow = {
            "schema_version": 1,
            "viewport": {"x": 0, "y": 0, "zoom": 1},
            "nodes": nodes,
            "edges": edges,
        }
        legal_edges = [
            edge
            for edge in edges
            if not (
                edge["source_node_id"] == text_nodes[1]
                and edge["target_node_id"] in {targets["texts"], targets["text_only"]}
            )
        ]
        saved = client.put(
            f"/api/projects/{project_id}/workflow",
            json={
                "base_revision": 0,
                "workflow": {
                    "schema_version": 1,
                    "viewport": {"x": 0, "y": 0, "zoom": 1},
                    "nodes": nodes,
                    "edges": legal_edges,
                },
            },
        )
        assert saved.status_code == 200, saved.text

        # Existing projects may contain this formerly valid shape. Reading must
        # remain backward compatible, while every new write rejects it.
        with SessionLocal() as db:
            stored_project = db.get(Project, UUID(project_id))
            assert stored_project is not None
            stored_project.workflow = workflow
            db.commit()

        historical = client.get(f"/api/projects/{project_id}/workflow")
        assert historical.status_code == 200, historical.text
        assert historical.json()["workflow"]["edges"] == edges

        rejected_save = client.put(
            f"/api/projects/{project_id}/workflow",
            json={"base_revision": 1, "workflow": workflow},
        )
        assert rejected_save.status_code == 422, rejected_save.text
        assert "图片节点最多只能有一个文本前置节点" in rejected_save.json()["detail"]

        cases = [
            ("images", image_nodes, "accepts at most 1 image predecessors"),
            ("texts", [image_nodes[0], *text_nodes], "at most one text predecessor"),
            ("text_only", text_nodes, "at most one text predecessor"),
            ("pending", [pending_image], "ready project asset"),
        ]
        for name, input_ids, detail in cases:
            response = client.post(
                "/api/generations",
                headers={"Idempotency-Key": str(uuid4())},
                json={
                    "project_id": project_id,
                    "node_id": targets[name],
                    "input_node_ids": input_ids,
                    "input_revision": 1,
                    "kind": "image",
                    "model": str(model_id),
                    "prompt": "must not rescue invalid graph inputs",
                    "parameters": {},
                },
            )
            assert response.status_code == 422, (name, response.text)
            assert detail in response.json()["detail"]
        assert provider_calls == 0
    finally:
        with SessionLocal() as db:
            user = db.scalar(select(User).where(User.email == email))
            if user is not None:
                db.delete(user)
            db.execute(delete(AIModel).where(AIModel.id == model_id))
            db.execute(delete(ModelGroup).where(ModelGroup.id == group_id))
            db.commit()


def test_five_image_edit_preserves_edge_order_compiles_tokens_and_sends_all_images(
    monkeypatch, tmp_path
):
    provider_requests: list[dict] = []

    class FakeProviderClient:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        @contextmanager
        def stream(self, method, url, **kwargs):
            assert method == "POST"
            captured_files = [
                (field, (filename, file.read(), mime_type))
                for field, (filename, file, mime_type) in kwargs.pop("files")
            ]
            provider_requests.append({"url": url, "files": captured_files, **kwargs})
            yield httpx.Response(
                200,
                json={"data": [{"b64_json": base64.b64encode(PNG_BYTES).decode()}]},
            )

    monkeypatch.setattr(
        generation_provider, "provider_http_client", lambda _timeout: FakeProviderClient()
    )
    monkeypatch.setattr(settings, "generated_storage_root", str(tmp_path))
    monkeypatch.setattr(settings, "asset_storage_backend", "local")

    client = TestClient(app)
    email = f"five-image-edit-{uuid4()}@example.com"
    group_id = uuid4()
    model_id = uuid4()
    try:
        registered = client.post(
            "/api/auth/register", json={"email": email, "password": "Strong123"}
        )
        assert registered.status_code == 201, registered.text
        user_id = registered.json()["user"]["id"]
        _install_edit_model(
            user_id,
            group_id,
            model_id,
            edit_max_input_images=5,
            edit_image_field="image[]",
        )
        project = client.post("/api/projects", json={"name": "Five ordered images"})
        assert project.status_code == 201, project.text
        project_id = project.json()["id"]
        target_id = str(uuid4())
        image_node_ids = [str(uuid4()) for _ in range(5)]
        nodes = [_node(target_id, "image")]
        input_bytes: list[bytes] = []
        for ordinal, node_id in enumerate(image_node_ids):
            content = PNG_BYTES + bytes([ordinal])
            input_bytes.append(content)
            storage_key = f"{user_id}/{project_id}/ordered-{ordinal}.png"
            LocalAssetStorage().put_object(storage_key, content, "image/png")
            asset = client.post(
                "/api/assets",
                json={
                    "project_id": project_id,
                    "kind": "image",
                    "source": "upload",
                    "storage_key": storage_key,
                    "original_name": f"ordered-{ordinal}.png",
                    "mime_type": "image/png",
                    "byte_size": len(content),
                    "status": "ready",
                },
            )
            assert asset.status_code == 201, asset.text
            nodes.append(_node(node_id, "image", asset_id=asset.json()["id"]))
        edges = [_edge(node_id, target_id) for node_id in image_node_ids]
        workflow = {
            "schema_version": 1,
            "viewport": {"x": 0, "y": 0, "zoom": 1},
            "nodes": nodes,
            "edges": edges,
        }
        saved = client.put(
            f"/api/projects/{project_id}/workflow",
            json={"base_revision": 0, "workflow": workflow},
        )
        assert saved.status_code == 200, saved.text

        wrong_order = client.post(
            "/api/generations",
            headers={"Idempotency-Key": str(uuid4())},
            json={
                "project_id": project_id,
                "node_id": target_id,
                "input_node_ids": list(reversed(image_node_ids)),
                "input_revision": 1,
                "kind": "image",
                "model": str(model_id),
                "prompt": "ordered",
                "parameters": {},
            },
        )
        assert wrong_order.status_code == 422, wrong_order.text
        assert "workflow edge order" in wrong_order.json()["detail"]

        prompt = (
            f"Combine @{{image:{image_node_ids[2]}}} with "
            f"@{{image:{image_node_ids[0]}}}; reuse "
            f"@{{image:{image_node_ids[2]}}} and retain every reference image"
        )
        started = client.post(
            "/api/generations",
            headers={"Idempotency-Key": str(uuid4())},
            json={
                "project_id": project_id,
                "node_id": target_id,
                "input_node_ids": image_node_ids,
                "input_revision": 1,
                "kind": "image",
                "model": str(model_id),
                "prompt": prompt,
                "parameters": {},
            },
        )
        assert started.status_code == 202, started.text
        expected_prompt = (
            "Combine 图片3 with 图片1; reuse 图片3 and retain every reference image"
        )
        assert started.json()["prompt"] == expected_prompt
        references = started.json()["parameters"]["_input_references"]
        assert [reference["node_id"] for reference in references] == image_node_ids
        assert [reference["ordinal"] for reference in references] == list(range(5))

        generation_provider.run_generation_task(UUID(started.json()["id"]))
        completed = client.get(f"/api/generations/{started.json()['id']}")
        assert completed.json()["status"] == "succeeded", completed.text
        assert len(provider_requests) == 1
        request = provider_requests[0]
        assert request["data"]["prompt"] == expected_prompt
        assert [field for field, _ in request["files"]] == ["image[]"] * 5
        assert [item[1][1] for item in request["files"]] == input_bytes
        assert [item[1][2] for item in request["files"]] == ["image/png"] * 5

        sixth_id = str(uuid4())
        too_many = {
            **workflow,
            "nodes": [*nodes, _node(sixth_id, "image")],
            "edges": [*edges, _edge(sixth_id, target_id)],
        }
        rejected_save = client.put(
            f"/api/projects/{project_id}/workflow",
            json={"base_revision": 1, "workflow": too_many},
        )
        assert rejected_save.status_code == 422, rejected_save.text
        assert "最多只能有五个图片前置节点" in rejected_save.json()["detail"]

        # Legacy snapshots remain readable, but generation from the invalid graph
        # is fail-closed until the extra edge is removed.
        with SessionLocal() as db:
            stored_project = db.get(Project, UUID(project_id))
            assert stored_project is not None
            stored_project.workflow = too_many
            db.commit()
        historical = client.get(f"/api/projects/{project_id}/workflow")
        assert historical.status_code == 200, historical.text
        assert len(historical.json()["workflow"]["edges"]) == 6
        rejected_generation = client.post(
            "/api/generations",
            headers={"Idempotency-Key": str(uuid4())},
            json={
                "project_id": project_id,
                "node_id": target_id,
                "input_node_ids": [*image_node_ids, sixth_id],
                "input_revision": 1,
                "kind": "image",
                "model": str(model_id),
                "prompt": "legacy graph",
                "parameters": {},
            },
        )
        assert rejected_generation.status_code == 422, rejected_generation.text
        assert "at most five image predecessors" in rejected_generation.json()["detail"]
    finally:
        with SessionLocal() as db:
            user = db.scalar(select(User).where(User.email == email))
            if user is not None:
                db.delete(user)
            db.execute(delete(AIModel).where(AIModel.id == model_id))
            db.execute(delete(ModelGroup).where(ModelGroup.id == group_id))
            db.commit()


def test_generation_rejects_total_input_image_bytes_before_provider(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "generated_storage_root", str(tmp_path))
    monkeypatch.setattr(settings, "asset_storage_backend", "local")
    monkeypatch.setattr(settings, "generation_input_images_max_total_bytes", 100)
    client = TestClient(app)
    email = f"image-total-limit-{uuid4()}@example.com"
    group_id = uuid4()
    model_id = uuid4()
    try:
        registered = client.post(
            "/api/auth/register", json={"email": email, "password": "Strong123"}
        )
        assert registered.status_code == 201, registered.text
        user_id = registered.json()["user"]["id"]
        _install_edit_model(
            user_id,
            group_id,
            model_id,
            edit_max_input_images=5,
            edit_image_field="image[]",
        )
        project = client.post("/api/projects", json={"name": "Input total limit"}).json()
        target_id = str(uuid4())
        image_ids = [str(uuid4()), str(uuid4())]
        nodes = [_node(target_id, "image")]
        for ordinal, node_id in enumerate(image_ids):
            storage_key = f"{user_id}/{project['id']}/limit-{ordinal}.png"
            LocalAssetStorage().put_object(storage_key, PNG_BYTES, "image/png")
            asset = client.post(
                "/api/assets",
                json={
                    "project_id": project["id"],
                    "kind": "image",
                    "source": "upload",
                    "storage_key": storage_key,
                    "mime_type": "image/png",
                    "byte_size": 60,
                    "status": "ready",
                },
            )
            assert asset.status_code == 201, asset.text
            nodes.append(_node(node_id, "image", asset_id=asset.json()["id"]))
        workflow = {
            "schema_version": 1,
            "viewport": {"x": 0, "y": 0, "zoom": 1},
            "nodes": nodes,
            "edges": [_edge(node_id, target_id) for node_id in image_ids],
        }
        saved = client.put(
            f"/api/projects/{project['id']}/workflow",
            json={"base_revision": 0, "workflow": workflow},
        )
        assert saved.status_code == 200, saved.text
        rejected = client.post(
            "/api/generations",
            headers={"Idempotency-Key": str(uuid4())},
            json={
                "project_id": project["id"],
                "node_id": target_id,
                "input_node_ids": image_ids,
                "input_revision": 1,
                "kind": "image",
                "model": str(model_id),
                "prompt": "combine",
                "parameters": {},
            },
        )
        assert rejected.status_code == 422, rejected.text
        assert "Total predecessor image size" in rejected.json()["detail"]
    finally:
        with SessionLocal() as db:
            user = db.scalar(select(User).where(User.email == email))
            if user is not None:
                db.delete(user)
            db.execute(delete(AIModel).where(AIModel.id == model_id))
            db.execute(delete(ModelGroup).where(ModelGroup.id == group_id))
            db.commit()
