"""Cutover and rollback rehearsal: schema, data, API, and writer switch.

Uses the importer fixture (tests/fixtures/sqlite_tasks_schema.sql) as the
SQLite control plane and the session cluster as PostgreSQL.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from empire_platform.cutover import backup, export_to_sqlite, freeze_sqlite, restore, set_writes, unfreeze_sqlite
from empire_platform.devdb import migrate
from empire_platform.devtoken import key_pair, mint
from empire_platform.tasks_import import run
from tasks_api.app import create_app
from tasks_api.auth import TokenVerifier
from test_tasks_import import make_source

ISSUER = "empire-cutover-test"


def snapshot(cluster, dbname: str, workspace) -> dict:
    with cluster.connect("postgres", dbname) as conn:
        conn.execute("SELECT set_config('app.workspace_id', %s, true)", (str(workspace),))
        return {
            "tasks": conn.execute("SELECT id, title, status, version FROM tasks.tasks WHERE workspace_id = %s"
                                  " ORDER BY id", (workspace,)).fetchall(),
            "links": conn.execute("SELECT task_id, relation, target_ref FROM tasks.task_links WHERE workspace_id = %s"
                                  " ORDER BY id", (workspace,)).fetchall(),
            "audit": conn.execute("SELECT count(*) FROM platform.audit_events WHERE workspace_id = %s",
                                  (workspace,)).fetchone()[0],
            "migrations": conn.execute("SELECT name, sha256 FROM public.schema_migrations ORDER BY name").fetchall(),
        }


@pytest.fixture
def rehearsal(cluster, tmp_path):
    """SQLite fixture imported into a fresh workspace (cutover done)."""
    source = make_source(tmp_path / "control-plane.db")
    slug = f"cutover-{uuid.uuid4().hex[:8]}"
    report = run(source, cluster, workspace_slug=slug, apply=True)
    assert report.ok, report.failures
    with cluster.connect("postgres") as conn:
        workspace, owner = conn.execute(
            "SELECT id, owner_identity_id FROM platform.workspaces WHERE slug = %s", (slug,)).fetchone()
        # The importer owner gets a role able to delete with approval, like the real owner.
        conn.execute("SELECT set_config('app.workspace_id', %s, false)", (str(workspace),))
        approval = conn.execute(
            "INSERT INTO platform.approvals (workspace_id, requested_action, approver_identity_id, decision, decided_at)"
            " VALUES (%s, 'tasks.task:delete', %s, 'approved', now()) RETURNING id", (workspace, owner)).fetchone()[0]
        conn.commit()
    private_pem, public_pem = key_pair(tmp_path / "keys")
    client = TestClient(create_app(lambda: cluster.connect("tasks_api"), TokenVerifier(public_pem, ISSUER)))
    headers = {"Authorization": "Bearer " + mint(private_pem, sub=owner, ws=workspace, issuer=ISSUER)}
    freeze_sqlite(source)
    yield source, workspace, client, headers, approval
    set_writes(cluster, True)


def test_restore_from_backup_recovers_schema_and_data(cluster, rehearsal, tmp_path):
    _, workspace, client, headers, _ = rehearsal
    client.post("/v1/tasks", json={"title": "Before backup"}, headers=headers)
    dump = backup(cluster, tmp_path / "empire.dump")
    expected = snapshot(cluster, "empire", workspace)

    restored = f"restore_{uuid.uuid4().hex[:8]}"
    restore(cluster, dump, restored)
    assert snapshot(cluster, restored, workspace) == expected
    # A restored database keeps working: migrations are recorded and nothing re-applies.
    assert migrate(cluster, restored) == []

    # Simulated bad schema change on a copy, undone by restoring the dump.
    damaged = f"damaged_{uuid.uuid4().hex[:8]}"
    restore(cluster, dump, damaged)
    with cluster.connect("postgres", damaged) as conn:
        conn.execute("DROP SCHEMA tasks CASCADE")
        conn.commit()
    recovered = f"recovered_{uuid.uuid4().hex[:8]}"
    restore(cluster, dump, recovered)
    assert snapshot(cluster, recovered, workspace) == expected


def test_frozen_sqlite_refuses_writers_until_rollback(rehearsal):
    source, *_ = rehearsal
    with sqlite3.connect(source) as conn:
        with pytest.raises(sqlite3.IntegrityError, match="empire-platform"):
            conn.execute("INSERT INTO tasks (title) VALUES ('Legacy writer')")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE tasks SET title = 'x' WHERE id = 1")
    unfreeze_sqlite(source)
    with sqlite3.connect(source) as conn:
        conn.execute("INSERT INTO tasks (title) VALUES ('Legacy writer')")


def test_write_switch_stops_api_writes_but_not_reads(cluster, rehearsal):
    _, _, client, headers, _ = rehearsal
    set_writes(cluster, False)
    assert client.post("/v1/tasks", json={"title": "During rollback"}, headers=headers).status_code == 503
    assert client.get("/v1/tasks", headers=headers).status_code == 200
    set_writes(cluster, True)
    assert client.post("/v1/tasks", json={"title": "After"}, headers=headers).status_code == 201


def test_full_rollback_keeps_every_write_made_after_cutover(cluster, rehearsal):
    """Cutover -> API writes -> rollback to SQLite without losing anything."""
    source, workspace, client, headers, approval = rehearsal
    tasks = {t["title"]: t for t in client.get("/v1/tasks", headers=headers).json()["items"]}
    register, long_run, shoes = tasks["Register for the marathon"], tasks["Long run 20 km"], tasks["Buy shoes"]

    new = client.post("/v1/tasks", json={"title": "Book hotel", "tags": ["travel"], "parent_task_id": register["id"]},
                      headers=headers).json()
    client.patch(f"/v1/tasks/{long_run['id']}", json={"title": "Long run 22 km", "priority": 1}, headers=headers)
    client.post(f"/v1/tasks/{register['id']}/complete", headers=headers)
    assert client.delete(f"/v1/tasks/{shoes['id']}", headers={**headers, "X-Empire-Approval": str(approval)}).status_code == 204
    client.post(f"/v1/tasks/{new['id']}/links", headers=headers,
                json={"relation": "supports-goal", "target_type": "goal", "target_ref": "empire:goals:goal:legacy-7"})
    client.post(f"/v1/tasks/{new['id']}/links", headers=headers,
                json={"relation": "supports-goal", "target_type": "goal", "target_ref": "empire:goals:goal:new-goal"})

    # Rollback: stop API writes, export, then let SQLite writers back in.
    set_writes(cluster, False)
    report = export_to_sqlite(cluster, str(workspace), source)
    unfreeze_sqlite(source)

    assert (report.inserted, report.deleted, report.links_inserted) == (1, 1, 1)
    assert report.links_unmapped and "new-goal" in report.links_unmapped[0]
    with sqlite3.connect(source) as conn:
        conn.row_factory = sqlite3.Row
        rows = {r["title"]: r for r in conn.execute("SELECT * FROM tasks")}
        assert "Buy shoes" not in rows
        assert rows["Long run 22 km"]["priority"] == 1
        assert rows["Register for the marathon"]["status"] == "done"
        assert rows["Register for the marathon"]["completed_at"] is not None
        hotel = rows["Book hotel"]
        assert json.loads(hotel["tags"]) == ["travel"] and hotel["parent_task_id"] == rows["Register for the marathon"]["id"]
        goal_links = conn.execute("SELECT goal_id FROM task_goal_links WHERE task_id = ?", (hotel["id"],)).fetchall()
        assert [tuple(r) for r in goal_links] == [(7,)]
        # Pre-cutover links are untouched.
        assert conn.execute("SELECT count(*) FROM task_project_links").fetchone()[0] == 2
        # Legacy writers work again.
        conn.execute("INSERT INTO tasks (title) VALUES ('Written by a legacy script')")


def test_export_is_all_or_nothing(cluster, rehearsal, monkeypatch):
    source, workspace, client, headers, _ = rehearsal
    client.post("/v1/tasks", json={"title": "Will not be exported"}, headers=headers)
    before = sqlite3.connect(source).execute("SELECT count(*) FROM tasks").fetchone()[0]

    import empire_platform.cutover as cutover

    def broken(task, ids):
        raise RuntimeError("disk full")

    monkeypatch.setattr(cutover, "_row_values", broken)
    with pytest.raises(RuntimeError):
        export_to_sqlite(cluster, str(workspace), source)
    with sqlite3.connect(source) as conn:
        assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == before
        # The freeze is still in place after a failed export.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO tasks (title) VALUES ('x')")


def test_tool_failures_never_echo_the_connection_string(tmp_path):
    import pytest

    from empire_platform.cutover import ToolFailed, _run_tool

    fake = tmp_path / "pg_dump"
    fake.write_text("#!/bin/sh\necho \"connection to $2 failed\" >&2\nexit 1\n")
    fake.chmod(0o755)
    secret_url = "postgresql://empire_migrator:not-a-real-password@host.example/empire?sslmode=require"  # pragma: allowlist secret
    with pytest.raises(ToolFailed) as failure:
        _run_tool([str(fake)], secret_url)
    assert "not-a-real-password" not in str(failure.value) and "<connection>" in str(failure.value)
    assert failure.value.__cause__ is None and failure.value.__context__ is None
