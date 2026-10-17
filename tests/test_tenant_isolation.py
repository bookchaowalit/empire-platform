"""Workspace A cannot see or change workspace B through platform_api."""

from __future__ import annotations

import psycopg
import pytest
from conftest import (
    add_member,
    create_workspace,
    delegate,
    has_permission,
    new_id,
    use_workspace,
)

TENANT_TABLES = [
    "workspaces",
    "memberships",
    "roles",
    "role_permissions",
    "membership_roles",
    "policies",
    "delegations",
    "approvals",
    "executions",
    "audit_events",
    "identities",
]


@pytest.fixture
def two_workspaces(api):
    a = create_workspace(api, "alpha", ["tasks.task:read", "tasks.task:write"])
    agent = add_member(api, a, "coach", "agent")
    delegation = delegate(api, a, a.owner, agent, ["tasks.task:read"])
    execution = new_id(api)
    api.execute(
        "INSERT INTO platform.executions (id, workspace_id, initiated_by, executed_by, on_behalf_of,"
        " delegation_id, channel) VALUES (%s, %s, %s, %s, %s, %s, 'api')",
        (execution, a.id, a.owner, agent, a.owner, delegation),
    )
    api.execute(
        "INSERT INTO platform.policies (workspace_id, permission, requires_approval) VALUES (%s, 'tasks.task:delete', true)",
        (a.id,),
    )
    api.execute(
        "INSERT INTO platform.approvals (workspace_id, requested_action) VALUES (%s, 'tasks.task:delete')",
        (a.id,),
    )
    api.execute(
        "INSERT INTO platform.audit_events (workspace_id, execution_id, service, action, resource_type, resource_id)"
        " VALUES (%s, %s, 'platform', 'delegation.created', 'delegation', %s)",
        (a.id, execution, str(delegation)),
    )
    b = create_workspace(api, "beta", ["tasks.task:read"])
    return a, b


def count(conn, table: str) -> int:
    return conn.execute(f"SELECT count(*) FROM platform.{table}").fetchone()[0]


def rows_of(conn, table: str, workspace) -> int:
    """Rows that belong to ``workspace``, as far as the current context can see."""
    if table == "identities":
        return conn.execute(
            "SELECT count(*) FROM platform.identities WHERE id = %s", (workspace.owner,)
        ).fetchone()[0]
    column = "id" if table == "workspaces" else "workspace_id"
    return conn.execute(
        f"SELECT count(*) FROM platform.{table} WHERE {column} = %s", (workspace.id,)
    ).fetchone()[0]


@pytest.mark.parametrize("table", TENANT_TABLES)
def test_rows_of_one_workspace_are_invisible_from_another(api, two_workspaces, table):
    a, b = two_workspaces
    use_workspace(api, a.id)
    assert rows_of(api, table, a) > 0
    use_workspace(api, b.id)
    assert rows_of(api, table, a) == 0


@pytest.mark.parametrize("table", TENANT_TABLES)
def test_no_workspace_context_sees_nothing(api, two_workspaces, table):
    use_workspace(api, None)
    assert count(api, table) == 0


def test_cannot_write_into_another_workspace(api, two_workspaces):
    a, b = two_workspaces
    use_workspace(api, b.id)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        api.execute(
            "INSERT INTO platform.roles (workspace_id, name) VALUES (%s, 'intruder')",
            (a.id,),
        )


def test_cannot_update_or_delete_another_workspace(api, two_workspaces):
    a, b = two_workspaces
    use_workspace(api, b.id)
    assert (
        api.execute(
            "UPDATE platform.roles SET name = 'hijacked' WHERE workspace_id = %s",
            (a.id,),
        ).rowcount
        == 0
    )
    assert (
        api.execute(
            "DELETE FROM platform.membership_roles WHERE workspace_id = %s", (a.id,)
        ).rowcount
        == 0
    )
    use_workspace(api, a.id)
    assert (
        api.execute(
            "SELECT count(*) FROM platform.roles WHERE name = 'hijacked'"
        ).fetchone()[0]
        == 0
    )


def test_permissions_do_not_cross_workspaces(api, two_workspaces):
    a, b = two_workspaces
    use_workspace(api, a.id)
    assert has_permission(api, a.owner, "tasks.task:write")
    use_workspace(api, b.id)
    assert not has_permission(api, a.owner, "tasks.task:write")
    assert not has_permission(api, a.owner, "tasks.task:read")


def test_cannot_move_a_row_to_another_workspace(api, two_workspaces):
    a, b = two_workspaces
    use_workspace(api, a.id)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        api.execute("UPDATE platform.policies SET workspace_id = %s", (b.id,))
