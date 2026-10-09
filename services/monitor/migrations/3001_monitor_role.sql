-- platform_monitor: the daily check across workspaces. Column grants keep it
-- to identifiers, counts, versions, timestamps, and decision codes; it cannot
-- read titles, payloads, error text, or anything it could change.
GRANT USAGE ON SCHEMA platform, tasks, graph TO platform_monitor;

GRANT SELECT (id, slug, status) ON platform.workspaces TO platform_monitor;
CREATE POLICY monitor_read ON platform.workspaces FOR SELECT TO platform_monitor USING (true);

GRANT SELECT (workspace_id, legacy_id, version, status, archived_at) ON tasks.tasks TO platform_monitor;
CREATE POLICY monitor_read ON tasks.tasks FOR SELECT TO platform_monitor USING (true);

GRANT SELECT (id, workspace_id, type, occurred_at, published_at, attempts, next_attempt_at)
    ON tasks.outbox TO platform_monitor;
CREATE POLICY monitor_read ON tasks.outbox FOR SELECT TO platform_monitor USING (true);

GRANT SELECT ON tasks.settings TO platform_monitor;

GRANT SELECT (workspace_id, action, channel, decision, reason, decided_at)
    ON platform.authorization_decisions TO platform_monitor;
CREATE POLICY monitor_read ON platform.authorization_decisions FOR SELECT TO platform_monitor USING (true);

GRANT SELECT (consumer, event_id, workspace_id, outcome, processed_at) ON graph.inbox TO platform_monitor;
CREATE POLICY monitor_read ON graph.inbox FOR SELECT TO platform_monitor USING (true);
