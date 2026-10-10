-- Ingest for cells that publish over HTTP. One entry point, so the process
-- serving the HTTP route needs no graph table grants: it checks the bound
-- workspace and, after resolving aliases, that the event's subject (and the
-- event type) belong to the publishing cell, then applies the event.
CREATE FUNCTION graph.ingest_cell_event(p_event jsonb, p_cell text) RETURNS text
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    v_outcome text;
    v_subject text;
BEGIN
    IF p_cell IS NULL OR p_cell !~ '^[a-z][a-z0-9-]*$' THEN
        RAISE EXCEPTION 'invalid cell' USING ERRCODE = 'invalid_parameter_value';
    END IF;
    IF (p_event ->> 'workspace_id')::uuid IS DISTINCT FROM platform.current_workspace() THEN
        RAISE EXCEPTION 'event workspace does not match the bound workspace' USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF split_part(p_event ->> 'type', '.', 1) IS DISTINCT FROM p_cell THEN
        RAISE EXCEPTION 'event type outside the cell' USING ERRCODE = 'insufficient_privilege';
    END IF;
    v_subject := graph.resolve(p_event ->> 'subject');
    IF v_subject IS NULL OR split_part(v_subject, ':', 2) IS DISTINCT FROM p_cell
       OR split_part(p_event ->> 'subject', ':', 2) IS DISTINCT FROM p_cell THEN
        RAISE EXCEPTION 'subject outside the cell' USING ERRCODE = 'insufficient_privilege';
    END IF;
    v_outcome := graph.ingest_event(p_event);
    IF v_outcome <> 'duplicate' THEN
        PERFORM graph.record_correlation(p_event);
    END IF;
    RETURN v_outcome;
END
$$;

REVOKE ALL ON FUNCTION graph.ingest_cell_event(jsonb, text) FROM PUBLIC;
GRANT USAGE ON SCHEMA graph TO tasks_api;
GRANT EXECUTE ON FUNCTION graph.ingest_cell_event(jsonb, text) TO tasks_api;
