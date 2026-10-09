"""Permission sets used by the platform's own tooling and tests.

Write access to tasks is split by operation so automations get only what they
use: ``tasks.task:write`` (create, upsert, general edits), ``tasks.status:write``
(status, due date, progress, complete), ``tasks.calendar:write``,
``tasks.link:write``, ``tasks.archive:write``, ``tasks.reminder:write``, and
``tasks.occurrence:write``.
"""

TASKS_OWNER = (
    "tasks.task:read", "tasks.task:write", "tasks.task:delete", "tasks.status:write", "tasks.calendar:write",
    "tasks.link:write", "tasks.archive:write", "tasks.reminder:write", "tasks.occurrence:write",
)
