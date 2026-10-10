# empire-platform

Identity, workspace, authorization, workflow, and graph foundation for the
Solo Empire domain cells. Architecture and decisions live in the Solo Empire
repository:

- ADR: `docs/superpowers/specs/2026-10-09-platform-foundation-identity-workflow-design.md`
- Domain Cells + Empire Graph: `docs/superpowers/specs/2026-10-09-domain-cells-empire-graph-design.md`
- Phase 1 plan: `docs/superpowers/plans/2026-10-09-platform-foundation-phase-1.md`

Status: phase 1 on local PostgreSQL only: platform schema, shared
authorization, tasks schema and import, the Tasks API with writer parity, the
transactional outbox and publisher, a minimal Empire Graph, the MCP adapter,
and a rehearsed cutover/rollback. No hosted database, deployment, or
production data is involved, and SQLite writers have not been switched.

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
.venv/bin/empire-tasks-import  # dry run from the Solo Empire SQLite (read-only); --apply commits locally
.venv/bin/empire-contracts --check
.venv/bin/tasks-api            # http://127.0.0.1:3601, tokens from empire-dev-token
```

## Layout

| Path | Contents |
|---|---|
| `db/bootstrap/roles.sql` | cluster roles: `empire_migrator` (owns schemas, runs migrations) and `platform_api` (runtime, no BYPASSRLS, owns nothing) |
| `services/platform/migrations/` | `platform` schema: identities, workspaces, memberships, roles, policies, delegations, approvals, executions, audit events, RLS, and grants |
| `src/empire_platform/devdb.py` | local cluster control and the checksum-verified migration runner |
| `services/platform/migrations/0003_*` | `platform.authorize` policy decision point, decision log, and SECURITY DEFINER entry points for domain services |
| `contracts/authz/v1/` | language-neutral authorization request/decision schemas and conformance cases |
| `packages/authz/` | Python client (`authorize`, `enforce`) for the decision point |
| `services/tasks/migrations/` | `tasks` schema with forced RLS, URN links, and the transactional outbox |
| `services/tasks/tasks_api/` | Tasks API (`tasks.v1`); contract in `contracts/tasks/v1/` |
| `src/empire_platform/tasks_import.py` | read-only SQLite snapshot import with full verification; dry run by default |
| `services/tasks/tasks_api/publisher.py` | outbox publisher (`tasks_publisher` role): oldest first, retry with backoff, at least once |
| `services/graph/` | graph schema (nodes, aliases, edges, relation types, consents, inbox), event consumer, and the read API |
| `services/mcp/empire_mcp/` | MCP tools `tasks.*` and `graph.*` that call the APIs with the caller's token and reject unknown arguments; `hosted.py` serves them over Streamable HTTP behind Clerk OAuth with a subject allowlist (`docs/runbooks/remote-mcp.md`) |
| `src/empire_platform/cutover.py`, `docs/runbooks/` | backup/restore, write switch, SQLite freeze, lossless export back to SQLite |
| `tests/` | isolation, RLS bypass, delegation, authorization conformance and TOCTOU, import, API, contract, writer parity, graph, MCP, and cutover/rollback tests |

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

## Authorization

`platform.authorize(actor, workspace, action, resource, delegation, on_behalf_of,
approval, channel, lock)` is the only place that decides. It denies by default,
answers `resource_not_found` for other workspaces, follows delegation
revocation and the grantor's live permissions, applies approval policies, and
records every decision. Other languages call the same function; the contract
and its conformance cases live in `contracts/authz/v1/`.

Writes check twice: an unlocked, recorded precheck, then `enforce` (with row
locks) inside the transaction that changes data, so a revocation between the
two is caught.
