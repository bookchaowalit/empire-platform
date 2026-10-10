-- platform_monitor: cell executions left 'running' (a cell committed locally but
-- its finish call never arrived). Identifiers, service, status, and start time
-- only; never the bound resource, actors, or audit content.
GRANT SELECT (id, workspace_id, status, started_at) ON platform.executions TO platform_monitor;
CREATE POLICY monitor_read ON platform.executions FOR SELECT TO platform_monitor USING (true);

GRANT SELECT (execution_id, workspace_id, service) ON platform.cell_executions TO platform_monitor;
CREATE POLICY monitor_read ON platform.cell_executions FOR SELECT TO platform_monitor USING (true);
