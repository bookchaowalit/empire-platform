"""Provision automation identities and their delegations from the registry.

Each automation in contracts/automations/v1/automations.json becomes an
``automation`` identity in the workspace with a delegation from the
workspace owner carrying only the registry scopes. Provisioning is
idempotent: an unchanged, unexpired delegation is kept; a changed or
expiring one is replaced and the old one revoked.

    empire-automations provision --workspace SLUG [--tokens DIR]   # local only
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from psycopg.rows import dict_row

from empire_platform.devdb import REPO_ROOT, Cluster, default_cluster

REGISTRY = REPO_ROOT / "contracts" / "automations" / "v1" / "automations.json"
RENEW_BEFORE = timedelta(days=7)


@dataclass(frozen=True)
class Provisioned:
    automation: str
    identity: uuid.UUID
    delegation: uuid.UUID
    owner: uuid.UUID
    scopes: tuple[str, ...]
    renewed: bool


def load_registry(path: Path = REGISTRY) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def provision(cluster: Cluster, workspace_id: uuid.UUID, registry: dict | None = None) -> list[Provisioned]:
    registry = registry or load_registry()
    days = int(registry["delegation_days"])
    results: list[Provisioned] = []
    with cluster.connect("platform_api") as conn:
        conn.row_factory = dict_row
        conn.execute("SELECT set_config('app.workspace_id', %s, true)", (str(workspace_id),))
        owner = conn.execute("SELECT owner_identity_id FROM platform.workspaces WHERE id = %s",
                             (workspace_id,)).fetchone()["owner_identity_id"]
        for automation in registry["automations"]:
            subject = f"automation:{workspace_id}:{automation['id']}"
            scopes = tuple(sorted(automation["scopes"]))
            row = conn.execute("SELECT id FROM platform.identities WHERE external_subject = %s", (subject,)).fetchone()
            if row is None:
                identity = conn.execute("SELECT platform.uuid_v7() AS id").fetchone()["id"]
                conn.execute("INSERT INTO platform.identities (id, kind, display_name, external_subject)"
                             " VALUES (%s, 'automation', %s, %s)", (identity, automation["id"], subject))
                conn.execute("INSERT INTO platform.memberships (workspace_id, identity_id) VALUES (%s, %s)",
                             (workspace_id, identity))
            else:
                identity = row["id"]
            current = conn.execute(
                "SELECT id, scopes::text[] AS scopes, expires_at FROM platform.delegations"
                " WHERE grantee_identity_id = %s AND grantor_identity_id = %s AND revoked_at IS NULL"
                " ORDER BY created_at DESC LIMIT 1", (identity, owner)).fetchone()
            now = datetime.now(timezone.utc)
            if current and tuple(sorted(current["scopes"])) == scopes and current["expires_at"] - now > RENEW_BEFORE:
                results.append(Provisioned(automation["id"], identity, current["id"], owner, scopes, False))
                continue
            conn.execute("UPDATE platform.delegations SET revoked_at = now() WHERE grantee_identity_id = %s"
                         " AND revoked_at IS NULL", (identity,))
            delegation = conn.execute("SELECT platform.uuid_v7() AS id").fetchone()["id"]
            conn.execute(
                "INSERT INTO platform.delegations (id, workspace_id, grantor_identity_id, grantee_identity_id, scopes,"
                " expires_at) VALUES (%s, %s, %s, %s, %s::platform.permission[], now() + make_interval(days => %s))",
                (delegation, workspace_id, owner, identity, list(scopes), days))
            results.append(Provisioned(automation["id"], identity, delegation, owner, scopes, True))
        conn.commit()
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="empire-automations", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("provision")
    run.add_argument("--workspace", required=True, help="workspace slug in the local cluster")
    run.add_argument("--tokens", type=Path, help="write a short-lived dev token per automation into this directory")
    args = parser.parse_args(argv)
    cluster = default_cluster()
    with cluster.connect("postgres") as conn:
        found = conn.execute("SELECT id FROM platform.workspaces WHERE slug = %s", (args.workspace,)).fetchone()
    if not found:
        print(f"unknown workspace {args.workspace}", file=sys.stderr)
        return 2
    results = provision(cluster, found[0])
    if args.tokens:
        from empire_platform.devtoken import key_pair, mint

        private_pem, _ = key_pair()
        args.tokens.mkdir(parents=True, exist_ok=True, mode=0o700)
        for item in results:
            path = args.tokens / f"{item.automation}.jwt"
            path.write_text(mint(private_pem, sub=item.identity, ws=found[0], delegation=item.delegation,
                                 obo=item.owner, ttl=3600), encoding="utf-8")
            path.chmod(0o600)
    for item in results:
        print(f"{item.automation:20} {'renewed' if item.renewed else 'kept':8} {', '.join(item.scopes)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
