"""Solo Empire's TaskStore adapter behaves the same on SQLite and on the Tasks API.

The adapter lives in the Solo Empire repository (infra/scripts/utils/task_store.py);
this test runs one writer scenario against both backends over real HTTP and
compares the outcomes. Skipped when this checkout is not inside Solo Empire.
"""

from __future__ import annotations

import socket
import sqlite3
import sys
import threading
import time
import uuid
from pathlib import Path

import pytest
import uvicorn

from conftest import create_workspace
from empire_platform.devtoken import key_pair, mint
from empire_platform.permissions import TASKS_OWNER
from tasks_api.app import create_app
from tasks_api.auth import TokenVerifier

ADAPTER_DIR = next((p / "infra" / "scripts" / "utils" for p in Path(__file__).resolve().parents
                    if (p / "infra" / "scripts" / "utils" / "task_store.py").is_file()), None)
pytestmark = pytest.mark.skipif(ADAPTER_DIR is None, reason="not inside a Solo Empire checkout")
SCHEMA = Path(__file__).parent / "fixtures" / "sqlite_tasks_schema.sql"
ISSUER = "empire-store-contract"


@pytest.fixture(scope="module")
def task_store():
    sys.path.insert(0, str(ADAPTER_DIR))
    import task_store as module

    yield module
    sys.path.remove(str(ADAPTER_DIR))


@pytest.fixture(scope="module")
def api_url(cluster, tmp_path_factory):
    private_pem, public_pem = key_pair(tmp_path_factory.mktemp("store-keys"))
    app = create_app(lambda: cluster.connect("tasks_api"), TokenVerifier(public_pem, ISSUER))
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}", private_pem
    server.should_exit = True
    thread.join(timeout=5)


def scenario(store) -> dict:
    """What a writer does, through the store only. Returns backend-neutral results."""
    out: dict = {}
    out["upsert"] = [
        store.upsert_external("engagements", "E-1", {"title": "Next action", "priority": 2, "tags": "client,oak",
                                                     "due_date": "2026-10-20"})[0],
        store.upsert_external("engagements", "E-1", {"title": "Next action", "priority": 2, "tags": "client,oak",
                                                     "due_date": "2026-10-20"})[0],
        store.upsert_external("engagements", "E-1", {"title": "Next action", "priority": 1})[0],
    ]
    found = store.find_external("engagements", "E-1")
    out["found"] = (found["title"], found["priority"], found["tags"], found["due_date"], found["status"])
    out["missing"] = store.find_external("engagements", "nope")

    weekly = store.create({"title": "Weekly run", "recurrence_rule": "weekly", "due_date": "2026-10-01"})
    first = store.generate_occurrences(weekly, "2026-10-15")
    out["occurrences"] = [(store.get(n)["title"], store.get(n)["due_date"]) for n in first]
    out["occurrences_retry"] = store.generate_occurrences(weekly, "2026-10-15")

    reminder = store.create({"title": "Stretch", "reminder_at": "2026-10-10 09:00:00"})
    out["reminders_due"] = [t["title"] for t in store.list(reminder_due_before="2026-10-10 10:00:00")]
    out["reminder"] = [store.record_reminder_delivered(reminder, "2026-10-10 09:00:00", "line"),
                       store.record_reminder_delivered(reminder, "2026-10-10 09:00:00", "line")]
    out["reminders_due_after"] = [t["title"] for t in store.list(reminder_due_before="2026-10-10 10:00:00")]

    moved = store.create({"title": "Overdue", "due_date": "2026-09-01"})
    out["overdue"] = [t["title"] for t in store.list(status=["todo"], due_before="2026-10-01")]
    store.update(moved, {"due_date": "2026-11-01"})
    cancelled = store.create({"title": "Gone upstream"})
    store.update(cancelled, {"status": "cancelled"})
    done = store.create({"title": "Finished"})
    store.complete(done)
    store.update(store.create({"title": "Calendar"}), {"calendar_event_id": "cal-9"})
    old = store.create({"title": "Old done"})
    store.complete(old)
    store.archive(old, "older than 30 days")
    restored = store.create({"title": "Archived by mistake"})
    store.archive(restored, "mistake")
    store.unarchive(restored)

    def view(task):
        return (task["title"], task["status"], task["due_date"], bool(task["completed_at"]), task["calendar_event_id"],
                bool(task["archived_at"]))

    out["active"] = sorted(view(t) for t in store.list())
    out["with_archived"] = sorted(view(t) for t in store.list(include_archived=True))
    out["missing_calendar"] = sorted(t["title"] for t in store.list(missing_calendar_event=True))
    return out


def test_sqlite_and_api_backends_agree(task_store, api_url, cluster, tmp_path):
    db = sqlite3.connect(tmp_path / "legacy.db", isolation_level=None)
    db.executescript(SCHEMA.read_text(encoding="utf-8"))
    legacy = scenario(task_store.SqliteTaskStore(db))

    url, private_pem = api_url
    with cluster.connect("platform_api") as conn:
        ws = create_workspace(conn, f"store-{uuid.uuid4().hex[:8]}", list(TASKS_OWNER))
        conn.commit()
    token = mint(private_pem, sub=ws.owner, ws=ws.id, issuer=ISSUER)
    api = scenario(task_store.ApiTaskStore(url, token, channel="cli"))

    assert legacy["upsert"] == ["created", "unchanged", "updated"]
    assert legacy["found"] == ("Next action", 1, ["client", "oak"], "2026-10-20", "todo")
    assert legacy["occurrences"] == [("[R1] Weekly run", "2026-10-08"), ("[R2] Weekly run", "2026-10-15")]
    assert legacy["reminder"] == [True, False] and legacy["reminders_due_after"] == []
    for key in legacy:
        assert api[key] == legacy[key], key


def test_api_backend_reports_permission_errors(task_store, api_url, cluster):
    url, private_pem = api_url
    with cluster.connect("platform_api") as conn:
        ws = create_workspace(conn, f"store-deny-{uuid.uuid4().hex[:8]}", ["tasks.task:read"])
        conn.commit()
    store = task_store.ApiTaskStore(url, mint(private_pem, sub=ws.owner, ws=ws.id, issuer=ISSUER))
    with pytest.raises(task_store.TaskStoreError) as error:
        store.create({"title": "x"})
    assert error.value.status == 403 and error.value.reason == "no_permission"


def test_store_rejects_unknown_fields(task_store, tmp_path):
    db = sqlite3.connect(tmp_path / "legacy.db")
    db.executescript(SCHEMA.read_text(encoding="utf-8"))
    with pytest.raises(ValueError, match="sync_status"):
        task_store.SqliteTaskStore(db).create({"title": "x", "sync_status": "synced"})
