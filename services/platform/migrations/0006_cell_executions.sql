-- Executions for cells that keep their own database (platform over HTTP).
--
-- Each execution started over HTTP is bound to the operation it was
-- authorized for (service, permission, resource, delegation context). A replay
-- with the same idempotency key must ask for exactly the same operation, and
-- finishing may only audit that operation, so a read grant can never be
-- turned into a write record. The HTTP routes run as a domain service role
-- that holds only these entry points, never platform table grants.

CREATE TABLE platform.cell_executions (
    execution_id  uuid PRIMARY KEY,
    workspace_id  uuid NOT NULL REFERENCES platform.workspaces (id),
    service       text NOT NULL CHECK (service ~ '^[a-z][a-z0-9-]*$'),
    permission    platform.permission NOT NULL,
    -- Further permissions the same operation needed (enforced with locks together at begin).
    also_permissions platform.permission[] NOT NULL DEFAULT '{}',
    resource_type text NOT NULL,
    resource_id   text NOT NULL,
    delegation_id uuid,
    on_behalf_of  uuid NOT NULL,
    approval_id   uuid,
    executed_by   uuid NOT NULL,
    idempotency_key text CHECK (idempotency_key IS NULL OR length(idempotency_key) BETWEEN 8 AND 200),
    created_at    timestamptz NOT NULL DEFAULT now(),
    CHECK (split_part(permission, '.', 1) = service),
    CHECK (permission !~ ':read$')
);
-- Keys live here, per service and actor, never in platform.executions, so no other
-- producer (the tasks API stores raw keys there) can collide with them.
CREATE UNIQUE INDEX cell_executions_key ON platform.cell_executions (workspace_id, service, executed_by, idempotency_key)
    WHERE idempotency_key IS NOT NULL;
ALTER TABLE platform.cell_executions ENABLE ROW LEVEL SECURITY;
ALTER TABLE platform.cell_executions FORCE ROW LEVEL SECURITY;
CREATE POLICY workspace_isolation ON platform.cell_executions
    USING (workspace_id = platform.current_workspace())
    WITH CHECK (workspace_id = platform.current_workspace());

-- Start (or replay) an execution bound to one operation. The idempotency key
-- is kept per service and actor in cell_executions, so it never collides with
-- another producer's keys. Raises 'idempotency_conflict' when a replay asks for a
-- different operation. The caller must still enforce the decision with locks
-- in the same transaction for a new execution.
CREATE FUNCTION platform.begin_cell_execution(
    p_initiated_by uuid, p_executed_by uuid, p_on_behalf_of uuid, p_delegation uuid, p_approval uuid,
    p_channel text, p_idempotency_key text, p_correlation_id text,
    p_service text, p_permission text, p_resource_type text, p_resource_id text, p_also text[]
) RETURNS TABLE (execution_id uuid, replayed boolean, status text)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    v_found record;
    v_id    uuid;
BEGIN
    IF p_idempotency_key IS NOT NULL THEN
        SELECT e.id, e.status, e.executed_by, b.service, b.permission, b.resource_type, b.resource_id,
               b.delegation_id, b.on_behalf_of, b.approval_id, b.also_permissions
          INTO v_found
          FROM platform.cell_executions b
          JOIN platform.executions e ON e.id = b.execution_id
         WHERE b.workspace_id = platform.current_workspace() AND b.service = p_service
           AND b.executed_by = p_executed_by AND b.idempotency_key = p_idempotency_key;
        IF FOUND THEN
            IF v_found.executed_by IS DISTINCT FROM p_executed_by OR v_found.service IS DISTINCT FROM p_service
               OR v_found.permission IS DISTINCT FROM p_permission
               OR v_found.resource_type IS DISTINCT FROM p_resource_type
               OR v_found.resource_id IS DISTINCT FROM p_resource_id
               OR v_found.delegation_id IS DISTINCT FROM p_delegation
               OR v_found.on_behalf_of IS DISTINCT FROM p_on_behalf_of
               OR v_found.approval_id IS DISTINCT FROM p_approval
               OR v_found.also_permissions::text[] IS DISTINCT FROM coalesce(p_also, '{}') THEN
                RAISE EXCEPTION 'idempotency_conflict' USING ERRCODE = 'unique_violation';
            END IF;
            RETURN QUERY SELECT v_found.id, true, v_found.status;
            RETURN;
        END IF;
    END IF;
    v_id := platform.begin_execution(p_initiated_by, p_executed_by, p_on_behalf_of, p_delegation, p_approval,
                                     p_channel, NULL, p_correlation_id);
    INSERT INTO platform.cell_executions (execution_id, workspace_id, service, permission, resource_type,
                                          resource_id, delegation_id, on_behalf_of, approval_id, executed_by,
                                          idempotency_key, also_permissions)
    VALUES (v_id, platform.current_workspace(), p_service, p_permission, p_resource_type, p_resource_id,
            p_delegation, p_on_behalf_of, p_approval, p_executed_by, p_idempotency_key,
            coalesce(p_also, '{}')::platform.permission[]);
    RETURN QUERY SELECT v_id, false, 'running'::text;
END
$$;

-- Finish an execution the caller runs, auditing only the bound operation.
-- The caller must present the same authority context (delegation, on-behalf-of)
-- the execution was started with; p_scopes, when the token carries scopes, must
-- include the bound permission. The audit action (decisions.decision.created)
-- must be in the service's namespace, name the bound resource exactly, and use
-- a verb the bound permission allows: "deleted" needs :delete, "approved" and
-- "rejected" need :approve. Returns 'finished', 'not_found' (missing, other
-- workspace, actor or authority), 'mismatch', or the existing status (replay).
CREATE FUNCTION platform.finish_cell_execution(
    p_execution uuid, p_actor uuid, p_delegation uuid, p_on_behalf_of uuid, p_scopes text[],
    p_status text, p_service text, p_action text,
    p_resource_type text, p_resource_id text, p_before_hash text, p_after_hash text
) RETURNS text
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    v_actor  uuid;
    v_status text;
    v_bound  record;
    v_verb   text := split_part(p_action, '.', 3);
    v_right  text;
BEGIN
    IF p_status NOT IN ('succeeded', 'failed') THEN
        RAISE EXCEPTION 'invalid status' USING ERRCODE = 'check_violation';
    END IF;
    SELECT executed_by, status INTO v_actor, v_status FROM platform.executions
    WHERE id = p_execution AND workspace_id = platform.current_workspace()
    FOR UPDATE;
    IF NOT FOUND OR v_actor IS DISTINCT FROM p_actor THEN
        RETURN 'not_found';
    END IF;
    SELECT * INTO v_bound FROM platform.cell_executions WHERE execution_id = p_execution;
    IF NOT FOUND OR v_bound.delegation_id IS DISTINCT FROM p_delegation
       OR v_bound.on_behalf_of IS DISTINCT FROM p_on_behalf_of
       OR (p_scopes IS NOT NULL AND NOT (v_bound.permission = ANY (p_scopes))) THEN
        RETURN 'not_found';
    END IF;
    IF v_status <> 'running' THEN
        RETURN v_status;
    END IF;
    IF p_status = 'succeeded' THEN
        v_right := split_part(v_bound.permission, ':', 2);
        IF p_service IS DISTINCT FROM v_bound.service
           OR p_action IS NULL OR p_action !~ '^[a-z][a-z0-9-]*\.[a-z][a-z0-9-]*\.[a-z][a-z0-9-]*$'
           OR split_part(p_action, '.', 1) IS DISTINCT FROM v_bound.service
           -- the audited entity is the one the permission is for (decisions.review:write audits reviews only)
           OR split_part(p_action, '.', 2) IS DISTINCT FROM split_part(split_part(v_bound.permission, ':', 1), '.', 2)
           OR p_resource_type IS DISTINCT FROM v_bound.resource_type
           OR p_resource_id IS DISTINCT FROM v_bound.resource_id
           OR (v_verb = 'deleted' AND v_right <> 'delete')
           OR (v_verb IN ('approved', 'rejected') AND v_right <> 'approve') THEN
            RETURN 'mismatch';
        END IF;
        INSERT INTO platform.audit_events (
            workspace_id, execution_id, service, action, resource_type, resource_id, before_hash, after_hash)
        VALUES (platform.current_workspace(), p_execution, v_bound.service, p_action, v_bound.resource_type,
                v_bound.resource_id, p_before_hash, p_after_hash);
    END IF;
    UPDATE platform.executions SET status = p_status, finished_at = now() WHERE id = p_execution;
    RETURN 'finished';
END
$$;

REVOKE ALL ON FUNCTION platform.begin_cell_execution, platform.finish_cell_execution FROM PUBLIC;
GRANT EXECUTE ON FUNCTION platform.begin_cell_execution, platform.finish_cell_execution TO platform_api, tasks_api;
