"""Rehearse cutover and rollback on a copy of the current Solo Empire data.

Everything runs against a throwaway PostgreSQL cluster and a copy of the
SQLite control-plane database; the live database is only read, through the
SQLite backup API. Writers act through the Solo Empire TaskStore adapter with
their own automation identities and delegations, over real HTTP.

Steps: copy -> freeze copy -> import -> provision automations -> writer
operations (including retries, scope denials, and a revocation mid-run) ->
API writes off -> export back to the SQLite copy -> unfreeze -> verify that no
task was lost, duplicated, or changed unless a writer changed it.

The report (.runtime/readiness/rehearsal.json) holds counts and outcomes only,
never task titles or other content.

    python scripts/rehearse_cutover.py
"""

from __future__ import annotations

import json
import shutil
import socket
import sqlite3
import sys
import tempfile
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import uvicorn

from empire_platform.automations import provision
from empire_platform.cutover import export_to_sqlite, freeze_sqlite, set_writes, unfreeze_sqlite
from empire_platform.devdb import Cluster, bootstrap, migrate
from empire_platform.devtoken import key_pair, mint
from empire_platform.tasks_import import DEFAULT_SQLITE, run as import_tasks
from tasks_api.app import create_app
from tasks_api.auth import TokenVerifier

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / ".runtime" / "readiness" / "rehearsal.json"
ISSUER = "empire-rehearsal"
COMPARED = ("title", "description", "status", "priority", "project", "category", "due_date", "notes",
            "external_source", "external_id", "recurrence_rule", "estimated_minutes", "calendar_event_id")


def adapter():
    utils = next((p / "infra" / "scripts" / "utils" for p in ROOT.parents
                  if (p / "infra" / "scripts" / "utils" / "task_store.py").is_file()), None)
    if utils is None:
        sys.exit("not inside a Solo Empire checkout: the TaskStore adapter is needed")
    sys.path.insert(0, str(utils))
    import task_store

    return task_store


def copy_live(target: Path) -> None:
    source = sqlite3.connect(f"file:{DEFAULT_SQLITE}?mode=ro", uri=True)
    with sqlite3.connect(target) as copy:
        source.backup(copy)
    source.close()


def snapshot(path: Path) -> dict[int, dict]:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    rows = {r["id"]: {c: r[c] for c in COMPARED} | {"archived": r["archived_at"] is not None}
            for r in conn.execute("SELECT * FROM tasks")}
    conn.close()
    return rows


def serve(app) -> tuple[str, uvicorn.Server, threading.Thread]:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.05)
    return f"http://127.0.0.1:{port}", server, thread


def refused(call) -> str | None:
    """Run a call that must fail; return the status and reason, or None when it wrongly succeeded."""
    try:
        call()
    except Exception as error:  # noqa: BLE001  (TaskStoreError or sqlite3 error)
        return f"{getattr(error, 'status', type(error).__name__)}:{getattr(error, 'reason', 'refused')}"
    return None


def main() -> int:
    ts = adapter()
    report: dict = {"generated": datetime.now(timezone.utc).isoformat(timespec="seconds"), "checks": {}}
    checks = report["checks"]
    root = Path(tempfile.mkdtemp(prefix="ep-rh-"))
    cluster = Cluster(root / "data", root / "sock")
    server = None
    try:
        cluster.init()
        cluster.start()
        bootstrap(cluster)
        migrate(cluster)

        copy = root / "control-plane-copy.db"
        copy_live(copy)
        before = snapshot(copy)
        report["source_tasks"] = len(before)

        # Cutover
        freeze_sqlite(copy)
        legacy = ts.SqliteTaskStore(sqlite3.connect(copy, isolation_level=None))
        checks["frozen_sqlite_refuses_legacy_writer"] = refused(lambda: legacy.create({"title": "x"})) is not None
        imported = import_tasks(copy, cluster, workspace_slug="owner", apply=True)
        checks["import_ok"] = imported.ok
        report["import"] = {"counts": imported.counts, "anomalies": len(imported.anomalies),
                            "field_mismatches": len(imported.field_mismatches), "failures": len(imported.failures)}
        with cluster.connect("postgres") as conn:
            workspace = conn.execute("SELECT id FROM platform.workspaces WHERE slug = 'owner'").fetchone()[0]
        automations = {p.automation: p for p in provision(cluster, workspace)}
        report["automations"] = {name: list(p.scopes) for name, p in automations.items()}

        private_pem, public_pem = key_pair(root / "keys")
        app = create_app(lambda: cluster.connect("tasks_api"), TokenVerifier(public_pem, ISSUER))
        url, server, thread = serve(app)

        def store(name: str) -> object:
            p = automations[name]
            token = mint(private_pem, sub=p.identity, ws=workspace, delegation=p.delegation, obo=p.owner,
                         issuer=ISSUER)
            return ts.ApiTaskStore(url, token, channel="workflow")

        # Read parity on real data: every reader sees the same thing through either backend.
        reader = store("governance-reader")
        legacy_reader = ts.SqliteTaskStore(sqlite3.connect(f"file:{copy}?mode=ro", uri=True))
        reads = {
            "tasks": lambda s: s.list(include_archived=True),
            "open_tasks": lambda s: s.list(status=["todo", "in_progress", "blocked"]),
            "stats_status": lambda s: s.stats(group_by="status", include_archived=True),
            "stats_project": lambda s: s.stats(group_by="project"),
            "links": lambda s: s.links(),
            "assignments": lambda s: s.assignments(),
            "role_suggestions": lambda s: [{k: v for k, v in r.items() if k != "updated_at"}
                                           for r in s.role_suggestions()],
            "task_types": lambda s: [{k: v for k, v in t.items() if k != "id"} for t in s.task_types()],
        }
        volatile = {"created_at", "updated_at", "completed_at", "archived_at", "reminder_at", "started_at"}

        def comparable(value):
            if isinstance(value, list):
                return [comparable(v) for v in value]
            if isinstance(value, dict):
                return {k: (bool(v) if k in volatile else comparable(v)) for k, v in value.items()}
            return value

        differ = {}
        for name, read in reads.items():
            a, b = comparable(read(legacy_reader)), comparable(read(reader))
            if a != b:
                differ[name] = sorted({k for x, y in zip(a, b) if isinstance(x, dict) for k in x if x[k] != y.get(k)}
                                      if isinstance(a, list) and len(a) == len(b) else ["count"])
        report["read_parity_differences"] = differ
        checks["read_parity_on_current_data"] = not differ

        changed: set[int] = set()
        today = date.today()

        # Writer operations, each with its own identity.
        engagement = store("engagement-sync")
        results = [engagement.upsert_external("rehearsal", "E-1", {"title": "Rehearsal next action", "priority": 2})[0]
                   for _ in range(3)]
        checks["upsert_retry_creates_once"] = results == ["created", "unchanged", "unchanged"]
        created_number = engagement.find_external("rehearsal", "E-1")["id"]
        checks["new_number_continues_sqlite_ids"] = created_number > max(before)

        calendar = store("calendar-sync")
        missing = calendar.list(missing_calendar_event=True)
        if missing:
            calendar.update(missing[0]["id"], {"calendar_event_id": "rehearsal-event"})
            changed.add(missing[0]["id"])
            checks["calendar_cannot_change_title"] = refused(
                lambda: calendar.update(missing[0]["id"], {"title": "nope"})) is not None

        reconciler = store("overdue-reconciler")
        overdue = reconciler.list(status=["todo"], due_before=today.isoformat())
        if overdue:
            reconciler.update(overdue[0]["id"], {"due_date": (today + timedelta(days=7)).isoformat()})
            changed.add(overdue[0]["id"])
        done = [t for t in reconciler.list(status=["done"]) if t["id"] not in changed]
        if done:
            reconciler.archive(done[0]["id"], "rehearsal: completed more than 7 days ago")
            changed.add(done[0]["id"])
        checks["reconciler_cannot_create"] = refused(lambda: reconciler.create({"title": "nope"})) is not None

        recurring = store("recurring-gen")
        parents = [t for t in recurring.list() if t["recurrence_rule"] and not t["is_recurring_instance"]]
        occurrences: list[int] = []
        if parents:
            until = (today + timedelta(days=7)).isoformat()
            occurrences = recurring.generate_occurrences(parents[0]["id"], until)
            checks["occurrence_retry_creates_nothing"] = recurring.generate_occurrences(parents[0]["id"], until) == []
        report["occurrences_created"] = len(occurrences)

        reminders = store("reminder-worker")
        slot = f"{today.isoformat()} 09:00:00"
        delivered = [reminders.record_reminder_delivered(created_number, slot, "line") for _ in range(2)]
        checks["reminder_delivered_once"] = delivered == [True, False]

        # Revocation mid-run: the next call of that automation is refused.
        with cluster.connect("platform_api") as conn:
            conn.execute("SELECT set_config('app.workspace_id', %s, true)", (str(workspace),))
            conn.execute("UPDATE platform.delegations SET revoked_at = now() WHERE id = %s",
                         (automations["engagement-sync"].delegation,))
            conn.commit()
        checks["revoked_automation_refused_next_call"] = refused(
            lambda: engagement.upsert_external("rehearsal", "E-2", {"title": "after revoke"})) is not None

        # Rollback
        set_writes(cluster, False)
        checks["write_switch_off_refuses_writes"] = (refused(lambda: reconciler.archive(created_number, "x"))
                                                     or "").startswith("503")
        exported = export_to_sqlite(cluster, str(workspace), copy)
        unfreeze_sqlite(copy)
        report["export"] = {"updated": exported.updated, "inserted": exported.inserted, "deleted": exported.deleted,
                            "links_inserted": exported.links_inserted, "links_unmapped": len(exported.links_unmapped)}
        after = snapshot(copy)
        expected_new = 1 + len(occurrences)
        checks["no_task_lost"] = set(before) <= set(after)
        checks["no_duplicate_or_extra_task"] = len(after) == len(before) + expected_new
        checks["new_tasks_keep_their_numbers"] = all(
            str(n) in map(str, exported.new_legacy_ids.values()) for n in [created_number, *occurrences])
        untouched = [n for n in before if n not in changed]
        drift = {c for n in untouched for c in COMPARED + ("archived",)
                 if str(before[n][c] if c != "archived" else before[n][c]) != str(after[n][c])}
        report["untouched_tasks"] = len(untouched)
        report["untouched_columns_that_differ"] = sorted(drift)
        checks["writer_changes_survive_rollback"] = all(after[n] != before[n] for n in changed)
        checks["legacy_writer_works_after_rollback"] = refused(
            lambda: ts.SqliteTaskStore(sqlite3.connect(copy, isolation_level=None)).create({"title": "x"})) is None
    finally:
        if server is not None:
            server.should_exit = True
        cluster.stop()
        shutil.rmtree(root, ignore_errors=True)

    report["ok"] = all(checks.values()) and not report.get("untouched_columns_that_differ")
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
