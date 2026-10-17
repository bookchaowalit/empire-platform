"""Shared world for outbox, graph, and MCP tests: one workspace with a goal,
a confidential budget, and a restricted diary entry, plus another workspace."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

import pytest
from fastapi.testclient import TestClient

from conftest import add_member, create_workspace, delegate, new_id, use_workspace
from empire_platform.devtoken import key_pair, mint
from empire_platform.permissions import TASKS_OWNER
from graph_service.app import create_app as create_graph_app
from graph_service.consumer import GraphConsumer
from tasks_api.app import create_app as create_tasks_app
from tasks_api.auth import TokenVerifier
from tasks_api.publisher import publish

ISSUER = "empire-graph-test"


@dataclass
class GraphWorld:
    ws: uuid.UUID
    beta: uuid.UUID
    owner: uuid.UUID
    viewer: uuid.UUID
    coach: uuid.UUID
    outsider: uuid.UUID
    coach_rw: uuid.UUID
    coach_read: uuid.UUID
    goal: str
    budget: str
    diary: str


def event(
    ws: uuid.UUID,
    type_: str,
    subject: str,
    payload: dict[str, Any],
    classification: str = "internal",
) -> dict:
    return {
        "id": str(uuid.uuid4()),
        "workspace_id": str(ws),
        "type": type_,
        "version": 1,
        "subject": subject,
        "execution_id": None,
        "classification": classification,
        "payload": payload,
        "occurred_at": "2026-10-09T00:00:00+00:00",
    }


@pytest.fixture(scope="module")
def gkeys(tmp_path_factory):
    return key_pair(tmp_path_factory.mktemp("graph-keys"))


@pytest.fixture(scope="module")
def gworld(cluster) -> GraphWorld:
    with cluster.connect("platform_api") as conn:
        suffix = uuid.uuid4().hex[:8]
        ws = create_workspace(
            conn,
            f"graph-{suffix}",
            [
                *TASKS_OWNER,
                "goals.goal:read",
                "goals.goal:write",
                "finance.budget:read",
                "diary.entry:read",
                "diary.entry:write",
            ],
        )
        viewer = add_member(conn, ws, "viewer", "human")
        coach = add_member(conn, ws, "coach", "agent")
        use_workspace(conn, ws.id)
        role = new_id(conn)
        conn.execute(
            "INSERT INTO platform.roles (id, workspace_id, name) VALUES (%s, %s, 'viewer')",
            (role, ws.id),
        )
        for permission in ("tasks.task:read", "goals.goal:read"):
            conn.execute(
                "INSERT INTO platform.role_permissions (workspace_id, role_id, permission) VALUES (%s, %s, %s)",
                (ws.id, role, permission),
            )
        conn.execute(
            "INSERT INTO platform.membership_roles (workspace_id, identity_id, role_id) VALUES (%s, %s, %s)",
            (ws.id, viewer, role),
        )
        coach_rw = delegate(
            conn,
            ws,
            ws.owner,
            coach,
            [
                "tasks.task:read",
                "tasks.task:write",
                "tasks.link:write",
                "goals.goal:read",
            ],
        )
        coach_read = delegate(conn, ws, ws.owner, coach, ["tasks.task:read"])
        beta = create_workspace(
            conn, f"graph-beta-{suffix}", ["tasks.task:read", "goals.goal:read"]
        )
        conn.commit()

    goal = f"empire:goals:goal:{uuid.uuid4()}"
    budget = f"empire:finance:budget:{uuid.uuid4()}"
    diary = f"empire:diary:entry:{uuid.uuid4()}"
    consumer = GraphConsumer(lambda: cluster.connect("graph_api"))
    consumer(
        event(
            ws.id,
            "goals.goal.created",
            goal,
            {"version": 1, "deleted": False, "title": "Run Marathon"},
        )
    )
    consumer(
        event(
            ws.id,
            "finance.budget.created",
            budget,
            {"version": 1, "deleted": False, "title": "Race budget"},
            "confidential",
        )
    )
    consumer(
        event(
            ws.id,
            "finance.budget.linked",
            budget,
            {
                "version": 1,
                "deleted": False,
                "title": "Race budget",
                "relation": "tracks-goal",
                "target_type": "goal",
                "target_ref": goal,
            },
            "confidential",
        )
    )
    consumer(
        event(
            ws.id,
            "diary.entry.created",
            diary,
            {"version": 1, "deleted": False, "title": "Felt slow today"},
            "restricted",
        )
    )
    consumer(
        event(
            ws.id,
            "diary.entry.linked",
            diary,
            {
                "version": 1,
                "deleted": False,
                "relation": "tracks-goal",
                "target_type": "goal",
                "target_ref": goal,
            },
            "restricted",
        )
    )
    return GraphWorld(
        ws.id,
        beta.id,
        ws.owner,
        viewer,
        coach,
        beta.owner,
        coach_rw,
        coach_read,
        goal,
        budget,
        diary,
    )


@pytest.fixture(scope="module")
def apps(cluster, gkeys):
    _, public_pem = gkeys
    tasks = TestClient(
        create_tasks_app(
            lambda: cluster.connect("tasks_api"),
            TokenVerifier(public_pem, ISSUER, "tasks-api"),
        )
    )
    graph = TestClient(
        create_graph_app(
            lambda: cluster.connect("graph_api"),
            TokenVerifier(public_pem, ISSUER, "graph-api"),
        )
    )
    return tasks, graph


@pytest.fixture
def gtoken(gkeys):
    private_pem, _ = gkeys

    def make(sub, ws, **kwargs) -> str:
        return mint(private_pem, sub=sub, ws=ws, issuer=ISSUER, **kwargs)

    return make


def bearer(value: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {value}"}


def run_publisher(cluster, deliver=None):
    deliver = deliver or GraphConsumer(lambda: cluster.connect("graph_api"))
    return publish(lambda: cluster.connect("tasks_publisher"), deliver, batch=1000)


def due_now(cluster) -> None:
    """Skip the retry backoff, as if time had passed."""
    with cluster.connect("postgres") as conn:
        conn.execute(
            "UPDATE tasks.outbox SET next_attempt_at = now() WHERE published_at IS NULL"
        )
        conn.commit()


def neighbors(graph, token: str, urn: str) -> list[tuple[str, str, str]]:
    response = graph.get(
        "/v1/graph/neighbors", params={"urn": urn}, headers=bearer(token)
    )
    assert response.status_code == 200, response.text
    return sorted(
        (item["urn"], item["relation"], item["direction"])
        for item in response.json()["items"]
    )
