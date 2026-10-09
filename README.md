# empire-platform

Identity, workspace, authorization, workflow, and graph foundation for the
Solo Empire domain cells. Architecture and decisions live in the Solo Empire
repository:

- ADR: `docs/superpowers/specs/2026-10-09-platform-foundation-identity-workflow-design.md`
- Domain Cells + Empire Graph: `docs/superpowers/specs/2026-10-09-domain-cells-empire-graph-design.md`
- Phase 1 plan: `docs/superpowers/plans/2026-10-09-platform-foundation-phase-1.md`

Status: phase 1, items P1.1 (foundation) and P1.2 (identity and workspace
schema), local PostgreSQL only. No hosted database, deployment, or production
data is involved.

## Local development

PostgreSQL comes from the `pgserver` wheel (16.x) unless `EMPIRE_PG_BIN` points
at other binaries. The cluster lives in `.runtime/pgdata` and listens only on a
Unix socket.

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -e ".[dev]"
.venv/bin/empire-db start      # init, start, bootstrap roles, apply migrations
.venv/bin/empire-db psql       # connect as platform_api
.venv/bin/empire-db stop
.venv/bin/python -m pytest     # each run uses its own throwaway cluster
```

## Layout

| Path | Contents |
|---|---|
| `db/bootstrap/roles.sql` | cluster roles: `empire_migrator` (owns schemas, runs migrations) and `platform_api` (runtime, no BYPASSRLS, owns nothing) |
| `services/platform/migrations/` | `platform` schema: identities, workspaces, memberships, roles, policies, delegations, approvals, executions, audit events, RLS, and grants |
| `src/empire_platform/devdb.py` | local cluster control and the checksum-verified migration runner |
| `tests/` | tenant isolation, RLS bypass attempts, delegation limits, and migration checks |

## Security model

- Every `platform` table has RLS enabled and forced. Rows are visible only when
  `app.workspace_id` (set with `SET LOCAL` from a verified token) matches.
  Without it, nothing is visible.
- `platform_api` cannot disable or alter RLS, assume the owner role, create
  objects, or change `audit_events`, which is also append-only for its owner.
- Agents hold no direct permissions. They act through a delegation whose scopes
  must be a subset of the grantor's permissions when created and are checked
  against the grantor's live permissions on every use.
- Acting on behalf of another identity requires a live delegation from that
  identity.
- Application code still authorizes every request; RLS is the second line of
  defence.
