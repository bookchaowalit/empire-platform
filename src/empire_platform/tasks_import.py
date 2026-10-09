"""Import the tasks domain from the Solo Empire SQLite control plane.

The source is never written: it is opened read-only (``mode=ro`` and
``query_only``) and copied to an in-memory snapshot with the SQLite backup
API, so concurrent writers cannot change the data mid-import.

By default this is a dry run: everything happens in one PostgreSQL
transaction that is verified and then rolled back. ``--apply`` commits, and
only into the local development cluster.

Checks: row counts per table, referential integrity inside the cell, soft
references from other SQLite domains into tasks, and field-by-field equality
of every imported task after normalisation.

    empire-tasks-import [--sqlite PATH] [--workspace SLUG] [--apply] [--report FILE]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import psycopg

from empire_platform.devdb import Cluster, default_cluster

def _default_sqlite() -> Path:
    """The control-plane database of the Solo Empire checkout that contains this repo."""
    override = os.environ.get("SOLO_EMPIRE_ROOT")
    candidates = [Path(override)] if override else list(Path(__file__).resolve().parents)
    for root in candidates:
        path = root / "infra" / "database" / "solo-empire.db"
        if path.is_file():
            return path
    return Path("infra/database/solo-empire.db")


DEFAULT_SQLITE = _default_sqlite()
DROPPED_COLUMNS = ("sync_status", "last_synced_at", "md_hash")
TASK_COLUMNS = (
    "id", "title", "description", "status", "priority", "project", "tags", "category", "due_date",
    "completed_at", "external_id", "external_source", "external_url", "notes", "created_at",
    "updated_at", "reminder_at", "reminder_sent", "reminder_channels", "estimated_minutes",
    "actual_minutes", "started_at", "recurrence_rule", "is_recurring_instance",
    "recurrence_occurrence", "parent_recurring_id", "archived_at", "archive_reason",
    "parent_task_id", "calendar_event_id", "progress_percent",
) + DROPPED_COLUMNS
TIMESTAMP_COLUMNS = ("completed_at", "created_at", "updated_at", "reminder_at", "started_at", "archived_at")
# Tables imported into the tasks cell; every other SQLite table whose foreign
# key points at tasks keeps the legacy ID as a soft reference after cutover
# (phase 1 plan §3.3) and is checked for unresolved references.
CELL_TABLES = {
    "tasks", "task_types", "task_assignments", "task_dependencies", "task_comments", "task_attachments",
    "task_goal_links", "task_project_links", "task_context_links", "task_skill_links", "task_role_suggestions",
}
LINK_SOURCES = {
    "task_goal_links": ("goal_id", "goal", "goals:goal"),
    "task_project_links": ("project_id", "project", "projects:project"),
    "task_context_links": ("context_id", "context", "projects:context"),
    "task_skill_links": ("skill_id", "skill", "roles-skills:skill"),
}


@dataclass
class Report:
    mode: str
    source: str
    snapshot_sha256: str = ""
    counts: dict[str, dict[str, int]] = field(default_factory=dict)
    anomalies: list[str] = field(default_factory=list)
    dropped_columns: dict[str, int] = field(default_factory=dict)
    inbound_references: dict[str, dict[str, int]] = field(default_factory=dict)
    field_mismatches: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failures


def snapshot(path: Path) -> sqlite3.Connection:
    """Read-only, point-in-time copy of the source database in memory."""
    source = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    source.execute("PRAGMA query_only = ON")
    memory = sqlite3.connect(":memory:")
    source.backup(memory)
    source.close()
    memory.row_factory = sqlite3.Row
    return memory


def snapshot_digest(conn: sqlite3.Connection) -> str:
    digest = hashlib.sha256()
    for line in conn.iterdump():
        digest.update(line.encode())
    return digest.hexdigest()


def table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)).fetchone() is not None


def parse_timestamp(value: Any, column: str, report: Report, row_id: Any) -> datetime | None:
    if value in (None, ""):
        return None
    text = str(value).strip().replace("Z", "+00:00")
    for candidate in (text, text.replace(" ", "T", 1)):
        try:
            parsed = datetime.fromisoformat(candidate)
            break
        except ValueError:
            continue
    else:
        report.failures.append(f"tasks[{row_id}].{column}: unparseable timestamp {value!r}")
        return None
    if parsed.tzinfo is None:  # SQLite CURRENT_TIMESTAMP is UTC
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def parse_date(value: Any, report: Report, row_id: Any) -> date | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        report.failures.append(f"tasks[{row_id}].due_date: unparseable date {value!r}")
        return None


def parse_tags(value: Any) -> list[str]:
    if value in (None, ""):
        return []
    text = str(value).strip()
    if text.startswith("["):
        return [str(tag).strip() for tag in json.loads(text) if str(tag).strip()]
    return [tag.strip() for tag in text.split(",") if tag.strip()]


def normalise_task(row: sqlite3.Row, report: Report) -> dict[str, Any]:
    """The value each target column must hold for this source row."""
    task_id = row["id"]
    values: dict[str, Any] = {
        "legacy_id": task_id,
        "title": row["title"],
        "description": row["description"],
        "status": row["status"] or "todo",
        "priority": row["priority"] if row["priority"] is not None else 3,
        "project": row["project"],
        "tags": parse_tags(row["tags"]),
        "category": row["category"],
        "due_date": parse_date(row["due_date"], report, task_id),
        "external_id": row["external_id"],
        "external_source": row["external_source"],
        "external_url": row["external_url"],
        "notes": row["notes"],
        "reminder_sent": bool(row["reminder_sent"]),
        "reminder_channels": row["reminder_channels"],
        "estimated_minutes": row["estimated_minutes"],
        "actual_minutes": row["actual_minutes"] or 0,
        "recurrence_rule": row["recurrence_rule"],
        "is_recurring_instance": bool(row["is_recurring_instance"]),
        "recurrence_occurrence": row["recurrence_occurrence"],
        "archive_reason": row["archive_reason"],
        "calendar_event_id": row["calendar_event_id"],
        "progress_percent": row["progress_percent"] or 0,
    }
    for column in TIMESTAMP_COLUMNS:
        values[column] = parse_timestamp(row[column], column, report, task_id)
    if values["created_at"] is None:
        values["created_at"] = datetime.now(timezone.utc)
        report.anomalies.append(f"tasks[{task_id}]: missing created_at, set to import time")
    if values["updated_at"] is None:
        values["updated_at"] = values["created_at"]
    if row["priority"] is None:
        report.anomalies.append(f"tasks[{task_id}]: missing priority, set to 3")
    if row["tags"] and not str(row["tags"]).strip().startswith("["):
        report.anomalies.append(f"tasks[{task_id}]: tags stored as comma-separated text, converted to an array")
    return values


def ensure_workspace(pg: psycopg.Connection, slug: str) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    """New workspace, its owner, an importer automation, and the owner's delegation to it.

    Returns (workspace, owner, delegation, importer). A second ``--apply`` with
    the same slug fails on the unique slug instead of importing twice.
    """
    workspace_id = pg.execute("SELECT platform.uuid_v7()").fetchone()[0]
    owner_id = pg.execute("SELECT platform.uuid_v7()").fetchone()[0]
    importer_id = pg.execute("SELECT platform.uuid_v7()").fetchone()[0]
    pg.execute("SELECT set_config('app.workspace_id', %s, true)", (str(workspace_id),))
    pg.execute("INSERT INTO platform.identities (id, kind, display_name) VALUES (%s, 'human', 'workspace owner')", (owner_id,))
    pg.execute("INSERT INTO platform.identities (id, kind, display_name) VALUES (%s, 'automation', 'tasks importer')", (importer_id,))
    pg.execute(
        "INSERT INTO platform.workspaces (id, slug, name, owner_identity_id) VALUES (%s, %s, %s, %s)",
        (workspace_id, slug, slug, owner_id),
    )
    role_id = pg.execute("SELECT platform.uuid_v7()").fetchone()[0]
    pg.execute("INSERT INTO platform.memberships (workspace_id, identity_id) VALUES (%s, %s), (%s, %s)",
               (workspace_id, owner_id, workspace_id, importer_id))
    pg.execute("INSERT INTO platform.roles (id, workspace_id, name) VALUES (%s, %s, 'owner')", (role_id, workspace_id))
    for permission in ("tasks.task:read", "tasks.task:write", "tasks.task:delete"):
        pg.execute("INSERT INTO platform.role_permissions (workspace_id, role_id, permission) VALUES (%s, %s, %s)",
                   (workspace_id, role_id, permission))
    pg.execute("INSERT INTO platform.membership_roles (workspace_id, identity_id, role_id) VALUES (%s, %s, %s)",
               (workspace_id, owner_id, role_id))
    delegation_id = pg.execute("SELECT platform.uuid_v7()").fetchone()[0]
    pg.execute(
        "INSERT INTO platform.delegations (id, workspace_id, grantor_identity_id, grantee_identity_id, scopes, expires_at)"
        " VALUES (%s, %s, %s, %s, ARRAY['tasks.task:write']::platform.permission[], now() + interval '1 hour')",
        (delegation_id, workspace_id, owner_id, importer_id),
    )
    return workspace_id, owner_id, delegation_id, importer_id


def run(source_path: Path, cluster: Cluster, *, workspace_slug: str, apply: bool) -> Report:
    report = Report(mode="apply-local" if apply else "dry-run", source=str(source_path))
    src = snapshot(source_path)
    report.snapshot_sha256 = snapshot_digest(src)

    with cluster.connect("empire_migrator") as pg:
        workspace_id, owner_id, delegation_id, importer_id = ensure_workspace(pg, workspace_slug)
        execution_id = pg.execute(
            "SELECT platform.begin_execution(%s, %s, %s, %s, NULL, 'cli', %s)",
            (owner_id, importer_id, owner_id, delegation_id, f"tasks-import-{report.snapshot_sha256[:16]}"),
        ).fetchone()[0]

        # tasks
        rows = src.execute(f"SELECT {', '.join(TASK_COLUMNS)} FROM tasks ORDER BY id").fetchall()
        for column in DROPPED_COLUMNS:
            report.dropped_columns[column] = sum(
                1 for row in rows if row[column] not in (None, "", "local")
            )
        ids: dict[int, uuid.UUID] = {}
        expected: dict[int, dict[str, Any]] = {}
        for row in rows:
            ids[row["id"]] = pg.execute("SELECT platform.uuid_v7()").fetchone()[0]
            expected[row["id"]] = normalise_task(row, report)
        for row in rows:
            values = dict(expected[row["id"]])
            for parent in ("parent_recurring_id", "parent_task_id"):
                legacy = row[parent]
                if legacy is not None and legacy not in ids:
                    report.failures.append(f"tasks[{row['id']}].{parent}: missing parent {legacy}")
                values[parent] = ids.get(legacy) if legacy is not None else None
            values.update(id=ids[row["id"]], workspace_id=workspace_id,
                          created_by_execution=execution_id, updated_by_execution=execution_id)
            columns = list(values)
            pg.execute(
                f"INSERT INTO tasks.tasks ({', '.join(columns)}) VALUES ({', '.join(['%s'] * len(columns))})",
                [values[c] for c in columns],
            )
        # The update trigger is not involved on insert, so version stays 1.
        report.counts["tasks"] = {"source": len(rows), "target": 0}

        def link_target(table: str, legacy_task: int) -> uuid.UUID | None:
            target = ids.get(legacy_task)
            if target is None:
                report.failures.append(f"{table}: task {legacy_task} not found")
            return target

        # task_types
        types = src.execute("SELECT * FROM task_types ORDER BY id").fetchall()
        for row in types:
            pg.execute(
                "INSERT INTO tasks.task_types (workspace_id, legacy_id, name, description, icon, color, default_status,"
                " allowed_statuses, custom_template, is_active, sort_order, created_at)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (workspace_id, row["id"], row["name"], row["description"], row["icon"], row["color"],
                 row["default_status"] or "todo", json.loads(row["allowed_statuses"] or "[]"),
                 json.dumps(json.loads(row["custom_template"])) if row["custom_template"] else None,
                 bool(row["is_active"]), row["sort_order"] or 0,
                 parse_timestamp(row["created_at"], "created_at", report, row["id"]) or datetime.now(timezone.utc)),
            )
        report.counts["task_types"] = {"source": len(types), "target": 0}

        # assignments (legacy rows are role assignments)
        assignments = src.execute("SELECT * FROM task_assignments ORDER BY id").fetchall()
        for row in assignments:
            task = link_target("task_assignments", row["task_id"])
            if task is None:
                continue
            kind = {"user": "identity", "team": "team", "role": "role"}[row["assignee_type"] or "user"]
            if kind == "identity":
                report.anomalies.append(f"task_assignments[{row['id']}]: user assignee kept as a soft reference")
            pg.execute(
                "INSERT INTO tasks.task_assignments (workspace_id, task_id, assignee_type, assignee_ref, assignee_name,"
                " assignment_type, legacy_id, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (workspace_id, task, kind, row["assignee_id"], row["assignee_name"], row["assignment_type"] or "assigned",
                 row["id"], parse_timestamp(row["created_at"], "created_at", report, row["id"]) or datetime.now(timezone.utc)),
            )
        report.counts["task_assignments"] = {"source": len(assignments), "target": 0}

        # dependencies, comments, attachments (empty today; imported when present)
        for table in ("task_dependencies", "task_comments", "task_attachments"):
            count = src.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            if count:
                report.failures.append(f"{table}: {count} rows present but this importer version does not map them")
            report.counts[table] = {"source": count, "target": 0}

        # cross-cell links
        link_total = 0
        for table, (column, target_type, urn_prefix) in LINK_SOURCES.items():
            links = src.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()
            for row in links:
                task = link_target(table, row["task_id"])
                if task is None:
                    continue
                if table == "task_goal_links":
                    relation = "supports-goal"
                elif table == "task_project_links":
                    relation = "belongs-to" if row["relation_type"] == "delivery" else "related-to"
                else:
                    relation = "related-to"
                attributes = {k: row[k] for k in row.keys() if k not in ("id", "task_id", column, "created_at", "updated_at")}
                pg.execute(
                    "INSERT INTO tasks.task_links (workspace_id, task_id, relation, target_type, target_ref, attributes,"
                    " legacy_table, legacy_id, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (workspace_id, task, relation, target_type, f"empire:{urn_prefix}:legacy-{row[column]}",
                     json.dumps(attributes), table, row["id"],
                     parse_timestamp(row["created_at"], "created_at", report, row["id"]) or datetime.now(timezone.utc)),
                )
            report.counts[table] = {"source": len(links), "target": 0}
            link_total += len(links)

        suggestions = src.execute("SELECT * FROM task_role_suggestions ORDER BY id").fetchall()
        for row in suggestions:
            task = link_target("task_role_suggestions", row["task_id"])
            if task is None:
                continue
            pg.execute(
                "INSERT INTO tasks.task_role_suggestions (workspace_id, task_id, role_id, confidence, match_reason,"
                " review_status, source_registry, legacy_id, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (workspace_id, task, row["role_id"], row["confidence"], row["match_reason"], row["review_status"],
                 row["source_registry"], row["id"],
                 parse_timestamp(row["created_at"], "created_at", report, row["id"]) or datetime.now(timezone.utc)),
            )
        report.counts["task_role_suggestions"] = {"source": len(suggestions), "target": 0}

        # counts read back from the target
        target_counts = {
            "tasks": "SELECT count(*) FROM tasks.tasks",
            "task_types": "SELECT count(*) FROM tasks.task_types",
            "task_assignments": "SELECT count(*) FROM tasks.task_assignments",
            "task_dependencies": "SELECT count(*) FROM tasks.task_dependencies",
            "task_comments": "SELECT count(*) FROM tasks.task_comments",
            "task_attachments": "SELECT count(*) FROM tasks.task_attachments",
            "task_role_suggestions": "SELECT count(*) FROM tasks.task_role_suggestions",
        }
        for table, query in target_counts.items():
            report.counts[table]["target"] = pg.execute(query).fetchone()[0]
        for table in LINK_SOURCES:
            report.counts[table]["target"] = pg.execute(
                "SELECT count(*) FROM tasks.task_links WHERE legacy_table = %s", (table,)
            ).fetchone()[0]
        for table, numbers in report.counts.items():
            if numbers["source"] != numbers["target"]:
                report.failures.append(f"{table}: {numbers['source']} source rows but {numbers['target']} imported")

        # referential integrity inside the cell (orphans are impossible with FKs; prove it)
        orphan_links = pg.execute(
            "SELECT count(*) FROM tasks.task_links l LEFT JOIN tasks.tasks t ON t.id = l.task_id WHERE t.id IS NULL"
        ).fetchone()[0]
        if orphan_links:
            report.failures.append(f"task_links: {orphan_links} orphaned rows")

        # soft references from other SQLite domains
        for (table,) in src.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name").fetchall():
            if table in CELL_TABLES:
                continue
            for fk in src.execute(f"PRAGMA foreign_key_list({table})").fetchall():
                if fk["table"] != "tasks":
                    continue
                column = fk["from"]
                refs = src.execute(f"SELECT {column} FROM {table} WHERE {column} IS NOT NULL").fetchall()
                unresolved = sum(1 for (legacy,) in refs if legacy not in ids)
                report.inbound_references[f"{table}.{column}"] = {"references": len(refs), "unresolved": unresolved}
                if unresolved:
                    report.failures.append(f"{table}.{column}: {unresolved} references to tasks that do not exist")

        # field-by-field comparison of every task
        columns = list(next(iter(expected.values())).keys()) if expected else []
        for legacy, values in expected.items():
            row = pg.execute(
                f"SELECT {', '.join(columns)} FROM tasks.tasks WHERE legacy_id = %s", (legacy,)
            ).fetchone()
            for column, want in zip(columns, values.values()):
                got = row[columns.index(column)]
                if got != want:
                    report.field_mismatches.append(f"tasks[{legacy}].{column}: {want!r} != {got!r}")
        if report.field_mismatches:
            report.failures.append(f"{len(report.field_mismatches)} field mismatches")

        pg.execute(
            "SELECT platform.record_audit(%s, 'tasks', 'tasks.import', 'workspace', %s, NULL, %s)",
            (execution_id, str(workspace_id), report.snapshot_sha256),
        )
        pg.execute("SELECT platform.finish_execution(%s, %s)", (execution_id, "succeeded" if report.ok else "failed"))
        if apply and report.ok:
            pg.commit()
        else:
            pg.rollback()
    src.close()
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="empire-tasks-import", description=__doc__.splitlines()[0])
    parser.add_argument("--sqlite", type=Path, default=DEFAULT_SQLITE)
    parser.add_argument("--workspace", default="owner")
    parser.add_argument("--apply", action="store_true", help="commit into the local development cluster")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    if not args.sqlite.is_file():
        print(f"source not found: {args.sqlite}", file=sys.stderr)
        return 2
    report = run(args.sqlite, default_cluster(), workspace_slug=args.workspace, apply=args.apply)
    payload = {**report.__dict__, "ok": report.ok}
    if args.report:
        args.report.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print(json.dumps({k: payload[k] for k in ("mode", "ok", "counts", "dropped_columns", "inbound_references", "failures")},
                     indent=2, default=str))
    print(f"anomalies: {len(report.anomalies)}; field mismatches: {len(report.field_mismatches)}")
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
