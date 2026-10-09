-- What the SQLite writers need before a cutover: stable external keys,
-- retry-safe recurring occurrences and reminder deliveries, and outbox
-- delivery bookkeeping for retries.

-- One task per external record (task-sync, engagement, Gmail, sales, decision syncs).
CREATE UNIQUE INDEX tasks_external_key ON tasks.tasks (workspace_id, external_source, external_id)
    WHERE external_source IS NOT NULL AND external_id IS NOT NULL;

-- One instance per occurrence of a recurring task.
CREATE UNIQUE INDEX tasks_recurrence_occurrence ON tasks.tasks (parent_recurring_id, recurrence_occurrence)
    WHERE parent_recurring_id IS NOT NULL;

-- One delivery per reminder time and channel, however often the notifier retries.
CREATE TABLE tasks.reminder_deliveries (
    id           uuid PRIMARY KEY DEFAULT platform.uuid_v7(),
    workspace_id uuid NOT NULL,
    task_id      uuid NOT NULL,
    reminder_at  timestamptz NOT NULL,
    channel      text NOT NULL CHECK (channel ~ '^[a-z][a-z0-9-]{0,31}$'),
    execution_id uuid NOT NULL,
    delivered_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (task_id, reminder_at, channel),
    FOREIGN KEY (workspace_id, task_id) REFERENCES tasks.tasks (workspace_id, id) ON DELETE CASCADE
);
ALTER TABLE tasks.reminder_deliveries ENABLE ROW LEVEL SECURITY;
ALTER TABLE tasks.reminder_deliveries FORCE ROW LEVEL SECURITY;
CREATE POLICY workspace_isolation ON tasks.reminder_deliveries
    USING (workspace_id = platform.current_workspace())
    WITH CHECK (workspace_id = platform.current_workspace());
GRANT SELECT, INSERT ON tasks.reminder_deliveries TO tasks_api;

-- Delivery bookkeeping for the outbox publisher.
ALTER TABLE tasks.outbox
    ADD COLUMN attempts        integer NOT NULL DEFAULT 0,
    ADD COLUMN next_attempt_at timestamptz NOT NULL DEFAULT now(),
    ADD COLUMN last_error      text;
GRANT UPDATE (attempts, next_attempt_at, last_error) ON tasks.outbox TO tasks_api;
