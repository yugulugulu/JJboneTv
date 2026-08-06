from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.schemas import Workflow


def workflow_node(node_id, kind):
    return {
        "id": str(node_id),
        "type": "prompt" if kind == "text" else kind,
        "position": {"x": 0, "y": 0},
        "size": {"width": 100, "height": 100},
        "data": {},
    }


def workflow_edge(source, target, *, source_handle="bottom", target_handle="top"):
    return {
        "id": str(uuid4()),
        "source_node_id": str(source),
        "target_node_id": str(target),
        "source_handle": source_handle,
        "target_handle": target_handle,
        "data": {},
    }


def validate(nodes, edges):
    return Workflow.model_validate(
        {
            "schema_version": 1,
            "viewport": {"x": 0, "y": 0, "zoom": 1},
            "nodes": nodes,
            "edges": edges,
        }
    )


def test_workflow_accepts_legacy_handles_on_compatible_acyclic_edges():
    source = uuid4()
    target = uuid4()
    parsed = validate(
        [workflow_node(source, "text"), workflow_node(target, "image")],
        [workflow_edge(source, target)],
    )
    assert parsed.edges[0].source_handle == "bottom"
    assert parsed.edges[0].target_handle == "top"


def test_workflow_accepts_media_metadata_and_generation_output_message():
    node_id = uuid4()
    asset_id = uuid4()
    task_id = uuid4()
    node = workflow_node(node_id, "video")
    node["data"] = {
        "asset_id": str(asset_id),
        "asset_metadata": {
            "byte_size": 1024,
            "duration_ms": 5000,
            "mime_type": "video/mp4",
        },
        "messages": [
            {
                "id": str(uuid4()),
                "role": "assistant",
                "content": "视频生成完成",
                "generation_task_id": str(task_id),
                "generation_phase": "completed",
                "asset_id": str(asset_id),
                "asset_kind": "video",
                "created_at": "2026-08-05T00:00:00Z",
            }
        ],
    }
    parsed = validate([node], [])
    assert parsed.nodes[0].data.asset_metadata["duration_ms"] == 5000
    assert parsed.nodes[0].data.messages[0].asset_kind == "video"


@pytest.mark.parametrize("case", ["self", "duplicate", "incompatible", "text_video", "cycle"])
def test_workflow_rejects_invalid_graph_semantics(case):
    first = uuid4()
    second = uuid4()
    third = uuid4()
    if case == "self":
        nodes = [workflow_node(first, "image")]
        edges = [workflow_edge(first, first)]
    elif case == "duplicate":
        nodes = [workflow_node(first, "text"), workflow_node(second, "image")]
        edges = [workflow_edge(first, second), workflow_edge(first, second)]
    elif case == "incompatible":
        nodes = [workflow_node(first, "audio"), workflow_node(second, "image")]
        edges = [workflow_edge(first, second)]
    elif case == "text_video":
        nodes = [workflow_node(first, "text"), workflow_node(second, "video")]
        edges = [workflow_edge(first, second)]
    else:
        nodes = [
            workflow_node(first, "image"),
            workflow_node(second, "video"),
            workflow_node(third, "video"),
        ]
        edges = [
            workflow_edge(first, second),
            workflow_edge(second, third),
            workflow_edge(third, second),
        ]
    with pytest.raises(ValidationError):
        validate(nodes, edges)
