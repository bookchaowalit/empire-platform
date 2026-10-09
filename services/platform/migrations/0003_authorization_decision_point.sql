-- Policy decision point shared by every domain cell, API, and MCP adapter.
-- It lives in the database so any language calls the same rules; the
-- language-neutral request/decision contract is contracts/authz/v1.
--
-- Domain services get EXECUTE on these functions and no privileges on the
-- platform tables. The functions run as their owner, which is still bound by
-- the forced RLS policies, so they only ever see the current workspace.

CREATE TABLE platform.authorization_decisions (
    id                 uuid PRIMARY KEY DEFAULT platform.uuid_v7(),
    workspace_id       uuid NOT NULL REFERENCES platform.workspaces (id),
    actor_identity_id  uuid NOT NULL,
    action             text NOT NULL,
    resource_type      text NOT NULL,
    resource_id        text NOT NULL,
    delegation_id      uuid,
    on_behalf_of       uuid,
    approval_id        uuid,
    channel            text NOT NULL,
    decision           text NOT NULL CHECK (decision IN ('allow', 'deny', 'require_approval')),
    reason             text NOT NULL,
    locked             boolean NOT NULL,
    decided_at         timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON platform.authorization_decisions (workspace_id, decided_at);

ALTER TABLE platform.authorization_decisions ENABLE ROW LEVEL SECURITY;
ALTER TABLE platform.authorization_decisions FORCE ROW LEVEL SECURITY;
CREATE POLICY workspace_isolation ON platform.authorization_decisions
    USING (workspace_id = platform.current_workspace())
    WITH CHECK (workspace_id = platform.current_workspace());
CREATE TRIGGER authorization_decisions_append_only
    BEFORE UPDATE OR DELETE ON platform.authorization_decisions
    FOR EACH STATEMENT EXECUTE FUNCTION platform.reject_audit_change();

-- authorize: deny by default. p_lock = true is for the transaction that
-- performs the action: it takes FOR SHARE locks on the delegation, the
-- memberships, and the approval so a concurrent revocation either waits for
-- this transaction or is already visible to it (no time-of-check gap).
CREATE FUNCTION platform.authorize(
    p_actor             uuid,
    p_workspace         uuid,
    p_action            text,
    p_resource_type     text,
    p_resource_id       text,
    p_resource_workspace uuid,
    p_delegation        uuid DEFAULT NULL,
    p_on_behalf_of      uuid DEFAULT NULL,
    p_approval          uuid DEFAULT NULL,
    p_channel           text DEFAULT 'api',
    p_lock              boolean DEFAULT false
) RETURNS TABLE (decision text, reason text, decision_id uuid)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    v_decision text := 'deny';
    v_reason   text := 'no_permission';
    v_id       uuid;
    v_policy   boolean;
    v_approval text;
    v_grantor  uuid;
BEGIN
    -- The workspace in the request must be the one this transaction is bound to.
    IF platform.current_workspace() IS NULL OR p_workspace IS DISTINCT FROM platform.current_workspace() THEN
        RETURN QUERY SELECT 'deny'::text, 'workspace_mismatch'::text, NULL::uuid;
        RETURN;
    END IF;

    IF p_actor IS NULL OR p_resource_type IS NULL OR p_resource_id IS NULL
       OR p_action IS NULL OR p_action !~ '^[a-z][a-z0-9-]*\.[a-z][a-z0-9-]*:(read|write|delete|approve|admin)$'
       OR p_channel IS NULL OR p_channel NOT IN ('api', 'mcp', 'workflow', 'cli', 'event') THEN
        v_reason := 'invalid_request';
    ELSIF p_resource_workspace IS DISTINCT FROM p_workspace THEN
        -- Same answer as a resource that does not exist, so nothing leaks.
        v_reason := 'resource_not_found';
    ELSE
        IF p_lock THEN
            PERFORM 1 FROM platform.memberships
            WHERE workspace_id = p_workspace AND identity_id = p_actor FOR SHARE;
        END IF;

        IF p_delegation IS NOT NULL THEN
            SELECT d.grantor_identity_id INTO v_grantor
            FROM platform.delegations d
            WHERE d.id = p_delegation AND d.grantee_identity_id = p_actor
              AND d.revoked_at IS NULL AND d.expires_at > now();
            IF p_lock AND v_grantor IS NOT NULL THEN
                PERFORM 1 FROM platform.delegations WHERE id = p_delegation FOR SHARE;
                PERFORM 1 FROM platform.memberships
                WHERE workspace_id = p_workspace AND identity_id = v_grantor FOR SHARE;
            END IF;
            IF v_grantor IS NULL OR (p_on_behalf_of IS NOT NULL AND p_on_behalf_of <> v_grantor) THEN
                v_reason := 'delegation_invalid';
            ELSIF platform.has_permission(p_actor, p_action, p_delegation) THEN
                v_decision := 'allow';
            END IF;
        ELSIF p_on_behalf_of IS NOT NULL AND p_on_behalf_of <> p_actor THEN
            v_reason := 'delegation_invalid';
        ELSIF platform.has_permission(p_actor, p_action) THEN
            v_decision := 'allow';
        END IF;

        IF v_decision = 'allow' THEN
            v_reason := 'allowed';
            SELECT requires_approval INTO v_policy
            FROM platform.policies WHERE workspace_id = p_workspace AND permission = p_action;
            IF coalesce(v_policy, false) THEN
                SELECT a.decision INTO v_approval
                FROM platform.approvals a
                WHERE a.id = p_approval AND a.requested_action = p_action;
                IF p_lock AND v_approval IS NOT NULL THEN
                    PERFORM 1 FROM platform.approvals WHERE id = p_approval FOR SHARE;
                END IF;
                IF v_approval = 'approved' THEN
                    v_reason := 'allowed_with_approval';
                ELSIF v_approval = 'rejected' THEN
                    v_decision := 'deny';
                    v_reason := 'approval_rejected';
                ELSE
                    v_decision := 'require_approval';
                    v_reason := 'approval_required';
                END IF;
            END IF;
        END IF;
    END IF;

    INSERT INTO platform.authorization_decisions (
        workspace_id, actor_identity_id, action, resource_type, resource_id, delegation_id,
        on_behalf_of, approval_id, channel, decision, reason, locked)
    VALUES (
        p_workspace, coalesce(p_actor, '00000000-0000-0000-0000-000000000000'), coalesce(p_action, ''),
        coalesce(p_resource_type, ''), coalesce(p_resource_id, ''), p_delegation, p_on_behalf_of,
        p_approval, coalesce(p_channel, ''), v_decision, v_reason, p_lock)
    RETURNING id INTO v_id;

    RETURN QUERY SELECT v_decision, v_reason, v_id;
END
$$;

-- Execution and audit records for domain services that hold no platform grants.
CREATE FUNCTION platform.begin_execution(
    p_initiated_by uuid, p_executed_by uuid, p_on_behalf_of uuid, p_delegation uuid,
    p_approval uuid, p_channel text, p_idempotency_key text
) RETURNS uuid
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    v_id uuid;
BEGIN
    INSERT INTO platform.executions (
        workspace_id, initiated_by, executed_by, on_behalf_of, delegation_id, approval_id,
        channel, idempotency_key)
    VALUES (platform.current_workspace(), p_initiated_by, p_executed_by, p_on_behalf_of,
            p_delegation, p_approval, p_channel, p_idempotency_key)
    RETURNING id INTO v_id;
    RETURN v_id;
END
$$;

CREATE FUNCTION platform.find_execution(p_idempotency_key text)
RETURNS TABLE (id uuid, status text)
LANGUAGE sql STABLE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
    SELECT e.id, e.status FROM platform.executions e
    WHERE e.workspace_id = platform.current_workspace() AND e.idempotency_key = p_idempotency_key
$$;

CREATE FUNCTION platform.finish_execution(p_execution uuid, p_status text) RETURNS void
LANGUAGE sql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
    UPDATE platform.executions SET status = p_status, finished_at = now()
    WHERE id = p_execution AND workspace_id = platform.current_workspace()
$$;

CREATE FUNCTION platform.record_audit(
    p_execution uuid, p_service text, p_action text, p_resource_type text, p_resource_id text,
    p_before_hash text, p_after_hash text
) RETURNS uuid
LANGUAGE sql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
    INSERT INTO platform.audit_events (
        workspace_id, execution_id, service, action, resource_type, resource_id, before_hash, after_hash)
    VALUES (platform.current_workspace(), p_execution, p_service, p_action, p_resource_type,
            p_resource_id, p_before_hash, p_after_hash)
    RETURNING id
$$;

-- Domain service roles get only these entry points into platform.
REVOKE ALL ON FUNCTION platform.authorize, platform.begin_execution, platform.find_execution,
    platform.finish_execution, platform.record_audit FROM PUBLIC;
GRANT EXECUTE ON FUNCTION platform.authorize, platform.begin_execution, platform.find_execution,
    platform.finish_execution, platform.record_audit TO platform_api;
GRANT SELECT, INSERT ON platform.authorization_decisions TO platform_api;
