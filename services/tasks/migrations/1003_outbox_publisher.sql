-- Outbox publisher role for the tasks cell.
-- The tasks outbox publisher sees every workspace's outbox rows and nothing else.
CREATE POLICY publisher_all ON tasks.outbox TO tasks_publisher USING (true) WITH CHECK (true);
GRANT USAGE ON SCHEMA tasks TO tasks_publisher;
GRANT SELECT ON tasks.outbox TO tasks_publisher;
GRANT UPDATE (published_at, attempts, next_attempt_at, last_error) ON tasks.outbox TO tasks_publisher;
GRANT USAGE ON SCHEMA platform TO tasks_publisher;
GRANT EXECUTE ON FUNCTION platform.current_workspace() TO tasks_publisher;
