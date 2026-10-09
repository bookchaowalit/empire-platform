"""Transactional outbox publisher for the tasks cell.

Runs as ``tasks_publisher``, which can read and mark outbox rows in every
workspace and nothing else. Events go out oldest first; a failed delivery is
retried later with exponential backoff. Delivery is at least once: if the
publisher stops between delivering and marking, the event is delivered again,
so every consumer must be idempotent on the event ID.

    empire-publish [--batch N]
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import psycopg
from psycopg.rows import dict_row

MAX_BACKOFF_SECONDS = 600


@dataclass
class PublishReport:
    delivered: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)


def publish(connect: Callable[[], psycopg.Connection], deliver: Callable[[dict[str, Any]], Any],
            *, batch: int = 100) -> PublishReport:
    report = PublishReport()
    with connect() as conn:
        conn.row_factory = dict_row
        rows = conn.execute(
            "SELECT id, workspace_id, type, version, subject, execution_id, classification, payload, occurred_at"
            " FROM tasks.outbox WHERE published_at IS NULL AND next_attempt_at <= now()"
            " ORDER BY occurred_at, id LIMIT %s FOR UPDATE SKIP LOCKED",
            (batch,),
        ).fetchall()
        for row in rows:
            event = json.loads(json.dumps(row, default=str))
            try:
                deliver(event)
            except Exception as exc:  # any consumer failure: retry later, never lose the event
                report.failed += 1
                report.errors.append(f"{row['id']}: {type(exc).__name__}")
                conn.execute(
                    "UPDATE tasks.outbox SET attempts = attempts + 1, last_error = %s,"
                    " next_attempt_at = now() + make_interval(secs => least(%s, power(2, attempts + 1)))"
                    " WHERE id = %s",
                    (type(exc).__name__, MAX_BACKOFF_SECONDS, row["id"]),
                )
                continue
            conn.execute("UPDATE tasks.outbox SET published_at = now(), last_error = NULL WHERE id = %s", (row["id"],))
            report.delivered += 1
        conn.commit()
    return report


def main(argv: list[str] | None = None) -> int:
    from empire_platform.devdb import default_cluster
    from graph_service.consumer import GraphConsumer

    parser = argparse.ArgumentParser(prog="empire-publish", description=__doc__.splitlines()[0])
    parser.add_argument("--batch", type=int, default=100)
    args = parser.parse_args(argv)
    cluster = default_cluster()
    report = publish(lambda: cluster.connect("tasks_publisher"), GraphConsumer(lambda: cluster.connect("graph_api")),
                     batch=args.batch)
    print(json.dumps(report.__dict__))
    return 0 if not report.failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
