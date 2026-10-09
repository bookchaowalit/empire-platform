-- Write switch for cutover and rollback. While writes are disabled the Tasks
-- API answers 503 for every change, so nothing is written to PostgreSQL that
-- the rollback export would miss. Only the migrator role can flip it.
CREATE TABLE tasks.settings (
    key   text PRIMARY KEY,
    value text NOT NULL
);
INSERT INTO tasks.settings VALUES ('writes_enabled', 'true');
GRANT SELECT ON tasks.settings TO tasks_api;

-- Row lock taken as the owner so tasks_api needs no UPDATE privilege; it is
-- held until the API's transaction ends, so switching writes off waits for
-- writes already under way.
CREATE FUNCTION tasks.writes_enabled() RETURNS boolean
LANGUAGE sql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
    SELECT coalesce((SELECT value = 'true' FROM tasks.settings WHERE key = 'writes_enabled' FOR SHARE), false)
$$;
REVOKE ALL ON FUNCTION tasks.writes_enabled() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION tasks.writes_enabled() TO tasks_api;
