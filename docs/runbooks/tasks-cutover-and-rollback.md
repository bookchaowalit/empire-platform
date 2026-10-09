# Tasks cutover and rollback

Status: rehearsed locally only (2026-10-09). A production cutover needs owner
approval, the writer migrations in the Solo Empire phase 1 plan (§5.1.1), and
a hosted database, none of which exist yet.

Source of truth is exactly one side at a time. There is never a dual write:
SQLite is frozen while PostgreSQL accepts writes, and the Tasks API write
switch is off while SQLite accepts writes again.

## Cutover

| Step | Command / mechanism | Rehearsed by |
|---|---|---|
| 1. Back up both sides | copy `infra/database/solo-empire.db`; `cutover.backup()` (pg_dump) | `test_restore_from_backup_recovers_schema_and_data` |
| 2. Freeze SQLite tasks writers | `cutover.freeze_sqlite()` installs refusing triggers on the tasks tables | `test_frozen_sqlite_refuses_writers_until_rollback` |
| 3. Final import | `empire-tasks-import --apply` (verifies counts, references, and every field; commits only if all pass) | `tests/test_tasks_import.py` |
| 4. Writers switch to the API | `SOLO_EMPIRE_TASKS_BACKEND=api` plus one token per automation (`empire-automations provision --workspace owner --tokens DIR`) | `scripts/rehearse_cutover.py` on a copy of current data; Go and TypeScript writers still pending |
| 5. API writes on | `cutover.set_writes(True)` | `test_write_switch_stops_api_writes_but_not_reads` |

## Rollback

| Step | Command / mechanism | Rehearsed by |
|---|---|---|
| 1. API writes off | `cutover.set_writes(False)`; the API answers 503 for changes and waits for writes already under way | `test_write_switch_stops_api_writes_but_not_reads` |
| 2. Back up PostgreSQL | `cutover.backup()` for forensics | `test_restore_from_backup_recovers_schema_and_data` |
| 3. Export to SQLite | `cutover.export_to_sqlite()`: updates, new tasks (new SQLite IDs, parents resolved), deletions, and new links to legacy targets, in one SQLite transaction | `test_full_rollback_keeps_every_write_made_after_cutover`, `test_export_is_all_or_nothing` |
| 4. Review unmapped links | links to entities that only exist in the new system are listed in `ExportReport.links_unmapped` and stay in the PostgreSQL backup | `test_full_rollback_keeps_every_write_made_after_cutover` |
| 5. Unfreeze SQLite | `cutover.unfreeze_sqlite()`; writers go back to SQLite | same |

## Full rehearsal on current data

`python scripts/rehearse_cutover.py` copies the live SQLite database (read-only
backup API) into a throwaway cluster, runs cutover, writer operations with
their own identities (retries, scope denials, a revocation mid-run), and the
rollback above, then checks that no task was lost, duplicated, or changed
unless a writer changed it. The report in `.runtime/readiness/rehearsal.json`
holds counts only.

## Schema or data damage without leaving PostgreSQL

Restore the last `pg_dump` into a new database with `cutover.restore()`,
verify, then point the services at it. Restoring never overwrites the live
database. Rehearsed by dropping the `tasks` schema on a copy and restoring.

## Known limits

- Export covers tasks, task links, and the task hierarchy. Assignments, role
  suggestions, and types are not changed after cutover by any API today, so
  SQLite keeps its own copies.
- Execution, audit, and authorization records exist only in PostgreSQL; keep
  the backup from rollback step 2.
