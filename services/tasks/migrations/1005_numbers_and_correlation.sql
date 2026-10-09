-- Task numbers: one increasing number per workspace, kept in legacy_id. The
-- imported SQLite IDs keep their numbers and new tasks continue after them,
-- so SQLite tables that still reference tasks by integer keep working during
-- the transition and a rollback export needs no renumbering.
CREATE TABLE tasks.workspace_counters (
    workspace_id uuid PRIMARY KEY,
    next_number  bigint NOT NULL CHECK (next_number > 0)
);
ALTER TABLE tasks.workspace_counters ENABLE ROW LEVEL SECURITY;
ALTER TABLE tasks.workspace_counters FORCE ROW LEVEL SECURITY;
CREATE POLICY workspace_isolation ON tasks.workspace_counters
    USING (workspace_id = platform.current_workspace())
    WITH CHECK (workspace_id = platform.current_workspace());
GRANT SELECT, INSERT, UPDATE ON tasks.workspace_counters TO tasks_api;

CREATE FUNCTION tasks.assign_number() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.legacy_id IS NULL THEN
        INSERT INTO tasks.workspace_counters AS c (workspace_id, next_number)
        VALUES (NEW.workspace_id, 2)
        ON CONFLICT (workspace_id) DO UPDATE SET next_number = c.next_number + 1
        RETURNING c.next_number - 1 INTO NEW.legacy_id;
    ELSE
        INSERT INTO tasks.workspace_counters AS c (workspace_id, next_number)
        VALUES (NEW.workspace_id, NEW.legacy_id + 1)
        ON CONFLICT (workspace_id) DO UPDATE SET next_number = greatest(c.next_number, NEW.legacy_id + 1);
    END IF;
    RETURN NEW;
END
$$;
CREATE TRIGGER tasks_assign_number BEFORE INSERT ON tasks.tasks
    FOR EACH ROW EXECUTE FUNCTION tasks.assign_number();
GRANT EXECUTE ON FUNCTION tasks.assign_number() TO tasks_api;

-- Existing rows get numbers too (none in production; local data only).
UPDATE tasks.tasks SET legacy_id = sub.n
FROM (SELECT id, row_number() OVER (PARTITION BY workspace_id ORDER BY created_at, id) + 100000 AS n
      FROM tasks.tasks WHERE legacy_id IS NULL) sub
WHERE tasks.tasks.id = sub.id;
ALTER TABLE tasks.tasks ALTER COLUMN legacy_id SET NOT NULL;

ALTER TABLE tasks.outbox ADD COLUMN correlation_id text;
GRANT EXECUTE ON FUNCTION platform.begin_execution TO tasks_api;
