# PostgreSQL and Neon recovery

Owner: platform operator; production decisions and release approval: owner.
Scope: restore into an isolated target, validate, and prepare a recovery
decision with evidence. This runbook does not authorize overwriting a database,
changing production URLs/write switches, replaying external side effects or
deleting provider branches. Historical SQLite cutover is recorded in
[tasks-cutover-and-rollback.md](tasks-cutover-and-rollback.md).

## Evidence and service objectives

| Item | Current evidence | Required decision/evidence |
|---|---|---|
| Recovery mechanism | Synthetic local PostgreSQL custom-format dump/restore drill below | Neon isolated-branch restore with actual project/branch metadata |
| RPO target | Not set by this runbook | Owner-approved maximum acceptable data loss |
| Measured RPO | Local snapshot: acceptance requires zero missing rows against the committed synthetic snapshot | Hosted incident time minus recovered commit/time boundary; reconcile approved missing transactions |
| RTO target | Not set by this runbook | Owner-approved service restoration deadline |
| Measured RTO | Drill emits dump/restore and acceptance seconds; excludes detection, approval, deployment and traffic switch | Hosted drill timeline plus an explicitly approved production restoration timeline |
| Retention/PIT window | Unknown; no current Neon plan/window was checked | Read-only provider metadata: earliest/latest recoverable time, retention setting, snapshot branch existence and access |

Do not turn local timings into a production RTO promise. A historical
`cutover-after-import-*` entry is not proof of a currently recoverable snapshot.
Record UTC times, correlation ID, commit, PostgreSQL/client versions, migration
checksums, target branch metadata, counts and pass/fail outcomes only. Do not
include task titles, identity names, raw audit/authorization events, outbox
payloads, JWTs, credential-bearing URLs or provider secrets.

## Repeatable offline drill

From this repository, with the existing development environment:

```bash
.venv/bin/python -m pytest -q -s tests/test_recovery_rehearsal.py --tb=short
```

`tests/conftest.py` creates an `ep-*` cluster under the OS temporary directory,
bootstraps roles and applies current checksum-verified migrations. The drill
uses its Unix socket with TCP disabled; it never calls `default_cluster()`,
reads `db.env`, uses hosted URLs or touches `.runtime/pgdata`. No cloud services
or installs are involved. Missing binaries/dependencies are an unavailable
prerequisite, never a passing restore.

The drill creates synthetic workspaces, identities, delegations, tasks, a graph
relation, audit/authorization records and a pending outbox event through the
existing test fixtures/APIs. It takes a real `pg_dump --format=custom`, then
runs `pg_restore --exit-on-error` into a unique empty database. Runtime roles
already exist in the isolated cluster: this does not prove cross-cluster role
provisioning or Neon role portability.

The dump is created exclusively with mode `0600` in a `0700` temporary test
directory, outside tracked files. Connection parameters go through a minimal
subprocess environment; no password or connection URL is accepted. Tool failures
expose only exit code and a bounded error category. Existing dump paths and
database names are refused. The target is dropped only after the test created
it; the session fixture removes its disposable cluster. Pytest can retain
synthetic dump artifacts under its temporary directory; apply the operator's
temporary-file retention policy there. Do not use this test helper for live backups.

Acceptance requires all of these to pass:

- Every row in `platform`, `tasks`, `graph` and `public.schema_migrations`
  equals the snapshot, including audit, delegations, decision records and
  migration checksums. The source remains unchanged by restore validation.
- Forced RLS survives on workspace tables; runtime API roles have no elevated
  attributes. Cross-workspace/no-workspace reads reveal nothing, the API denies
  other-workspace access, read-only delegated writes fail, and delegation
  revocation takes effect. Runtime audit deletion fails.
- Each restored outbox execution reference resolves within its workspace.
  The publisher resumes delivery to the isolated graph consumer, drains the
  pending backlog, preserves the task version and ignores duplicate deliveries.
  Existing graph rows/relations are preserved by the snapshot comparison.
- Safety regressions refuse a non-fixture endpoint, an existing dump and an
  existing database without changing its sentinel data.

Output is aggregate JSON: `scope=local_synthetic`, `restore_seconds` (includes
dump and restore), `restore_and_acceptance_seconds`, `dump_bytes`,
`tables_compared`, `published_after_restore`, `snapshot_data_loss_rows`.
The pytest result is the acceptance gate. Record measured output from the run;
never substitute a mock success or a historical test name.

## Hosted isolated-branch drill (not yet verified)

Provider guidance: [branching](https://neon.com/docs/introduction/branching),
[branch management](https://neon.com/docs/manage/branches), and
[branch restore](https://neon.com/docs/guides/branch-restore).
The parent checked these sources on 2026-10-13. The documented in-place
point-in-time restore is restricted to root branches, overwrites all databases,
schema and auth on the target and interrupts connections; child branches do
not support instant restore. This rehearsal uses a newly created isolated
branch from an eligible historical point or verified snapshot instead. Do not
invoke in-place root restore under this runbook. Confirm account capabilities
and recovery history before choosing the operation.

1. Obtain read-only metadata with the parent's explicit decision: project,
   source branch, PostgreSQL version, region, plan, recovery window, snapshot
   branches and endpoint/pooler configuration. Share identifiers and retention
   metadata only. Confirm the proposed UTC recovery point is inside the actual
   provider window. If unavailable, stop with `needs_parent`. A rejected
   authentication request supplies no hosted evidence.
2. Agree the incident/recovery boundary, target RPO/RTO, approval owner and
   evidence correlation ID. Signing keys and deployment credentials have
   separate approved secret-store procedures, outside database restore.
3. Get explicit approval for a new isolated Neon branch/database and any cost.
   Use the new-branch-from-historical-point mechanism supported by the verified
   account, or a verified snapshot as its source. Name and tag the target
   uniquely. Do not reset the source branch or reuse a production endpoint.
4. Restrict target access. Do not attach production services, schedules,
   publishers, integrations, automations or public routing. No restored outbox
   may be delivered to live external consumers. Provision/verify target runtime
   roles and database ACLs through this repository's approved migration
   process; a logical `pg_dump` excludes cluster roles/deployment configuration.
5. Connect read-only to the isolated target through approved private credential
   delivery. Verify branch identity before any query. Compare schema and
   aggregates at the recovery boundary, including the checklist below.
   Read-only verification must not run migrations or authorization calls that
   log decisions.
6. After approval for isolated writes, use a dedicated synthetic workspace and
   fresh test identities/delegations. Repeat read/create/update, stale-version
   rejection, denied cross-workspace access, revoked delegation, approval-policy
   and append-only audit checks. Route publisher delivery to an isolated graph
   consumer only; verify replay is idempotent. Do not mint production tokens.
7. Record restore request/ready/acceptance times, recovery point, discrepancy
   counts, schema checksums, security outcomes, RPO and RTO scope. Mark
   unavailable checks explicitly. Keep source and production endpoint
   configuration unchanged. Target retention/cleanup requires a separate
   approved provider operation.

## Aggregate verification checklist

Use the authorized metadata role's allowed projections; permissions may differ
by table. Do not broaden the role to fetch payloads. If its projections cannot
establish a condition, mark that check unavailable and request approved access.
Compare at a consistent recovery boundary/transaction; counts from a currently
advancing source cannot establish point-in-time parity.

| Area | Aggregate evidence and acceptance |
|---|---|
| Schema | Migration count, names/checksums and expected release; constraints, indexes, triggers, security-definer grants/search paths and runtime-role ownership/attributes/memberships match the approved schema |
| Tasks | Counts by workspace/status/archive state; zero duplicate task numbers; parent/dependency/link reference violations zero; expected task/version distribution at boundary |
| Platform | Workspace/identity/membership/role/delegation/policy/approval counts; validity/revocation distribution; ownership/membership references consistent |
| Authorization and audit | Decision counts by code/channel; execution states and audit counts; workspace/execution joins consistent; no decision or audit payload in evidence |
| Graph | Node/edge/alias/consent/inbox counts by workspace/type/outcome; version distribution; unexpected unresolved endpoints or cross-workspace joins zero after documented exceptions |
| Outbox | Counts by published/pending/retry state, oldest pending age, attempts, unresolved execution references; expected graph inbox progress; isolated replay produces no duplicates |
| Security | Forced RLS and policies, no unexpected runtime object owners/role memberships; dedicated synthetic negative checks and append-only audit behavior |

Counts alone do not prove functional recovery; require synthetic acceptance
checks and discrepancy review. Compare only records expected by the selected
point, documenting intentional post-point data loss separately.

## Production decision gate

A successful isolated drill prepares evidence for an owner decision. Promotion,
service reconfiguration, write-switch changes, token/key rotation, queue replay
and traffic changes are separate approved operations. Before approval, prepare
the exact target, reconciliation plan for writes after the recovery point,
deployment rollback path, role/security checks, monitoring and success criteria.
No automatic failback or SQLite import is part of this runbook. Hosted snapshot
restore and production cutover remain **unverified** until actual approved
operations produce evidence.
