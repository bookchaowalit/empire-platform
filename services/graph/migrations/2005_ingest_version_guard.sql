-- Concurrent events for a subject the graph has not seen yet both find no
-- node to lock, so a lower version that commits second used to overwrite the
-- higher one. The node upserts now only replace an older version, and a
-- deletion retracts edges only when it actually wins. Since 2003 the body
-- lives in graph.ingest_event_v1 (graph.ingest_event dispatches 'unlinked'
-- and delegates the rest), so that is the function replaced; signature,
-- owner and grants are unchanged.
CREATE OR REPLACE FUNCTION graph.ingest_event_v1(p_event jsonb) RETURNS text
LANGUAGE plpgsql AS $$
DECLARE
    v_id       uuid := (p_event ->> 'id')::uuid;
    v_ws       uuid := (p_event ->> 'workspace_id')::uuid;
    v_type     text := p_event ->> 'type';
    v_subject  text := graph.resolve(p_event ->> 'subject');
    v_payload  jsonb := p_event -> 'payload';
    v_class    text := coalesce(p_event ->> 'classification', 'internal');
    v_version  bigint := coalesce((p_event -> 'payload' ->> 'version')::bigint, 0);
    v_parts    text[] := regexp_match(v_subject, '^empire:([a-z0-9-]+):([a-z0-9-]+):');
    v_verb     text := split_part(v_type, '.', 3);
    v_current  bigint;
    v_outcome  text;
    v_target   text;
    v_target_class text;
BEGIN
    IF v_ws IS DISTINCT FROM platform.current_workspace() THEN
        RAISE EXCEPTION 'event workspace does not match the bound workspace' USING ERRCODE = 'insufficient_privilege';
    END IF;
    INSERT INTO graph.inbox (consumer, event_id, workspace_id, outcome)
    VALUES ('graph', v_id, v_ws, 'pending')
    ON CONFLICT DO NOTHING;
    IF NOT FOUND THEN
        RETURN 'duplicate';
    END IF;

    SELECT version INTO v_current FROM graph.nodes WHERE urn = v_subject FOR UPDATE;

    IF v_verb = 'linked' THEN
        v_target := graph.resolve(v_payload ->> 'target_ref');
        IF NOT EXISTS (
            SELECT 1 FROM graph.relation_types r,
                 LATERAL (SELECT regexp_match(v_target, '^empire:([a-z0-9-]+):([a-z0-9-]+):') AS t) target
            WHERE r.relation = v_payload ->> 'relation'
              AND ('*' = ANY (r.from_types) OR v_parts[1] || ':' || v_parts[2] = ANY (r.from_types))
              AND ('*' = ANY (r.to_types) OR target.t[1] || ':' || target.t[2] = ANY (r.to_types))
        ) THEN
            UPDATE graph.inbox SET outcome = 'rejected_relation' WHERE consumer = 'graph' AND event_id = v_id;
            RETURN 'rejected_relation';
        END IF;
        SELECT classification INTO v_target_class FROM graph.nodes WHERE urn = v_target;
        INSERT INTO graph.edges (workspace_id, from_urn, to_urn, relation, asserted_by, classification,
                                 provenance, execution_id)
        VALUES (v_ws, v_subject, v_target, v_payload ->> 'relation', v_parts[1],
                CASE WHEN graph.rank(coalesce(v_target_class, v_class)) > graph.rank(v_class)
                     THEN v_target_class ELSE v_class END,
                jsonb_build_object('event_id', v_id, 'source', 'event'), (p_event ->> 'execution_id')::uuid)
        ON CONFLICT (workspace_id, from_urn, to_urn, relation, asserted_by)
        DO UPDATE SET retracted_at = NULL;
        v_outcome := 'edge';
    END IF;

    IF v_current IS NOT NULL AND v_version <= v_current THEN
        v_outcome := coalesce(v_outcome, 'stale');
    ELSIF v_verb = 'deleted' OR (v_payload ->> 'deleted')::boolean THEN
        INSERT INTO graph.nodes (urn, workspace_id, cell, type, label, classification, lifecycle, version, deleted_at)
        VALUES (v_subject, v_ws, v_parts[1], v_parts[2], NULL, v_class, 'deleted', v_version, now())
        ON CONFLICT (urn) DO UPDATE SET label = NULL, lifecycle = 'deleted', version = EXCLUDED.version,
            deleted_at = now(), updated_at = now()
        WHERE graph.nodes.version < EXCLUDED.version;
        IF FOUND THEN
            UPDATE graph.edges SET retracted_at = now()
            WHERE (from_urn = v_subject OR to_urn = v_subject) AND retracted_at IS NULL;
            v_outcome := 'deleted';
        ELSE
            v_outcome := 'stale';
        END IF;
    ELSIF v_verb <> 'linked' OR v_current IS NULL THEN
        INSERT INTO graph.nodes (urn, workspace_id, cell, type, label, classification, lifecycle, version)
        VALUES (v_subject, v_ws, v_parts[1], v_parts[2],
                CASE WHEN v_class = 'restricted' THEN NULL ELSE v_payload ->> 'title' END, v_class,
                CASE WHEN (v_payload ->> 'archived')::boolean THEN 'archived' ELSE 'active' END,
                greatest(v_version, 1))
        ON CONFLICT (urn) DO UPDATE SET label = EXCLUDED.label, classification = EXCLUDED.classification,
            lifecycle = EXCLUDED.lifecycle, version = EXCLUDED.version, updated_at = now(), deleted_at = NULL
        WHERE graph.nodes.version < EXCLUDED.version;
        v_outcome := coalesce(v_outcome, CASE WHEN FOUND THEN 'node' ELSE 'stale' END);
    END IF;

    UPDATE graph.inbox SET outcome = coalesce(v_outcome, 'ignored') WHERE consumer = 'graph' AND event_id = v_id;
    RETURN coalesce(v_outcome, 'ignored');
END
$$;
