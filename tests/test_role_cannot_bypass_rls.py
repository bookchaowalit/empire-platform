"""platform_api cannot switch off, alter, or escape row-level security."""

from __future__ import annotations

import psycopg
import pytest

from conftest import create_workspace, new_id, use_workspace


def test_runtime_role_has_no_bypass_attributes(api):
    row = api.execute(
        "SELECT rolsuper, rolbypassrls, rolcreaterole, rolcreatedb, rolinherit FROM pg_roles WHERE rolname = 'platform_api'"
    ).fetchone()
    assert row == (False, False, False, False, False)


def test_runtime_role_owns_nothing_in_platform(api):
    owned = api.execute(
        "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
        " WHERE n.nspname = 'platform' AND pg_get_userbyid(c.relowner) = 'platform_api'"
    ).fetchone()[0]
    assert owned == 0


def test_every_platform_table_enforces_rls_even_for_its_owner(api):
    loose = api.execute(
        "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
        " WHERE n.nspname = 'platform' AND c.relkind = 'r'"
        " AND NOT (c.relrowsecurity AND c.relforcerowsecurity)"
    ).fetchall()
    assert loose == []


def test_row_security_off_is_refused(api):
    workspace = create_workspace(api, "gamma", ["tasks.task:read"])
    use_workspace(api, workspace.id)
    api.execute("SET LOCAL row_security = off")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        api.execute("SELECT count(*) FROM platform.memberships")


@pytest.mark.parametrize(
    "statement",
    [
        "ALTER TABLE platform.memberships DISABLE ROW LEVEL SECURITY",
        "ALTER TABLE platform.memberships NO FORCE ROW LEVEL SECURITY",
        "DROP POLICY workspace_isolation ON platform.memberships",
        "CREATE POLICY open ON platform.memberships USING (true)",
        "CREATE TABLE platform.shadow (id int)",
        "SET ROLE empire_migrator",
        "SET SESSION AUTHORIZATION postgres",
        "TRUNCATE platform.audit_events",
    ],
)
def test_privileged_statements_are_denied(api, statement):
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        api.execute(statement)


def test_audit_events_cannot_be_changed_by_the_runtime_role(api):
    workspace = create_workspace(api, "delta", ["tasks.task:read"])
    execution = new_id(api)
    api.execute(
        "INSERT INTO platform.executions (id, workspace_id, initiated_by, executed_by, on_behalf_of, channel)"
        " VALUES (%s, %s, %s, %s, %s, 'api')",
        (execution, workspace.id, workspace.owner, workspace.owner, workspace.owner),
    )
    api.execute(
        "INSERT INTO platform.audit_events (workspace_id, execution_id, service, action, resource_type, resource_id)"
        " VALUES (%s, %s, 'platform', 'test', 'workspace', 'x')",
        (workspace.id, execution),
    )
    for statement in (
        "UPDATE platform.audit_events SET action = 'edited'",
        "DELETE FROM platform.audit_events",
    ):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with api.transaction():
                api.execute(statement)


def test_audit_events_stay_append_only_for_the_owner(cluster):
    with cluster.connect("empire_migrator") as owner:
        with pytest.raises(psycopg.errors.InsufficientPrivilege, match="append-only"):
            owner.execute("DELETE FROM platform.audit_events")
        owner.rollback()
