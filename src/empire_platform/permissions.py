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

HABITS_OWNER = (
    "habits.habit:read", "habits.habit:write", "habits.habit:delete",
    "habits.routine:read", "habits.routine:write", "habits.routine:delete", "graph.habits:write",
)

NOTIFICATIONS_OWNER = (
    "notifications.rule:read", "notifications.rule:write",
    "notifications.notification:read", "notifications.notification:write",
)

GOALS_OWNER = (
    "goals.goal:read", "goals.goal:write", "goals.goal:delete",
    "goals.goal-task:read", "goals.goal-task:write", "goals.goal-task:delete",
    "goals.goal-metric:read", "goals.goal-metric:write", "goals.goal-metric:delete",
    "goals.goal-portfolio-mapping:read", "goals.goal-portfolio-mapping:write", "goals.goal-portfolio-mapping:delete",
    "goals.productivity-goal:read", "goals.productivity-goal:write", "goals.productivity-goal:delete",
    "goals.goal-project-link:read", "goals.goal-project-link:write", "goals.goal-project-link:delete",
    "goals.goal-context-link:read", "goals.goal-context-link:write", "goals.goal-context-link:delete",
    "goals.goal-task-project-link:read", "goals.goal-task-project-link:write", "goals.goal-task-project-link:delete",
    "graph.goals:write",
)

PROJECTS_OWNER = (
    "projects.project:read", "projects.project:write", "projects.project:delete",
    "projects.project-repo:read", "projects.project-repo:write", "projects.project-repo:delete",
    "projects.portfolio-context:read", "projects.portfolio-context:write", "projects.portfolio-context:delete",
    "projects.project-context-link:read", "projects.project-context-link:write", "projects.project-context-link:delete",
    "projects.work-item:read", "projects.work-item:write", "projects.work-item:delete",
    "projects.work-item-task-link:read", "projects.work-item-task-link:write", "projects.work-item-task-link:delete",
    "projects.work-item-goal-task-link:read", "projects.work-item-goal-task-link:write", "projects.work-item-goal-task-link:delete",
    "graph.projects:write",
)

ROLES_SKILLS_OWNER = (
    "roles-skills.personal-role:read", "roles-skills.personal-role:write", "roles-skills.personal-role:delete",
    "roles-skills.personal-role-context-link:read", "roles-skills.personal-role-context-link:write", "roles-skills.personal-role-context-link:delete",
    "roles-skills.personal-role-goal-link:read", "roles-skills.personal-role-goal-link:write", "roles-skills.personal-role-goal-link:delete",
    "roles-skills.personal-role-project-link:read", "roles-skills.personal-role-project-link:write", "roles-skills.personal-role-project-link:delete",
    "roles-skills.personal-role-task-link:read", "roles-skills.personal-role-task-link:write", "roles-skills.personal-role-task-link:delete",
    "roles-skills.role-goal-link:read", "roles-skills.role-goal-link:write", "roles-skills.role-goal-link:delete",
    "roles-skills.skill-category:read", "roles-skills.skill-category:write", "roles-skills.skill-category:delete",
    "roles-skills.skill:read", "roles-skills.skill:write", "roles-skills.skill:delete",
    "roles-skills.role-path-step:read", "roles-skills.role-path-step:write", "roles-skills.role-path-step:delete",
    "roles-skills.role-project-allocation:read", "roles-skills.role-project-allocation:write", "roles-skills.role-project-allocation:delete",
    "roles-skills.role-skill-link:read", "roles-skills.role-skill-link:write", "roles-skills.role-skill-link:delete",
    "graph.roles-skills:write",
)

ACCOUNTS_OWNER = tuple(
    f"accounts.{resource}:{right}"
    for resource in ("identity", "domain", "mailbox", "account")
    for right in ("read", "write", "delete")
)

# Owner permissions per cell; `empire-cells grant` adds them to a workspace's owner role.
CELL_OWNERS: dict[str, tuple[str, ...]] = {}

TASKS_OWNER = (
    "tasks.task:read", "tasks.task:write", "tasks.task:delete", "tasks.status:write", "tasks.calendar:write",
    "tasks.link:write", "tasks.archive:write", "tasks.reminder:write", "tasks.occurrence:write",
    "tasks.assignment:write", "tasks.suggestion:write",
)

CELL_OWNERS.update(tasks=TASKS_OWNER, decisions=DECISIONS_OWNER, habits=HABITS_OWNER,
                   notifications=NOTIFICATIONS_OWNER, goals=GOALS_OWNER, projects=PROJECTS_OWNER)
CELL_OWNERS["roles-skills"] = ROLES_SKILLS_OWNER
CELL_OWNERS["accounts"] = ACCOUNTS_OWNER
