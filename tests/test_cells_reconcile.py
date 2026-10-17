"""Owner reconciliation of cell executions whose finish call was lost."""

from __future__ import annotations

import uuid

import psycopg
import pytest
from conftest import create_workspace, use_workspace

from empire_platform import cells
from empire_platform.permissions import DECISIONS_OWNER


def superuser(cluster, query, params=()):
    with cluster.connect("postgres") as conn:
        result = conn.execute(query, params)
        return result.fetchall() if result.description else None


def start(
    cluster, ws, actor, permission="decisions.decision:write", resource_id="d-1"
) -> uuid.UUID:
    """An execution begun the way the platform HTTP route begins one."""
    with cluster.connect("platform_api") as conn:
        use_workspace(conn, ws)
        execution = conn.execute(
            "SELECT execution_id FROM platform.begin_cell_execution("
            "%s, %s, %s, NULL, NULL, 'api', NULL, NULL, 'decisions', %s, 'decision', %s, NULL)",
            (actor, actor, actor, permission, resource_id),
        ).fetchone()[0]
        conn.commit()
    return execution


def age(cluster, execution, interval="2 hours"):
    superuser(
        cluster,
        "UPDATE platform.executions SET started_at = now() - %s::interval WHERE id = %s",
        (interval, execution),
    )


def audit(cluster, execution):
    return superuser(
        cluster,
        "SELECT service, action, resource_id, before_hash, after_hash FROM platform.audit_events"
        " WHERE execution_id = %s ORDER BY occurred_at, action",
        (execution,),
    )


@pytest.fixture(scope="module")
def spaces(cluster):
    with cluster.connect("platform_api") as conn:
        suffix = uuid.uuid4().hex[:8]
        ws = create_workspace(conn, f"reconcile-{suffix}", list(DECISIONS_OWNER))
        other = create_workspace(
            conn, f"reconcile-other-{suffix}", list(DECISIONS_OWNER)
        )
        conn.commit()
    return ws, other


def test_recent_executions_are_not_listed_or_closed(cluster, spaces):
    ws, _ = spaces
    execution = start(cluster, ws.id, ws.owner)
    assert execution not in {
        row["execution_id"] for row in cells.stuck(cluster, ws.id, "decisions")
    }
    assert (
        cells.reconcile(cluster, ws.id, "decisions", execution, "failed")
        == "too_recent"
    )
    age(cluster, execution)
    assert execution in {
        row["execution_id"] for row in cells.stuck(cluster, ws.id, "decisions")
    }
    assert (
        cells.reconcile(cluster, ws.id, "decisions", execution, "failed") == "finished"
    )


def test_success_audits_only_the_bound_operation(cluster, spaces):
    ws, _ = spaces
    execution = start(cluster, ws.id, ws.owner, resource_id="d-7")
    age(cluster, execution)
    for forged in (
        "tasks.task.deleted",
        "decisions.review.updated",
        "decisions.decision.deleted",
        "decisions.decision.approved",
        "bogus",
    ):
        assert (
            cells.reconcile(cluster, ws.id, "decisions", execution, "succeeded", forged)
            == "mismatch"
        )
    assert (
        cells.reconcile(cluster, ws.id, "decisions", execution, "succeeded", None)
        == "mismatch"
    )
    assert audit(cluster, execution) == []
    assert (
        cells.reconcile(
            cluster,
            ws.id,
            "decisions",
            execution,
            "succeeded",
            "decisions.decision.updated",
        )
        == "finished"
    )
    assert superuser(
        cluster, "SELECT status FROM platform.executions WHERE id = %s", (execution,)
    ) == [("succeeded",)]
    # The bound resource, no row hashes, and a second record that marks the reconciliation.
    assert audit(cluster, execution) == [
        ("decisions", "decisions.decision.updated", "d-7", None, None),
        ("platform", "platform.execution.reconciled", str(execution), None, None),
    ]
    assert (
        cells.reconcile(cluster, ws.id, "decisions", execution, "failed") == "succeeded"
    )
    assert len(audit(cluster, execution)) == 2


def test_failure_records_only_the_reconciliation(cluster, spaces):
    ws, _ = spaces
    execution = start(cluster, ws.id, ws.owner)
    age(cluster, execution)
    with pytest.raises(psycopg.errors.CheckViolation):
        cells.reconcile(
            cluster,
            ws.id,
            "decisions",
            execution,
            "failed",
            "decisions.decision.updated",
        )
    assert (
        cells.reconcile(cluster, ws.id, "decisions", execution, "failed") == "finished"
    )
    assert superuser(
        cluster, "SELECT status FROM platform.executions WHERE id = %s", (execution,)
    ) == [("failed",)]
    assert [row[1] for row in audit(cluster, execution)] == [
        "platform.execution.reconciled"
    ]


def test_other_workspace_or_cell_cannot_close_it(cluster, spaces):
    ws, other = spaces
    execution = start(cluster, ws.id, ws.owner)
    age(cluster, execution)
    assert (
        cells.reconcile(cluster, other.id, "decisions", execution, "failed")
        == "not_found"
    )
    assert cells.reconcile(cluster, ws.id, "tasks", execution, "failed") == "not_found"
    assert execution not in {
        row["execution_id"] for row in cells.stuck(cluster, other.id, "decisions")
    }
    assert (
        cells.reconcile(cluster, ws.id, "decisions", execution, "failed") == "finished"
    )


@pytest.mark.parametrize("role", ["tasks_api", "platform_monitor", "graph_api"])
def test_only_the_platform_role_may_reconcile(cluster, role):
    with cluster.connect(role) as conn:
        for query in (
            "SELECT platform.reconcile_cell_execution(gen_random_uuid(), 'decisions', 'failed', NULL)",
            "SELECT * FROM platform.stuck_cell_executions('decisions', interval '1 hour')",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(query)
            conn.rollback()


@pytest.mark.parametrize(
    "argv",
    [
        ["--execution", str(uuid.uuid4())],
        ["--fail"],
        ["--execution", str(uuid.uuid4()), "--succeed"],
        [
            "--execution",
            str(uuid.uuid4()),
            "--fail",
            "--action",
            "decisions.decision.updated",
        ],
        [
            "--execution",
            str(uuid.uuid4()),
            "--fail",
            "--succeed",
            "--action",
            "decisions.decision.updated",
        ],
    ],
)
def test_cli_refuses_incomplete_reconcile_arguments(argv):
    with pytest.raises(SystemExit) as exited:
        cells.main(["reconcile", "--cell", "decisions", *argv])
    assert exited.value.code == 2
