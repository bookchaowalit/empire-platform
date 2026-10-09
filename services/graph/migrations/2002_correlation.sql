-- Keep the correlation ID of each applied event for cross-system tracing.
ALTER TABLE graph.inbox ADD COLUMN correlation_id text;

CREATE OR REPLACE FUNCTION graph.record_correlation(p_event jsonb) RETURNS void
LANGUAGE sql AS $$
    UPDATE graph.inbox SET correlation_id = p_event ->> 'correlation_id'
    WHERE consumer = 'graph' AND event_id = (p_event ->> 'id')::uuid AND correlation_id IS NULL
$$;
GRANT EXECUTE ON FUNCTION graph.record_correlation(jsonb) TO graph_api;
