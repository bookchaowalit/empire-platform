-- Empire Graph, minimal: nodes, aliases, edges, relation types, consents,
-- and a consumer inbox, all workspace-isolated. Built only from cell events;
-- never on the critical path of a cell.
-- Spec: solo-empire docs/superpowers/specs/2026-10-09-domain-cells-empire-graph-design.md §5, §8

CREATE SCHEMA graph;

-- Read check without a decision record (graph filters many nodes per query).
CREATE FUNCTION platform.can(p_actor uuid, p_permission text, p_delegation uuid DEFAULT NULL) RETURNS boolean
LANGUAGE sql STABLE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
    SELECT p_permission ~ '^[a-z][a-z0-9-]*\.[a-z][a-z0-9-]*:(read|write|delete|approve|admin)$'
       AND platform.has_permission(p_actor, p_permission, p_delegation)
$$;
REVOKE ALL ON FUNCTION platform.can FROM PUBLIC;

CREATE TABLE graph.relation_types (
    relation    text PRIMARY KEY,
    from_types  text[] NOT NULL,  -- '<cell>:<type>' or '*'
    to_types    text[] NOT NULL,
    description text NOT NULL
);
INSERT INTO graph.relation_types VALUES
    ('belongs-to',    '{*}', '{*}', 'child -> container'),
    ('part-of',       '{*}', '{*}', 'part -> whole'),
    ('depends-on',    '{*}', '{*}', 'waiting entity -> prerequisite'),
    ('supports-goal', '{tasks:task,habits:habit,projects:project,learning:item}', '{goals:goal}', 'work -> goal it helps'),
    ('tracks-goal',   '{habits:log,diary:entry,finance:budget,operating:metric}', '{goals:goal}', 'measure -> goal'),
    ('related-to',    '{*}', '{*}', 'loose association, symmetric'),
    ('produced-by',   '{*}', '{*}', 'output -> producer'),
    ('assigned-to',   '{tasks:task}', '{platform:identity}', 'work -> responsible identity');

CREATE TABLE graph.nodes (
    urn            text PRIMARY KEY CHECK (urn ~ '^empire:[a-z0-9-]+:[a-z0-9-]+:[A-Za-z0-9:._-]+$'),
    workspace_id   uuid NOT NULL,
    cell           text NOT NULL,
    type           text NOT NULL,
    label          text,
    classification text NOT NULL CHECK (classification IN ('public', 'internal', 'confidential', 'restricted')),
    lifecycle      text NOT NULL CHECK (lifecycle IN ('active', 'archived', 'deleted')),
    version        bigint NOT NULL,
    updated_at     timestamptz NOT NULL DEFAULT now(),
    deleted_at     timestamptz
);
CREATE INDEX ON graph.nodes (workspace_id, cell, type);

CREATE TABLE graph.aliases (
    alias_urn    text PRIMARY KEY,
    workspace_id uuid NOT NULL,
    urn          text NOT NULL
);

CREATE TABLE graph.edges (
    id             uuid PRIMARY KEY DEFAULT platform.uuid_v7(),
    workspace_id   uuid NOT NULL,
    from_urn       text NOT NULL,
    to_urn         text NOT NULL,
    relation       text NOT NULL REFERENCES graph.relation_types (relation),
    asserted_by    text NOT NULL,
    classification text NOT NULL CHECK (classification IN ('public', 'internal', 'confidential', 'restricted')),
    provenance     jsonb NOT NULL DEFAULT '{}'::jsonb,
    execution_id   uuid,
    valid_from     timestamptz NOT NULL DEFAULT now(),
    retracted_at   timestamptz,
    UNIQUE (workspace_id, from_urn, to_urn, relation, asserted_by)
);
CREATE INDEX ON graph.edges (workspace_id, to_urn);

-- Explicit consent to link a restricted entity (spec §8.2).
CREATE TABLE graph.consents (
    id           uuid PRIMARY KEY DEFAULT platform.uuid_v7(),
    workspace_id uuid NOT NULL,
    granted_by   uuid NOT NULL,
    relation     text NOT NULL REFERENCES graph.relation_types (relation),
    subject_urn  text NOT NULL,
    expires_at   timestamptz NOT NULL,
    revoked_at   timestamptz,
    created_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE graph.inbox (
    consumer     text NOT NULL,
    event_id     uuid NOT NULL,
    workspace_id uuid NOT NULL,
    outcome      text NOT NULL,
    processed_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (consumer, event_id)
);

DO $$
DECLARE
    t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['nodes', 'aliases', 'edges', 'consents', 'inbox'] LOOP
        EXECUTE format('ALTER TABLE graph.%I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('ALTER TABLE graph.%I FORCE ROW LEVEL SECURITY', t);
        EXECUTE format(
            'CREATE POLICY workspace_isolation ON graph.%I '
            'USING (workspace_id = platform.current_workspace()) '
            'WITH CHECK (workspace_id = platform.current_workspace())', t);
    END LOOP;
END
$$;

CREATE FUNCTION graph.rank(p_classification text) RETURNS int LANGUAGE sql IMMUTABLE AS $$
    SELECT array_position(ARRAY['public', 'internal', 'confidential', 'restricted'], p_classification)
$$;

CREATE FUNCTION graph.resolve(p_urn text) RETURNS text LANGUAGE sql STABLE AS $$
    SELECT coalesce((SELECT urn FROM graph.aliases WHERE alias_urn = p_urn), p_urn)
$$;

-- Apply one cell event. Duplicates are no-ops (inbox), and events older than
-- the node's version are ignored, so redelivery and reordering are safe.
CREATE FUNCTION graph.ingest_event(p_event jsonb) RETURNS text
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
            deleted_at = now(), updated_at = now();
        UPDATE graph.edges SET retracted_at = now()
        WHERE (from_urn = v_subject OR to_urn = v_subject) AND retracted_at IS NULL;
        v_outcome := 'deleted';
    ELSIF v_verb <> 'linked' OR v_current IS NULL THEN
        INSERT INTO graph.nodes (urn, workspace_id, cell, type, label, classification, lifecycle, version)
        VALUES (v_subject, v_ws, v_parts[1], v_parts[2],
                CASE WHEN v_class = 'restricted' THEN NULL ELSE v_payload ->> 'title' END, v_class,
                CASE WHEN (v_payload ->> 'archived')::boolean THEN 'archived' ELSE 'active' END,
                greatest(v_version, 1))
        ON CONFLICT (urn) DO UPDATE SET label = EXCLUDED.label, classification = EXCLUDED.classification,
            lifecycle = EXCLUDED.lifecycle, version = EXCLUDED.version, updated_at = now(), deleted_at = NULL;
        v_outcome := coalesce(v_outcome, 'node');
    END IF;

    UPDATE graph.inbox SET outcome = coalesce(v_outcome, 'ignored') WHERE consumer = 'graph' AND event_id = v_id;
    RETURN coalesce(v_outcome, 'ignored');
END
$$;

-- Visibility: the caller may read the node's cell/type, the node is not
-- deleted, and restricted edges need a live consent for that relation.
CREATE FUNCTION graph.node_visible(p_urn text, p_actor uuid, p_delegation uuid) RETURNS boolean
LANGUAGE sql STABLE AS $$
    SELECT EXISTS (
        SELECT 1 FROM graph.nodes n
        WHERE n.urn = p_urn AND n.lifecycle <> 'deleted'
          AND platform.can(p_actor, n.cell || '.' || n.type || ':read', p_delegation)
    )
$$;

CREATE FUNCTION graph.edge_visible(p_edge graph.edges, p_actor uuid, p_delegation uuid) RETURNS boolean
LANGUAGE sql STABLE AS $$
    SELECT p_edge.retracted_at IS NULL
       AND graph.node_visible(p_edge.from_urn, p_actor, p_delegation)
       AND graph.node_visible(p_edge.to_urn, p_actor, p_delegation)
       AND (p_edge.classification <> 'restricted' OR EXISTS (
            SELECT 1 FROM graph.consents c
            WHERE c.relation = p_edge.relation
              AND c.subject_urn IN (p_edge.from_urn, p_edge.to_urn)
              AND c.revoked_at IS NULL AND c.expires_at > now()))
$$;

CREATE FUNCTION graph.neighbors(p_urn text, p_actor uuid, p_delegation uuid DEFAULT NULL, p_limit int DEFAULT 100)
RETURNS TABLE (urn text, cell text, type text, label text, classification text, relation text, direction text)
LANGUAGE sql STABLE AS $$
    WITH start AS (SELECT graph.resolve(p_urn) AS urn)
    SELECT n.urn, n.cell, n.type, n.label, n.classification, e.relation,
           CASE WHEN e.from_urn = s.urn THEN 'out' ELSE 'in' END
    FROM start s
    JOIN graph.edges e ON s.urn IN (e.from_urn, e.to_urn)
    JOIN graph.nodes n ON n.urn = CASE WHEN e.from_urn = s.urn THEN e.to_urn ELSE e.from_urn END
    WHERE graph.node_visible(s.urn, p_actor, p_delegation)
      AND graph.edge_visible(e, p_actor, p_delegation)
    ORDER BY e.relation, n.urn
    LIMIT least(greatest(p_limit, 1), 500)
$$;

CREATE FUNCTION graph.search(p_query text, p_actor uuid, p_delegation uuid DEFAULT NULL, p_limit int DEFAULT 50)
RETURNS TABLE (urn text, cell text, type text, label text, classification text)
LANGUAGE sql STABLE AS $$
    SELECT n.urn, n.cell, n.type, n.label, n.classification
    FROM graph.nodes n
    WHERE n.label ILIKE '%' || replace(replace(p_query, '%', '\%'), '_', '\_') || '%'
      AND graph.node_visible(n.urn, p_actor, p_delegation)
    ORDER BY n.label, n.urn
    LIMIT least(greatest(p_limit, 1), 200)
$$;

GRANT USAGE ON SCHEMA graph TO graph_api;
GRANT SELECT ON graph.relation_types TO graph_api;
GRANT SELECT, INSERT, UPDATE ON graph.nodes, graph.aliases, graph.edges, graph.inbox TO graph_api;
GRANT SELECT, INSERT, UPDATE ON graph.consents TO graph_api;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA graph TO graph_api;
GRANT USAGE ON SCHEMA platform TO graph_api;
GRANT EXECUTE ON FUNCTION platform.current_workspace(), platform.uuid_v7(), platform.can TO graph_api;
