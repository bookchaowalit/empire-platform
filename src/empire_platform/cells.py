"""Give a workspace owner the permissions of a cell.

A new cell (for example ``decisions``) brings its own permission names. The
owner role of a workspace gets them once, through this command; automations
then receive narrower subsets from contracts/automations/v1 as usual.

    empire-cells grant --workspace SLUG --cell decisions [--dry-run]

Idempotent: permissions the role already has are left alone.
"""

from __future__ import annotations

import argparse
import sys
import uuid

from psycopg.rows import dict_row

from empire_platform.devdb import Cluster, default_cluster
from empire_platform.permissions import CELL_OWNERS


def grant(cluster: Cluster, workspace_id: uuid.UUID, cell: str, *, apply: bool = True) -> list[str]:
    """Permissions added to the owner role (or that would be, without ``apply``)."""
    permissions = CELL_OWNERS[cell]
    with cluster.connect("platform_api") as conn:
        conn.row_factory = dict_row
        conn.execute("SELECT set_config('app.workspace_id', %s, true)", (str(workspace_id),))
        role = conn.execute("SELECT id FROM platform.roles WHERE workspace_id = %s AND name = 'owner'",
                            (workspace_id,)).fetchone()
        if role is None:
            raise SystemExit("workspace has no owner role")
        have = {r["permission"] for r in conn.execute(
            "SELECT permission FROM platform.role_permissions WHERE role_id = %s", (role["id"],))}
        missing = [p for p in permissions if p not in have]
        for permission in missing:
            conn.execute("INSERT INTO platform.role_permissions (workspace_id, role_id, permission) VALUES (%s, %s, %s)",
                         (workspace_id, role["id"], permission))
        if apply:
            conn.commit()
        else:
            conn.rollback()
    return missing


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="empire-cells", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("grant")
    run.add_argument("--workspace", required=True, help="workspace slug")
    run.add_argument("--cell", required=True, choices=sorted(CELL_OWNERS))
    run.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    cluster = default_cluster()
    with cluster.connect("postgres") as conn:
        found = conn.execute("SELECT id FROM platform.workspaces WHERE slug = %s", (args.workspace,)).fetchone()
    if not found:
        print(f"unknown workspace {args.workspace}", file=sys.stderr)
        return 2
    added = grant(cluster, found[0], args.cell, apply=not args.dry_run)
    verb = "would add" if args.dry_run else "added"
    print(f"{args.cell}: {verb} {len(added)} owner permissions" + (f" ({', '.join(added)})" if added else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
