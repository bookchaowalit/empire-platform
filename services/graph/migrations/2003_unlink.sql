-- A cell may withdraw a link it asserted (tasks.task.unlinked). The edge is
-- retracted, not deleted, so history keeps that it once existed; a later
-- 'linked' event for the same edge clears retracted_at again.
ALTER FUNCTION graph.ingest_event(jsonb) RENAME TO ingest_event_v1;

CREATE FUNCTION graph.ingest_event(p_event jsonb) RETURNS text
LANGUAGE plpgsql AS $$
DECLARE
    v_id      uuid := (p_event ->> 'id')::uuid;
    v_ws      uuid := (p_event ->> 'workspace_id')::uuid;
    v_subject text;
    v_parts   text[];
BEGIN
    IF split_part(p_event ->> 'type', '.', 3) <> 'unlinked' THEN
        RETURN graph.ingest_event_v1(p_event);
    END IF;
    IF v_ws IS DISTINCT FROM platform.current_workspace() THEN
        RAISE EXCEPTION 'event workspace does not match the bound workspace' USING ERRCODE = 'insufficient_privilege';
    END IF;
    INSERT INTO graph.inbox (consumer, event_id, workspace_id, outcome)
    VALUES ('graph', v_id, v_ws, 'pending')
    ON CONFLICT DO NOTHING;
    IF NOT FOUND THEN
        RETURN 'duplicate';
    END IF;
    v_subject := graph.resolve(p_event ->> 'subject');
    v_parts := regexp_match(v_subject, '^empire:([a-z0-9-]+):([a-z0-9-]+):');
    UPDATE graph.edges SET retracted_at = now()
    WHERE workspace_id = v_ws AND from_urn = v_subject
      AND to_urn = graph.resolve(p_event -> 'payload' ->> 'target_ref')
      AND relation = p_event -> 'payload' ->> 'relation'
      AND asserted_by = v_parts[1] AND retracted_at IS NULL;
    UPDATE graph.inbox SET outcome = 'unlinked' WHERE consumer = 'graph' AND event_id = v_id;
    RETURN 'unlinked';
END
$$;
