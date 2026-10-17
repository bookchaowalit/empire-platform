"""tasks.v1 contract: committed OpenAPI, response bodies, and outbox events."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry
from referencing.jsonschema import DRAFT202012

from empire_platform.contracts import OPENAPI, render
from test_tasks_api import client, create, keys, superuser, token, world  # noqa: F401  (fixtures)

CONTRACTS = Path(__file__).resolve().parents[1] / "contracts" / "tasks" / "v1"
SPEC = json.loads(OPENAPI.read_text(encoding="utf-8"))
EVENT = json.loads((CONTRACTS / "event.schema.json").read_text(encoding="utf-8"))


def validator(component: str) -> Draft202012Validator:
    """Validate against a component of the committed OpenAPI document."""
    registry = Registry().with_resource(
        "urn:tasks-openapi", DRAFT202012.create_resource(SPEC)
    )
    schema = {"$ref": f"urn:tasks-openapi#/components/schemas/{component}"}
    return Draft202012Validator(
        schema, registry=registry, format_checker=FormatChecker()
    )


def test_committed_openapi_matches_the_code():
    assert OPENAPI.read_text(encoding="utf-8") == render(), "run empire-contracts"


def test_openapi_declares_every_operation_and_error():
    operations = {
        (path, method) for path, item in SPEC["paths"].items() for method in item
    }
    assert operations >= {
        ("/v1/tasks", "get"),
        ("/v1/tasks", "post"),
        ("/v1/tasks/{task_id}", "get"),
        ("/v1/tasks/{task_id}", "patch"),
        ("/v1/tasks/{task_id}", "delete"),
        ("/v1/tasks/{task_id}/complete", "post"),
        ("/v1/tasks/{task_id}/links", "post"),
    }
    for path, item in SPEC["paths"].items():
        if path == "/health":
            continue
        for method, operation in item.items():
            assert "401" in operation["responses"], (method, path)


def test_responses_follow_the_contract(client, token, world):  # noqa: F811
    owner = token(world.owner, world.alpha)
    task = create(client, owner, tags=["contract"], due_date="2026-10-20")
    validator("Task").validate(task)
    validator("Task").validate(
        client.get(f"/v1/tasks/{task['id']}", headers=owner).json()
    )
    validator("TaskList").validate(client.get("/v1/tasks", headers=owner).json())
    link = client.post(
        f"/v1/tasks/{task['id']}/links",
        headers=owner,
        json={
            "relation": "belongs-to",
            "target_type": "project",
            "target_ref": "empire:projects:project:p1",
        },
    )
    validator("Link").validate(link.json())


@pytest.mark.parametrize(
    "body",
    [
        {"title": ""},
        {"title": "x", "priority": 9},
        {"title": "x", "unexpected": True},
    ],
)
def test_invalid_bodies_are_rejected(client, token, world, body):  # noqa: F811
    assert (
        client.post(
            "/v1/tasks", json=body, headers=token(world.owner, world.alpha)
        ).status_code
        == 422
    )


def test_invalid_link_target_is_rejected(client, token, world):  # noqa: F811
    owner = token(world.owner, world.alpha)
    task = create(client, owner)
    response = client.post(
        f"/v1/tasks/{task['id']}/links",
        headers=owner,
        json={
            "relation": "owns",
            "target_type": "goal",
            "target_ref": "empire:goals:goal:g",
        },
    )
    assert response.status_code == 422


def test_outbox_events_follow_the_event_contract(client, token, world, cluster):  # noqa: F811
    owner = token(world.owner, world.alpha)
    task = create(client, owner)
    client.post(f"/v1/tasks/{task['id']}/complete", headers=owner)
    rows = superuser(
        cluster,
        "SELECT row_to_json(o)::text FROM tasks.outbox o WHERE subject = %s ORDER BY occurred_at",
        (task["urn"],),
    )
    events = [json.loads(row[0]) for row in rows]
    assert [event["type"] for event in events] == [
        "tasks.task.created",
        "tasks.task.completed",
    ]
    checker = Draft202012Validator(EVENT, format_checker=FormatChecker())
    for event in events:
        checker.validate(event)
