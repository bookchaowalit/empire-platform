-- Reconcile cell executions whose finish call was lost.
--
-- A cell commits locally and then calls finish; when that call is lost the
-- execution stays 'running'. The owner closes it from the CLI
-- (empire-cells reconcile) after checking the cell's own database. The close
-- goes through platform.finish_cell_execution with the authority the execution
-- was started with, read from its binding, never from the caller: the audit
-- record can only name the bound service, entity, and resource, with a verb
-- the bound permission allows. A second audit record marks the close as a
-- reconciliation. Executions younger than 15 minutes are refused so a live
-- finish is never raced.
--
-- platform_api could already call finish_cell_execution with any actor and
-- authority, so these entry points give it no new power.

CREATE FUNCTION platform.stuck_cell_executions(p_service text, p_min_age interval)
RETURNS TABLE (execution_id uuid, permission text, resource_type text, resource_id text, started_at timestamptz)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
    SELECT b.execution_id, b.permission::text, b.resource_type, b.resource_id, e.started_at
      FROM platform.cell_executions b
      JOIN platform.executions e ON e.id = b.execution_id
     WHERE b.workspace_id = platform.current_workspace() AND e.workspace_id = platform.current_workspace()
       AND b.service = p_service AND e.status = 'running'
       AND e.started_at < now() - greatest(p_min_age, interval '15 minutes')
     ORDER BY e.started_at
$$;

-- Returns 'finished', 'not_found' (missing, other workspace or service),
-- 'too_recent', 'mismatch' (the action does not fit the bound operation), or
-- the existing status when the execution is no longer running.
CREATE FUNCTION platform.reconcile_cell_execution(p_execution uuid, p_service text, p_status text, p_action text)
RETURNS text
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    v_bound   record;
    v_outcome text;
BEGIN
    IF p_status NOT IN ('succeeded', 'failed') THEN
        RAISE EXCEPTION 'invalid status' USING ERRCODE = 'check_violation';
    END IF;
    IF p_status = 'failed' AND p_action IS NOT NULL THEN
        RAISE EXCEPTION 'a failed execution has no audit action' USING ERRCODE = 'check_violation';
    END IF;
    SELECT b.service, b.executed_by, b.delegation_id, b.on_behalf_of, b.resource_type, b.resource_id,
           e.status, e.started_at
      INTO v_bound
      FROM platform.cell_executions b
      JOIN platform.executions e ON e.id = b.execution_id
     WHERE b.execution_id = p_execution AND b.workspace_id = platform.current_workspace()
       AND e.workspace_id = platform.current_workspace() AND b.service = p_service;
    IF NOT FOUND THEN
        RETURN 'not_found';
    END IF;
    IF v_bound.status <> 'running' THEN
        RETURN v_bound.status;
    END IF;
    IF v_bound.started_at > now() - interval '15 minutes' THEN
        RETURN 'too_recent';
    END IF;
    -- Row hashes are unknown here: a reconciled success audits the operation without them.
    v_outcome := platform.finish_cell_execution(
        p_execution, v_bound.executed_by, v_bound.delegation_id, v_bound.on_behalf_of, NULL,
        p_status, v_bound.service, p_action, v_bound.resource_type, v_bound.resource_id, NULL, NULL);
    IF v_outcome = 'finished' THEN
        INSERT INTO platform.audit_events (workspace_id, execution_id, service, action, resource_type, resource_id)
        VALUES (platform.current_workspace(), p_execution, 'platform', 'platform.execution.reconciled',
                'execution', p_execution::text);
    END IF;
    RETURN v_outcome;
END
$$;

REVOKE ALL ON FUNCTION platform.stuck_cell_executions, platform.reconcile_cell_execution FROM PUBLIC;
GRANT EXECUTE ON FUNCTION platform.stuck_cell_executions, platform.reconcile_cell_execution TO platform_api;
