"""Automation identities: least privilege per writer, revocation and expiry mid-run."""

from __future__ import annotations

import copy
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from conftest import create_workspace
from empire_platform.automations import load_registry, provision
from empire_platform.devtoken import key_pair, mint
from empire_platform.permissions import TASKS_OWNER
from tasks_api.app import create_app
from tasks_api.auth import TokenVerifier

ISSUER = "empire-automation-test"
REGISTRY = load_registry()
OPERATIONS = {
    # operation -> permission it needs
    "create": "tasks.task:write",
    "upsert": "tasks.task:write",
    "edit_notes": "tasks.task:write",
    "move_due_date": "tasks.status:write",
    "complete": "tasks.status:write",
    "set_calendar_event": "tasks.calendar:write",
    "link": "tasks.link:write",
    "archive": "tasks.archive:write",
    "reminder_delivered": "tasks.reminder:write",
    "generate_occurrences": "tasks.occurrence:write",
    "delete": "tasks.task:delete",
}


@pytest.fixture(scope="module")
def env(cluster, tmp_path_factory):
    private_pem, public_pem = key_pair(tmp_path_factory.mktemp("automation-keys"))
    with cluster.connect("platform_api") as conn:
        ws = create_workspace(conn, f"automations-{uuid.uuid4().hex[:8]}", list(TASKS_OWNER))
        conn.commit()
    client = TestClient(create_app(lambda: cluster.connect("tasks_api"), TokenVerifier(public_pem, ISSUER)))

    def token(sub, delegation=None, obo=None):
        return {"Authorization": "Bearer " + mint(private_pem, sub=sub, ws=ws.id, delegation=delegation, obo=obo,
                                                  issuer=ISSUER)}

    return ws, client, token


def owner_task(env, **fields):
    ws, client, token = env
    response = client.post("/v1/tasks", json={"title": "Fixture", "recurrence_rule": "weekly", "due_date": "2026-10-01",
                                              "reminder_at": "2026-10-10T09:00:00+00:00", **fields},
                           headers=token(ws.owner))
    assert response.status_code == 201, response.text
    return response.json()


def attempt(client, headers, operation, task) -> int:
    path = f"/v1/tasks/{task['id']}"
    calls = {
        "create": lambda: client.post("/v1/tasks", json={"title": "A"}, headers=headers),
        "upsert": lambda: client.put(f"/v1/tasks/external/probe/{uuid.uuid4().hex}", json={"title": "B"}, headers=headers),
        "edit_notes": lambda: client.patch(path, json={"notes": "n"}, headers=headers),
        "move_due_date": lambda: client.patch(path, json={"due_date": "2026-12-01"}, headers=headers),
        "complete": lambda: client.post(f"{path}/complete", headers=headers),
        "set_calendar_event": lambda: client.patch(path, json={"calendar_event_id": "cal-1"}, headers=headers),
        "link": lambda: client.post(f"{path}/links", headers=headers,
                                    json={"relation": "related-to", "target_type": "goal", "target_ref": "empire:goals:goal:g"}),
        "archive": lambda: client.post(f"{path}/archive", json={"reason": "old"}, headers=headers),
        "reminder_delivered": lambda: client.post(f"{path}/reminders/delivered", headers=headers,
                                                  json={"reminder_at": "2026-10-10T09:00:00+00:00", "channel": "line"}),
        "generate_occurrences": lambda: client.post(f"{path}/occurrences/generate", json={"until": "2026-10-15"},
                                                    headers=headers),
        "delete": lambda: client.delete(path, headers=headers),
    }
    return calls[operation]().status_code


@pytest.mark.parametrize("automation", REGISTRY["automations"], ids=[a["id"] for a in REGISTRY["automations"]])
def test_each_automation_can_do_exactly_its_scopes(cluster, env, automation):
    ws, client, token = env
    items = {item.automation: item for item in provision(cluster, ws.id)}
    item = items[automation["id"]]
    headers = token(item.identity, item.delegation, item.owner)
    for operation, permission in OPERATIONS.items():
        task = owner_task(env)
        status = attempt(client, headers, operation, task)
        allowed = permission in automation["scopes"] and operation != "delete"
        if allowed:
            assert status in (200, 201), (automation["id"], operation, status)
        else:
            assert status in (403, 409), (automation["id"], operation, status)


def test_no_automation_can_delete(env):
    assert all("tasks.task:delete" not in a["scopes"] for a in REGISTRY["automations"])
    reconciler = next(a for a in REGISTRY["automations"] if a["id"] == "overdue-reconciler")
    assert "tasks.archive:write" in reconciler["scopes"]


def test_revocation_mid_run_stops_the_next_call(cluster, env):
    ws, client, token = env
    item = next(i for i in provision(cluster, ws.id) if i.automation == "engagement-sync")
    headers = token(item.identity, item.delegation, item.owner)
    first = client.put("/v1/tasks/external/engagements/E-1", json={"title": "Next action"}, headers=headers)
    assert first.status_code in (200, 201)
    with cluster.connect("platform_api") as conn:
        conn.execute("SELECT set_config('app.workspace_id', %s, true)", (str(ws.id),))
        conn.execute("UPDATE platform.delegations SET revoked_at = now() WHERE id = %s", (item.delegation,))
        conn.commit()
    second = client.put("/v1/tasks/external/engagements/E-2", json={"title": "Other"}, headers=headers)
    assert second.status_code == 403 and second.json()["detail"]["reason"] == "delegation_invalid"
    retry = client.put("/v1/tasks/external/engagements/E-1", json={"title": "Next action"}, headers=headers)
    assert retry.status_code == 403
    # Re-provisioning issues a fresh delegation; the first record was not duplicated.
    renewed = next(i for i in provision(cluster, ws.id) if i.automation == "engagement-sync")
    assert renewed.renewed and renewed.delegation != item.delegation
    again = client.put("/v1/tasks/external/engagements/E-1", json={"title": "Next action"},
                       headers=token(renewed.identity, renewed.delegation, renewed.owner))
    assert again.json()["outcome"] == "unchanged"


def test_expired_delegation_is_refused(cluster, env):
    ws, client, token = env
    item = next(i for i in provision(cluster, ws.id) if i.automation == "calendar-sync")
    with cluster.connect("postgres") as conn:
        conn.execute("SELECT set_config('app.workspace_id', %s, true)", (str(ws.id),))
        conn.execute("UPDATE platform.delegations SET expires_at = now() - interval '1 second' WHERE id = %s",
                     (item.delegation,))
        conn.commit()
    task = owner_task(env)
    response = client.patch(f"/v1/tasks/{task['id']}", json={"calendar_event_id": "x"},
                            headers=token(item.identity, item.delegation, item.owner))
    # Without a valid delegation the automation cannot read the task, so it learns nothing about it.
    assert response.status_code == 404
    with cluster.connect("postgres") as conn:
        reason = conn.execute(
            "SELECT reason FROM platform.authorization_decisions WHERE delegation_id = %s"
            " ORDER BY decided_at DESC LIMIT 1", (item.delegation,)).fetchone()[0]
    assert reason == "delegation_invalid"
    renewed = next(i for i in provision(cluster, ws.id) if i.automation == "calendar-sync")
    assert renewed.renewed


def test_provisioning_is_idempotent_and_follows_scope_changes(cluster, env):
    ws, _, _ = env
    first = {i.automation: i for i in provision(cluster, ws.id)}
    second = {i.automation: i for i in provision(cluster, ws.id)}
    assert all(not i.renewed and i.delegation == first[name].delegation for name, i in second.items())
    narrowed = copy.deepcopy(REGISTRY)
    next(a for a in narrowed["automations"] if a["id"] == "sales-pipeline")["scopes"] = ["tasks.task:read"]
    third = {i.automation: i for i in provision(cluster, ws.id, narrowed)}
    assert third["sales-pipeline"].renewed and third["sales-pipeline"].scopes == ("tasks.task:read",)
    assert not third["decision-review"].renewed
    with cluster.connect("postgres") as conn:
        active = conn.execute(
            "SELECT count(*) FROM platform.delegations WHERE grantee_identity_id = %s AND revoked_at IS NULL",
            (third["sales-pipeline"].identity,)).fetchone()[0]
    assert active == 1
