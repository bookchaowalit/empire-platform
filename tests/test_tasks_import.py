"""SQLite -> PostgreSQL tasks import: dry run, verification, and source safety."""

from __future__ import annotations

import hashlib
import sqlite3
import uuid
from pathlib import Path

import pytest

from empire_platform.tasks_import import run, snapshot

SCHEMA = Path(__file__).parent / "fixtures" / "sqlite_tasks_schema.sql"


def make_source(path: Path, *, extra: str = "") -> Path:
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA.read_text(encoding="utf-8"))
    conn.executescript(
        """
        INSERT INTO tasks (id, title, status, priority, tags, due_date, created_at, updated_at, parent_task_id)
        VALUES (1, 'Register for the marathon', 'todo', 2, 'running,goal', '2026-10-20', '2026-10-01 08:38:00', '2026-10-04T16:47:13', NULL),
               (2, 'Long run 20 km', 'in_progress', NULL, '["running"]', NULL, '2026-10-02T06:00:00', NULL, 1),
               (3, 'Buy shoes', 'done', 4, NULL, '2026-10-05 00:00:00', '2026-10-03 10:00:00', '2026-10-03 10:00:00', NULL);
        UPDATE tasks SET completed_at = '2026-10-04 09:00:00' WHERE id = 3;
        INSERT INTO task_types (id, name, allowed_statuses) VALUES (1, 'Standard', '["todo","done"]');
        INSERT INTO task_assignments (id, task_id, assignee_type, assignee_id) VALUES (1, 1, 'role', 'running-coach');
        INSERT INTO task_goal_links (id, task_id, goal_id, source) VALUES (1, 1, 7, 'manual');
        INSERT INTO task_project_links (id, task_id, project_id, relation_type, source) VALUES (1, 1, 3, 'delivery', 'manual'),
                                                                                         (2, 2, 3, 'supporting', 'manual');
        INSERT INTO task_context_links (id, task_id, context_id, source) VALUES (1, 2, 9, 'manual');
        INSERT INTO task_skill_links (id, task_id, skill_id, match_reason) VALUES (1, 3, 5, 'gear');
        INSERT INTO task_role_suggestions (id, task_id, role_id, confidence, match_reason) VALUES (1, 1, 'coach', 'high', 'running');
        INSERT INTO work_item_task_links (task_id, work_item_id) VALUES (1, 1);
        """
        + extra
    )
    conn.commit()
    conn.close()
    return path


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def superuser_count(cluster, table: str) -> int:
    with cluster.connect("postgres") as conn:
        return conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


def test_dry_run_verifies_everything_and_writes_nothing(cluster, tmp_path):
    source = make_source(tmp_path / "source.db")
    before = digest(source)
    tasks_before = superuser_count(cluster, "tasks.tasks")

    report = run(source, cluster, workspace_slug=f"dry-{uuid.uuid4().hex[:8]}", apply=False)

    assert report.ok, report.failures
    assert report.counts["tasks"] == {"source": 3, "target": 3}
    assert report.counts["task_project_links"] == {"source": 2, "target": 2}
    assert report.field_mismatches == []
    assert report.inbound_references["work_item_task_links.task_id"] == {"references": 1, "unresolved": 0}
    assert any("comma-separated" in note for note in report.anomalies)
    assert any("missing priority" in note for note in report.anomalies)
    assert digest(source) == before
    assert superuser_count(cluster, "tasks.tasks") == tasks_before


def test_apply_local_commits_once(cluster, tmp_path):
    source = make_source(tmp_path / "source.db")
    slug = f"apply-{uuid.uuid4().hex[:8]}"
    report = run(source, cluster, workspace_slug=slug, apply=True)
    assert report.ok, report.failures
    with cluster.connect("postgres") as conn:
        workspace = conn.execute("SELECT id FROM platform.workspaces WHERE slug = %s", (slug,)).fetchone()[0]
        rows = conn.execute(
            "SELECT legacy_id, status, priority, tags, parent_task_id IS NOT NULL, completed_at IS NOT NULL"
            " FROM tasks.tasks WHERE workspace_id = %s ORDER BY legacy_id", (workspace,)
        ).fetchall()
        assert rows == [
            (1, "todo", 2, ["running", "goal"], False, False),
            (2, "in_progress", 3, ["running"], True, False),
            (3, "done", 4, [], False, True),
        ]
        links = dict(conn.execute(
            "SELECT legacy_table || ':' || legacy_id, relation || ' ' || target_ref FROM tasks.task_links WHERE workspace_id = %s",
            (workspace,),
        ).fetchall())
        assert links["task_goal_links:1"] == "supports-goal empire:goals:goal:legacy-7"
        assert links["task_project_links:1"] == "belongs-to empire:projects:project:legacy-3"
        assert links["task_project_links:2"] == "related-to empire:projects:project:legacy-3"
        audit = conn.execute(
            "SELECT count(*) FROM platform.audit_events WHERE workspace_id = %s AND action = 'tasks.import'", (workspace,)
        ).fetchone()[0]
        assert audit == 1
    with pytest.raises(Exception):
        run(source, cluster, workspace_slug=slug, apply=True)


def test_unresolved_inbound_reference_fails_and_rolls_back(cluster, tmp_path):
    source = make_source(tmp_path / "source.db", extra="INSERT INTO work_item_task_links (task_id, work_item_id) VALUES (99, 2);")
    tasks_before = superuser_count(cluster, "tasks.tasks")
    report = run(source, cluster, workspace_slug=f"bad-{uuid.uuid4().hex[:8]}", apply=True)
    assert not report.ok
    assert any("work_item_task_links.task_id: 1 references" in failure for failure in report.failures)
    assert superuser_count(cluster, "tasks.tasks") == tasks_before


def test_unparseable_timestamp_fails(cluster, tmp_path):
    source = make_source(tmp_path / "source.db", extra="UPDATE tasks SET started_at = 'yesterday' WHERE id = 1;")
    report = run(source, cluster, workspace_slug=f"ts-{uuid.uuid4().hex[:8]}", apply=False)
    assert not report.ok
    assert any("started_at" in failure for failure in report.failures)


def test_source_handle_is_read_only(tmp_path):
    source = make_source(tmp_path / "source.db")
    handle = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    with pytest.raises(sqlite3.OperationalError):
        handle.execute("DELETE FROM tasks")
    handle.close()
    memory = snapshot(source)
    assert memory.execute("SELECT count(*) FROM tasks").fetchone()[0] == 3
