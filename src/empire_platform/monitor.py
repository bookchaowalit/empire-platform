"""Daily cutover check against the rollback criteria.

Runs as ``platform_monitor``, which reads only identifiers, counts, versions,
timestamps, and decision codes. Each run compares a snapshot with the last
one and reports every check as ``ok``, ``warn``, or ``breach``:

- tasks lost: a workspace has fewer tasks than before, beyond its deletions
- duplicate task numbers within a workspace
- outbox backlog: oldest unpublished event older than 15 min (warn) / 60 min (breach)
- outbox retries: an unpublished event retried 3 times or more
- graph lag: events published more than 60 min ago that the graph never applied
- write switch off (warn: the cutover is paused)
- Tasks API down: warn at once, breach after 30 min
- workspace mismatches in authorization decisions (warn; RLS still holds)

Exit code 0 when all checks pass, 1 for a warning, 2 for a breach.

    empire-monitor [--api http://127.0.0.1:3601] [--json]
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from psycopg.rows import dict_row

from empire_platform.devdb import Cluster, default_cluster

REPO_ROOT = Path(__file__).resolve().parents[2]
STATE_DIR = REPO_ROOT / ".runtime" / "monitor"
OUTBOX_WARN = timedelta(minutes=15)
OUTBOX_BREACH = timedelta(minutes=60)
GRAPH_LAG = timedelta(minutes=60)
API_DOWN_BREACH = timedelta(minutes=30)
RETRY_WARN = 3
LEVELS = {"ok": 0, "warn": 1, "breach": 2}


@dataclass
class Finding:
    check: str
    level: str
    detail: str
    workspace: str | None = None


def snapshot(cluster: Cluster, dbname: str = "empire") -> dict[str, Any]:
    with cluster.connect("platform_monitor", dbname) as conn:
        conn.row_factory = dict_row
        workspaces = {
            str(row["id"]): {"slug": row["slug"], "tasks": 0, "distinct_numbers": 0, "deleted_events": 0}
            for row in conn.execute("SELECT id, slug FROM platform.workspaces")
        }
        for row in conn.execute(
                "SELECT workspace_id, count(*) AS tasks, count(DISTINCT legacy_id) AS numbers,"
                " count(*) FILTER (WHERE archived_at IS NULL) AS active FROM tasks.tasks GROUP BY workspace_id"):
            workspaces.setdefault(str(row["workspace_id"]), {"slug": None, "deleted_events": 0}).update(
                tasks=row["tasks"], distinct_numbers=row["numbers"], active=row["active"])
        for row in conn.execute("SELECT workspace_id, count(*) AS n FROM tasks.outbox"
                                " WHERE type = 'tasks.task.deleted' GROUP BY workspace_id"):
            workspaces[str(row["workspace_id"])]["deleted_events"] = row["n"]
        outbox = conn.execute(
            "SELECT count(*) AS pending, min(occurred_at) AS oldest, coalesce(max(attempts), 0) AS max_attempts"
            " FROM tasks.outbox WHERE published_at IS NULL").fetchone()
        lagging = conn.execute(
            "SELECT count(*) AS n FROM tasks.outbox o WHERE o.published_at < now() - %s::interval"
            " AND NOT EXISTS (SELECT 1 FROM graph.inbox i WHERE i.consumer = 'graph' AND i.event_id = o.id)",
            (f"{int(GRAPH_LAG.total_seconds())} seconds",)).fetchone()["n"]
        writes = conn.execute("SELECT value FROM tasks.settings WHERE key = 'writes_enabled'").fetchone()
        mismatches = conn.execute(
            "SELECT count(*) AS n FROM platform.authorization_decisions"
            " WHERE reason = 'workspace_mismatch' AND decided_at > now() - interval '1 day'").fetchone()["n"]
        now = conn.execute("SELECT now() AS now").fetchone()["now"]
    return {
        "taken_at": now.isoformat(),
        "workspaces": workspaces,
        "outbox": {"pending": outbox["pending"], "max_attempts": outbox["max_attempts"],
                   "oldest_age_seconds": int((now - outbox["oldest"]).total_seconds()) if outbox["oldest"] else 0,
                   "graph_lagging": lagging},
        "writes_enabled": bool(writes and writes["value"] == "true"),
        "workspace_mismatch_denials_24h": mismatches,
    }


def probe(api: str | None, timeout: float = 5.0) -> bool | None:
    if not api:
        return None
    try:
        with urllib.request.urlopen(f"{api.rstrip('/')}/health", timeout=timeout) as response:
            return response.status == 200
    except (urllib.error.URLError, OSError):
        return False


def evaluate(current: dict[str, Any], previous: dict[str, Any] | None, api_up: bool | None,
             api_down_since: str | None, now: datetime) -> tuple[list[Finding], str | None]:
    findings: list[Finding] = []
    for workspace, stats in current["workspaces"].items():
        if stats.get("tasks", 0) != stats.get("distinct_numbers", 0):
            findings.append(Finding("duplicate_task_numbers", "breach",
                                    f"{stats['tasks']} tasks but {stats['distinct_numbers']} numbers", workspace))
        before = (previous or {}).get("workspaces", {}).get(workspace)
        if before:
            allowed = stats.get("deleted_events", 0) - before.get("deleted_events", 0)
            if stats.get("tasks", 0) < before.get("tasks", 0) - allowed:
                findings.append(Finding("tasks_lost", "breach",
                                        f"{before['tasks']} -> {stats['tasks']} tasks with {allowed} deletions",
                                        workspace))
    for workspace in (previous or {}).get("workspaces", {}):
        if workspace not in current["workspaces"]:
            findings.append(Finding("workspace_missing", "breach", "workspace present in the last run is gone",
                                    workspace))

    age = timedelta(seconds=current["outbox"]["oldest_age_seconds"])
    if age > OUTBOX_BREACH:
        findings.append(Finding("outbox_backlog", "breach", f"oldest unpublished event is {age} old"))
    elif age > OUTBOX_WARN:
        findings.append(Finding("outbox_backlog", "warn", f"oldest unpublished event is {age} old"))
    if current["outbox"]["max_attempts"] >= RETRY_WARN:
        findings.append(Finding("outbox_retries", "warn",
                                f"an unpublished event was retried {current['outbox']['max_attempts']} times"))
    if current["outbox"]["graph_lagging"]:
        findings.append(Finding("graph_lag", "warn",
                                f"{current['outbox']['graph_lagging']} published events not applied by the graph"))
    if not current["writes_enabled"]:
        findings.append(Finding("writes_disabled", "warn", "Tasks API write switch is off"))
    if current["workspace_mismatch_denials_24h"]:
        findings.append(Finding("workspace_mismatch_attempts", "warn",
                                f"{current['workspace_mismatch_denials_24h']} cross-workspace attempts denied in 24 h"))

    down_since = None
    if api_up is False:
        down_since = api_down_since or now.isoformat()
        down_for = now - datetime.fromisoformat(down_since)
        level = "breach" if down_for > API_DOWN_BREACH else "warn"
        findings.append(Finding("api_down", level, f"Tasks API health check failing for {down_for}"))
    return findings, down_since


def run(cluster: Cluster, api: str | None, state_dir: Path = STATE_DIR) -> dict[str, Any]:
    state_dir.mkdir(parents=True, exist_ok=True)
    state_path = state_dir / "state.json"
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    current = snapshot(cluster)
    now = datetime.now(timezone.utc)
    findings, down_since = evaluate(current, state.get("snapshot"), probe(api), state.get("api_down_since"), now)
    level = max((f.level for f in findings), key=LEVELS.__getitem__, default="ok")
    report = {"level": level, "checked_at": now.isoformat(timespec="seconds"), "api_checked": api is not None,
              "findings": [asdict(f) for f in findings], "snapshot": current}
    state_path.write_text(json.dumps({"snapshot": current, "api_down_since": down_since}, indent=2))
    (state_dir / "latest.json").write_text(json.dumps(report, indent=2))
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="empire-monitor", description=__doc__.splitlines()[0])
    parser.add_argument("--api", help="Tasks API base URL to health-check")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    report = run(default_cluster(), args.api)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(f"tasks platform check: {report['level']}")
        for finding in report["findings"]:
            where = f" [{finding['workspace']}]" if finding["workspace"] else ""
            print(f"  {finding['level']:6} {finding['check']}{where}: {finding['detail']}")
    return LEVELS[report["level"]]


if __name__ == "__main__":
    sys.exit(main())
