-- Correlation across MCP -> API -> outbox -> graph. Opaque, caller- or
-- adapter-generated, never derived from user content.
ALTER TABLE platform.executions ADD COLUMN correlation_id text
    CHECK (correlation_id ~ '^[A-Za-z0-9._:-]{8,128}$');
CREATE INDEX ON platform.executions (correlation_id);

DROP FUNCTION platform.begin_execution(uuid, uuid, uuid, uuid, uuid, text, text);
CREATE FUNCTION platform.begin_execution(
    p_initiated_by uuid, p_executed_by uuid, p_on_behalf_of uuid, p_delegation uuid,
    p_approval uuid, p_channel text, p_idempotency_key text, p_correlation_id text DEFAULT NULL
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
        channel, idempotency_key, correlation_id)
    VALUES (platform.current_workspace(), p_initiated_by, p_executed_by, p_on_behalf_of,
            p_delegation, p_approval, p_channel, p_idempotency_key, p_correlation_id)
    RETURNING id INTO v_id;
    RETURN v_id;
END
$$;
REVOKE ALL ON FUNCTION platform.begin_execution FROM PUBLIC;
GRANT EXECUTE ON FUNCTION platform.begin_execution TO platform_api;
