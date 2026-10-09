"""Daily check: least-privilege monitor role and each rollback signal."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import psycopg
import pytest

from empire_platform.monitor import evaluate, run, snapshot
from graph_world import apps, bearer, gkeys, gtoken, gworld, run_publisher  # noqa: F401  (fixtures)


def superuser(cluster, query, params=()):
    with cluster.connect("postgres") as conn:
        result = conn.execute(query, params)
        return result.fetchall() if result.description else None


def test_monitor_role_cannot_read_content_or_write(cluster):
    with cluster.connect("platform_monitor") as conn:
        for query in ("SELECT title FROM tasks.tasks", "SELECT payload FROM tasks.outbox",
                      "SELECT last_error FROM tasks.outbox", "SELECT label FROM graph.nodes",
                      "UPDATE tasks.settings SET value = 'false'"):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(query)
            conn.rollback()


def test_snapshot_sees_every_workspace(cluster, apps, gworld, gtoken):
    tasks, _ = apps
    tasks.post("/v1/tasks", json={"title": "Counted"}, headers=bearer(gtoken(gworld.owner, gworld.ws)))
    snap = snapshot(cluster)
    assert str(gworld.ws) in snap["workspaces"]
    assert snap["workspaces"][str(gworld.ws)]["tasks"] >= 1
    assert snap["writes_enabled"] is True


def base(**workspaces):
    return {"workspaces": workspaces, "outbox": {"oldest_age_seconds": 0, "max_attempts": 0, "graph_lagging": 0},
            "writes_enabled": True, "workspace_mismatch_denials_24h": 0}


def checks(findings):
    return {(f.check, f.level) for f in findings}


def test_lost_tasks_breach_but_recorded_deletions_do_not():
    now = datetime.now(timezone.utc)
    before = base(w={"tasks": 10, "distinct_numbers": 10, "deleted_events": 0})
    lost = base(w={"tasks": 8, "distinct_numbers": 8, "deleted_events": 1})
    deleted = base(w={"tasks": 9, "distinct_numbers": 9, "deleted_events": 1})
    assert ("tasks_lost", "breach") in checks(evaluate(lost, before, None, None, now)[0])
    assert evaluate(deleted, before, None, None, now)[0] == []
    assert ("workspace_missing", "breach") in checks(evaluate(base(), before, None, None, now)[0])


def test_outbox_api_and_switch_signals():
    now = datetime.now(timezone.utc)
    snap = base()
    snap["outbox"].update(oldest_age_seconds=20 * 60, max_attempts=4, graph_lagging=2)
    snap["writes_enabled"] = False
    found = checks(evaluate(snap, None, True, None, now)[0])
    assert {("outbox_backlog", "warn"), ("outbox_retries", "warn"), ("graph_lag", "warn"),
            ("writes_disabled", "warn")} <= found
    snap["outbox"]["oldest_age_seconds"] = 2 * 3600
    assert ("outbox_backlog", "breach") in checks(evaluate(snap, None, True, None, now)[0])
    first, since = evaluate(base(), None, False, None, now)
    assert ("api_down", "warn") in checks(first) and since
    later, _ = evaluate(base(), None, False, (now - timedelta(minutes=31)).isoformat(), now)
    assert ("api_down", "breach") in checks(later)
    _, cleared = evaluate(base(), None, True, since, now)
    assert cleared is None


def test_run_keeps_state_between_days(cluster, tmp_path, apps, gworld, gtoken):
    run_publisher(cluster)
    first = run(cluster, None, tmp_path)
    assert first["level"] in ("ok", "warn")
    # A task vanishing without a delete event is the signal the owner rolls back on.
    tasks, _ = apps
    made = tasks.post("/v1/tasks", json={"title": f"Vanishes {uuid.uuid4().hex[:6]}"},
                      headers=bearer(gtoken(gworld.owner, gworld.ws))).json()
    run_publisher(cluster)
    run(cluster, None, tmp_path)
    superuser(cluster, "DELETE FROM tasks.tasks WHERE id = %s", (made["id"],))
    after = run(cluster, None, tmp_path)
    assert after["level"] == "breach"
    assert any(f["check"] == "tasks_lost" and f["workspace"] == str(gworld.ws) for f in after["findings"])
