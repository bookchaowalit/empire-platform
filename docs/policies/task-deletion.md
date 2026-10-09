# Task deletion policy

Status: proposed with the platform ADR (2026-10-09). Applies to the tasks
cell and every automation that writes tasks.

## Rules

1. **Automations never delete.** No automation in
   `contracts/automations/v1/automations.json` holds `tasks.task:delete`; a
   test enforces it. Where a SQLite-era writer deleted rows
   (`reconcile_overdue_tasks.py --archive-only`), it archives instead
   (`tasks.archive:write`) with a reason.
2. **Archive is the soft delete.** Archived tasks leave the default lists but
   keep every field, link, and audit record. `POST /v1/tasks/{id}/unarchive`
   restores them (same permission).
3. **Hard delete is human and approved.** `DELETE /v1/tasks/{id}` needs
   `tasks.task:delete` and, under the workspace policy, an approved approval
   for `tasks.task:delete`. Agents and automations cannot obtain it.
4. **Retention.** Archived tasks are kept at least 365 days. Purging older
   archived tasks is a separate, human-approved job that does not exist yet;
   until then nothing is purged.
5. **Audit.** Archive, unarchive, and delete each write an outbox event and
   an audit event tied to the execution (who initiated, who executed, on whose
   behalf, under which delegation and approval).

## Mapping from the SQLite era

| Before | After |
|---|---|
| `reconcile_overdue_tasks.py --archive-only` ran `DELETE FROM tasks` for old completed tasks | archives them with reason `completed more than N days ago` |
| `reconcile_overdue_tasks.py` set status `cancelled` for tasks gone upstream | unchanged (status change, `tasks.status:write`) |
| old MCP `delete_task` tool deleted directly | retired; the new MCP has no delete tool |
