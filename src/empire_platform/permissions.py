"""Permission sets used by the platform's own tooling and tests.

Write access to tasks is split by operation so automations get only what they
use: ``tasks.task:write`` (create, upsert, general edits), ``tasks.status:write``
(status, due date, progress, complete), ``tasks.calendar:write``,
``tasks.link:write``, ``tasks.archive:write``, ``tasks.reminder:write``, and
``tasks.occurrence:write``, ``tasks.assignment:write`` (assign roles or people),
and ``tasks.suggestion:write`` (role suggestions and their review).
"""

DECISIONS_OWNER = (
    "decisions.decision:read", "decisions.decision:write", "decisions.decision:approve",
    "decisions.decision:delete", "decisions.review:write", "graph.decisions:write",
)

# Owner permissions per cell; `empire-cells grant` adds them to a workspace's owner role.
CELL_OWNERS: dict[str, tuple[str, ...]] = {}

TASKS_OWNER = (
    "tasks.task:read", "tasks.task:write", "tasks.task:delete", "tasks.status:write", "tasks.calendar:write",
    "tasks.link:write", "tasks.archive:write", "tasks.reminder:write", "tasks.occurrence:write",
    "tasks.assignment:write", "tasks.suggestion:write",
)

CELL_OWNERS.update(tasks=TASKS_OWNER, decisions=DECISIONS_OWNER)
