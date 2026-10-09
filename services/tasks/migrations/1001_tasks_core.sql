-- Tasks cell schema: the reference implementation of the cell contract.
-- Spec: solo-empire docs/superpowers/specs/2026-10-09-domain-cells-empire-graph-design.md §3
-- Source mapping from SQLite: docs/migration-tasks-v1.md

CREATE SCHEMA tasks;

CREATE TABLE tasks.tasks (
    id                    uuid PRIMARY KEY DEFAULT platform.uuid_v7(),
    workspace_id          uuid NOT NULL,
    legacy_id             bigint,
    title                 text NOT NULL CHECK (length(title) BETWEEN 1 AND 500),
    description           text,
    status                text NOT NULL DEFAULT 'todo'
                          CHECK (status IN ('todo', 'in_progress', 'done', 'blocked', 'cancelled')),
    priority              smallint NOT NULL DEFAULT 3 CHECK (priority BETWEEN 1 AND 5),
    project               text,
    tags                  text[] NOT NULL DEFAULT '{}',
    category              text,
    due_date              date,
    completed_at          timestamptz,
    external_id           text,
    external_source       text,
    external_url          text,
    notes                 text,
    reminder_at           timestamptz,
    reminder_sent         boolean NOT NULL DEFAULT false,
    reminder_channels     text,
    estimated_minutes     integer CHECK (estimated_minutes >= 0),
    actual_minutes        integer NOT NULL DEFAULT 0 CHECK (actual_minutes >= 0),
    started_at            timestamptz,
    recurrence_rule       text,
    is_recurring_instance boolean NOT NULL DEFAULT false,
    recurrence_occurrence integer,
    parent_recurring_id   uuid,
    parent_task_id        uuid,
    archived_at           timestamptz,
    archive_reason        text,
    calendar_event_id     text,
    progress_percent      smallint NOT NULL DEFAULT 0 CHECK (progress_percent BETWEEN 0 AND 100),
    version               integer NOT NULL DEFAULT 1,
    created_at            timestamptz NOT NULL DEFAULT now(),
    updated_at            timestamptz NOT NULL DEFAULT now(),
    created_by_execution  uuid,
    updated_by_execution  uuid,
    UNIQUE (workspace_id, id),
    UNIQUE (workspace_id, legacy_id),
    FOREIGN KEY (workspace_id, parent_recurring_id) REFERENCES tasks.tasks (workspace_id, id),
    FOREIGN KEY (workspace_id, parent_task_id) REFERENCES tasks.tasks (workspace_id, id),
    CHECK (status <> 'done' OR completed_at IS NOT NULL)
);
CREATE INDEX ON tasks.tasks (workspace_id, status, due_date);

CREATE TABLE tasks.task_types (
    id               uuid PRIMARY KEY DEFAULT platform.uuid_v7(),
    workspace_id     uuid NOT NULL,
    legacy_id        bigint,
    name             text NOT NULL,
    description      text,
    icon             text,
    color            text,
    default_status   text NOT NULL DEFAULT 'todo',
    allowed_statuses text[] NOT NULL DEFAULT '{}',
    custom_template  jsonb,
    is_active        boolean NOT NULL DEFAULT true,
    sort_order       integer NOT NULL DEFAULT 0,
    created_at       timestamptz NOT NULL DEFAULT now(),
    UNIQUE (workspace_id, name),
    UNIQUE (workspace_id, legacy_id)
);

CREATE TABLE tasks.task_dependencies (
    id                 uuid PRIMARY KEY DEFAULT platform.uuid_v7(),
    workspace_id       uuid NOT NULL,
    task_id            uuid NOT NULL,
    depends_on_task_id uuid NOT NULL,
    dependency_type    text NOT NULL DEFAULT 'finish_to_start'
                       CHECK (dependency_type IN ('finish_to_start', 'start_to_start', 'finish_to_finish', 'start_to_finish')),
    created_at         timestamptz NOT NULL DEFAULT now(),
    UNIQUE (task_id, depends_on_task_id),
    CHECK (task_id <> depends_on_task_id),
    FOREIGN KEY (workspace_id, task_id) REFERENCES tasks.tasks (workspace_id, id) ON DELETE CASCADE,
    FOREIGN KEY (workspace_id, depends_on_task_id) REFERENCES tasks.tasks (workspace_id, id) ON DELETE CASCADE
);

CREATE TABLE tasks.task_comments (
    id                  uuid PRIMARY KEY DEFAULT platform.uuid_v7(),
    workspace_id        uuid NOT NULL,
    task_id             uuid NOT NULL,
    author              text,
    comment_type        text NOT NULL DEFAULT 'comment'
                        CHECK (comment_type IN ('comment', 'status_change', 'priority_change', 'assignment', 'system')),
    content             text NOT NULL,
    old_value           text,
    new_value           text,
    external_comment_id text,
    external_source     text,
    created_at          timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (workspace_id, task_id) REFERENCES tasks.tasks (workspace_id, id) ON DELETE CASCADE
);

CREATE TABLE tasks.task_attachments (
    id                     uuid PRIMARY KEY DEFAULT platform.uuid_v7(),
    workspace_id           uuid NOT NULL,
    task_id                uuid NOT NULL,
    file_name              text NOT NULL,
    file_ref               text NOT NULL,  -- artifact manifest reference, not a local path
    file_size              bigint CHECK (file_size >= 0),
    mime_type              text,
    description            text,
    uploaded_by            text,
    external_attachment_id text,
    external_source        text,
    created_at             timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (workspace_id, task_id) REFERENCES tasks.tasks (workspace_id, id) ON DELETE CASCADE
);

-- assignee_type 'identity' points at platform identities; 'role' and 'team'
-- keep the legacy soft reference until those cells exist.
CREATE TABLE tasks.task_assignments (
    id              uuid PRIMARY KEY DEFAULT platform.uuid_v7(),
    workspace_id    uuid NOT NULL,
    task_id         uuid NOT NULL,
    assignee_type   text NOT NULL CHECK (assignee_type IN ('identity', 'role', 'team')),
    assignee_ref    text NOT NULL,
    assignee_name   text,
    assignment_type text NOT NULL DEFAULT 'assigned' CHECK (assignment_type IN ('assigned', 'cc', 'viewer')),
    legacy_id       bigint,
    created_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (task_id, assignee_type, assignee_ref, assignment_type),
    FOREIGN KEY (workspace_id, task_id) REFERENCES tasks.tasks (workspace_id, id) ON DELETE CASCADE
);

-- Cross-cell links are soft references (URNs), never foreign keys.
-- Relation types follow the Empire Graph registry (spec §8.1).
CREATE TABLE tasks.task_links (
    id           uuid PRIMARY KEY DEFAULT platform.uuid_v7(),
    workspace_id uuid NOT NULL,
    task_id      uuid NOT NULL,
    relation     text NOT NULL CHECK (relation IN ('supports-goal', 'belongs-to', 'related-to', 'depends-on', 'part-of')),
    target_type  text NOT NULL CHECK (target_type IN ('goal', 'project', 'context', 'skill', 'task')),
    target_ref   text NOT NULL CHECK (target_ref ~ '^empire:[a-z0-9-]+:[a-z0-9-]+:[A-Za-z0-9:._-]+$'),
    attributes   jsonb NOT NULL DEFAULT '{}'::jsonb,
    legacy_table text,
    legacy_id    bigint,
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now(),
    UNIQUE (task_id, relation, target_ref),
    FOREIGN KEY (workspace_id, task_id) REFERENCES tasks.tasks (workspace_id, id) ON DELETE CASCADE
);

CREATE TABLE tasks.task_role_suggestions (
    id              uuid PRIMARY KEY DEFAULT platform.uuid_v7(),
    workspace_id    uuid NOT NULL,
    task_id         uuid NOT NULL,
    role_id         text NOT NULL,
    confidence      text NOT NULL CHECK (confidence IN ('high', 'medium', 'low')),
    match_reason    text NOT NULL,
    review_status   text NOT NULL DEFAULT 'suggested' CHECK (review_status IN ('suggested', 'accepted', 'dismissed')),
    source_registry text NOT NULL,
    legacy_id       bigint,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (task_id, role_id, source_registry),
    FOREIGN KEY (workspace_id, task_id) REFERENCES tasks.tasks (workspace_id, id) ON DELETE CASCADE
);

-- Transactional outbox: written in the same transaction as the change.
CREATE TABLE tasks.outbox (
    id             uuid PRIMARY KEY DEFAULT platform.uuid_v7(),
    workspace_id   uuid NOT NULL,
    type           text NOT NULL CHECK (type ~ '^tasks\.[a-z-]+\.[a-z-]+$'),
    version        integer NOT NULL DEFAULT 1,
    subject        text NOT NULL,
    execution_id   uuid NOT NULL,
    classification text NOT NULL DEFAULT 'internal'
                   CHECK (classification IN ('public', 'internal', 'confidential', 'restricted')),
    payload        jsonb NOT NULL,
    occurred_at    timestamptz NOT NULL DEFAULT now(),
    published_at   timestamptz
);
CREATE INDEX ON tasks.outbox (occurred_at) WHERE published_at IS NULL;

CREATE FUNCTION tasks.touch() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at := now();
    IF TG_TABLE_NAME = 'tasks' THEN
        NEW.version := OLD.version + 1;
    END IF;
    RETURN NEW;
END
$$;
CREATE TRIGGER tasks_touch BEFORE UPDATE ON tasks.tasks FOR EACH ROW EXECUTE FUNCTION tasks.touch();
CREATE TRIGGER task_links_touch BEFORE UPDATE ON tasks.task_links FOR EACH ROW EXECUTE FUNCTION tasks.touch();
CREATE TRIGGER task_role_suggestions_touch BEFORE UPDATE ON tasks.task_role_suggestions
    FOR EACH ROW EXECUTE FUNCTION tasks.touch();

DO $$
DECLARE
    t text;
BEGIN
    FOREACH t IN ARRAY ARRAY[
        'tasks', 'task_types', 'task_dependencies', 'task_comments', 'task_attachments',
        'task_assignments', 'task_links', 'task_role_suggestions', 'outbox'
    ] LOOP
        EXECUTE format('ALTER TABLE tasks.%I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('ALTER TABLE tasks.%I FORCE ROW LEVEL SECURITY', t);
        EXECUTE format(
            'CREATE POLICY workspace_isolation ON tasks.%I '
            'USING (workspace_id = platform.current_workspace()) '
            'WITH CHECK (workspace_id = platform.current_workspace())', t);
    END LOOP;
END
$$;

-- tasks_api: its own schema, and only the platform entry points.
GRANT USAGE ON SCHEMA tasks TO tasks_api;
GRANT SELECT, INSERT, UPDATE, DELETE ON tasks.tasks, tasks.task_types, tasks.task_dependencies,
    tasks.task_comments, tasks.task_attachments, tasks.task_assignments, tasks.task_links,
    tasks.task_role_suggestions TO tasks_api;
GRANT SELECT, INSERT ON tasks.outbox TO tasks_api;
GRANT UPDATE (published_at) ON tasks.outbox TO tasks_api;
GRANT EXECUTE ON FUNCTION tasks.touch() TO tasks_api;

GRANT USAGE ON SCHEMA platform TO tasks_api;
GRANT EXECUTE ON FUNCTION platform.current_workspace(), platform.uuid_v7(), platform.authorize,
    platform.begin_execution, platform.find_execution, platform.finish_execution,
    platform.record_audit TO tasks_api;
