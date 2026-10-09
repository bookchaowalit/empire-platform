-- DDL copied from the Solo Empire SQLite control plane (2026-10-09) for importer tests.
-- Referenced tables outside the tasks cell are not created; SQLite does not enforce
-- foreign keys unless PRAGMA foreign_keys is on.

CREATE TABLE tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    description TEXT,
    -- Status & priority
    status TEXT CHECK(status IN ('todo', 'in_progress', 'done', 'blocked', 'cancelled')) DEFAULT 'todo',
    priority INTEGER DEFAULT 3 CHECK(priority >= 1 AND priority <= 5),
    -- 1=highest, 5=lowest

    -- Classification
    project TEXT,
    tags TEXT,
    -- JSON array
    category TEXT,
    -- income, ops, learning, health, creative, admin

    -- Timeline
    due_date DATE,
    completed_at DATETIME,
    -- Sync metadata for bidirectional sync
    external_id TEXT,
    -- ID in external system (leantime ticket id, jira issue key, etc.)
    external_source TEXT,
    -- leantime, jira, notion, todoist, etc.
    external_url TEXT,
    -- URL to the task in external system
    sync_status TEXT CHECK(sync_status IN ('local', 'synced', 'dirty', 'conflict')) DEFAULT 'local',
    last_synced_at DATETIME,
    -- MD tracking
    md_hash TEXT,
    -- hash of MD line to detect local changes

    notes TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    reminder_at DATETIME,
    reminder_sent BOOLEAN DEFAULT 0,
    reminder_channels TEXT,
    estimated_minutes INTEGER,
    actual_minutes INTEGER DEFAULT 0,
    started_at DATETIME,
    recurrence_rule TEXT,
    is_recurring_instance BOOLEAN DEFAULT 0,
    recurrence_occurrence INTEGER,
    parent_recurring_id INTEGER REFERENCES tasks(id),
    archived_at DATETIME,
    archive_reason TEXT,
    parent_task_id INTEGER,
    calendar_event_id TEXT,
    progress_percent INTEGER NOT NULL DEFAULT 0 CHECK(progress_percent >= 0 AND progress_percent <= 100)
);

CREATE TABLE task_types (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    description TEXT,
    icon TEXT,
    -- emoji or icon name
  color TEXT,
    -- hex color
  default_status TEXT DEFAULT 'todo',
    allowed_statuses TEXT,
    -- JSON array of statuses for this type
  custom_template TEXT,
    -- JSON template for default values
  is_active INTEGER DEFAULT 1,
    sort_order INTEGER DEFAULT 0,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE task_assignments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    assignee_type TEXT CHECK(assignee_type IN ('user', 'team', 'role')) DEFAULT 'user',
    assignee_id TEXT NOT NULL,
    -- user ID, team ID, or role name
    assignee_name TEXT,
    assignment_type TEXT CHECK(assignment_type IN ('assigned', 'cc', 'viewer')) DEFAULT 'assigned',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(task_id, assignee_id, assignment_type)
);

CREATE TABLE task_dependencies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    depends_on_task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    dependency_type TEXT CHECK(dependency_type IN ('finish_to_start', 'start_to_start', 'finish_to_finish', 'start_to_finish')) DEFAULT 'finish_to_start',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(task_id, depends_on_task_id)
);

CREATE TABLE task_comments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    author TEXT,
    -- user/system identifier
    comment_type TEXT CHECK(comment_type IN ('comment', 'status_change', 'priority_change', 'assignment', 'system')) DEFAULT 'comment',
    content TEXT NOT NULL,
    old_value TEXT,
    new_value TEXT,
    external_comment_id TEXT,
    external_source TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE task_attachments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    file_name TEXT NOT NULL,
    file_path TEXT NOT NULL,
    file_size INTEGER,
    mime_type TEXT,
    description TEXT,
    uploaded_by TEXT,
    external_attachment_id TEXT,
    external_source TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE task_goal_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    goal_id INTEGER NOT NULL REFERENCES goals(id) ON DELETE CASCADE,
    link_type TEXT NOT NULL DEFAULT 'primary' CHECK(link_type IN ('primary', 'supporting')),
    source TEXT NOT NULL DEFAULT 'manual',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(task_id, goal_id)
);

CREATE TABLE task_project_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    relation_type TEXT NOT NULL DEFAULT 'delivery' CHECK(relation_type IN ('delivery', 'supporting')),
    weight REAL NOT NULL DEFAULT 1 CHECK(weight > 0),
    source TEXT NOT NULL DEFAULT 'manual',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(task_id, project_id)
);

CREATE TABLE task_context_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    context_id INTEGER NOT NULL REFERENCES portfolio_contexts(id) ON DELETE CASCADE,
    relation_type TEXT NOT NULL DEFAULT 'primary' CHECK(relation_type IN ('primary', 'supporting')),
    weight REAL NOT NULL DEFAULT 1 CHECK(weight > 0),
    source TEXT NOT NULL DEFAULT 'manual',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(task_id, context_id)
);

CREATE TABLE task_skill_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    skill_id INTEGER NOT NULL REFERENCES skills(id) ON DELETE CASCADE,
    link_type TEXT NOT NULL DEFAULT 'suggested' CHECK(link_type IN ('suggested', 'accepted', 'dismissed')),
    match_reason TEXT NOT NULL,
    source_registry TEXT NOT NULL DEFAULT 'docs/ROLE-LEARNING.yaml',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(task_id, skill_id, source_registry)
);

CREATE TABLE task_role_suggestions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    role_id TEXT NOT NULL,
    confidence TEXT NOT NULL CHECK(confidence IN ('high', 'medium', 'low')),
    match_reason TEXT NOT NULL,
    review_status TEXT NOT NULL DEFAULT 'suggested' CHECK(review_status IN ('suggested', 'accepted', 'dismissed')),
    source_registry TEXT NOT NULL DEFAULT 'docs/ROLE-TASK-ROUTING.yaml',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(task_id, role_id, source_registry)
);

CREATE TABLE work_item_task_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    work_item_id INTEGER NOT NULL REFERENCES portfolio_work_items(id) ON DELETE CASCADE,
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    relation_type TEXT NOT NULL DEFAULT 'implements' CHECK(relation_type IN ('implements', 'supports')),
    weight REAL NOT NULL DEFAULT 1 CHECK(weight > 0),
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(work_item_id, task_id)
);
