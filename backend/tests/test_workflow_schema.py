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


@pytest.mark.parametrize("case", ["self", "duplicate", "incompatible", "cycle"])
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
