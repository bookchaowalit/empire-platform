-- Authorization functions, integrity triggers, row-level security, and the
-- grants for platform_api. Application code still checks authorization on
-- every request; RLS is the second, independent line of defence.

-- Permissions an identity holds through its own roles in the current workspace.
-- Agents never hold direct permissions: they act only through a delegation.
CREATE FUNCTION platform.direct_permission(p_identity uuid, p_permission text) RETURNS boolean
LANGUAGE sql STABLE AS $$
    SELECT EXISTS (
        SELECT 1
        FROM platform.membership_roles mr
        JOIN platform.memberships m USING (workspace_id, identity_id)
        JOIN platform.identities i ON i.id = m.identity_id
        JOIN platform.workspaces w ON w.id = m.workspace_id
        JOIN platform.role_permissions rp ON rp.workspace_id = mr.workspace_id AND rp.role_id = mr.role_id
        WHERE mr.workspace_id = platform.current_workspace()
          AND mr.identity_id = p_identity
          AND m.status = 'active'
          AND i.status = 'active'
          AND i.kind <> 'agent'
          AND w.status = 'active'
          AND rp.permission = p_permission
    )
$$;

-- Effective permission. With a delegation, the grantee must hold the scope and
-- the grantor must still hold the permission right now; a delegation never
-- outlives or exceeds its grantor.
CREATE FUNCTION platform.has_permission(p_identity uuid, p_permission text, p_delegation uuid DEFAULT NULL)
RETURNS boolean
LANGUAGE sql STABLE AS $$
    SELECT CASE
        WHEN p_delegation IS NULL THEN platform.direct_permission(p_identity, p_permission)
        ELSE EXISTS (
            SELECT 1
            FROM platform.delegations d
            JOIN platform.memberships gm
              ON gm.workspace_id = d.workspace_id AND gm.identity_id = d.grantee_identity_id
            JOIN platform.identities gi ON gi.id = d.grantee_identity_id
            WHERE d.id = p_delegation
              AND d.workspace_id = platform.current_workspace()
              AND d.grantee_identity_id = p_identity
              AND d.revoked_at IS NULL
              AND d.expires_at > now()
              AND p_permission = ANY (d.scopes::text[])
              AND gm.status = 'active'
              AND gi.status = 'active'
              AND platform.direct_permission(d.grantor_identity_id, p_permission)
        )
    END
$$;

-- A delegation goes to a non-human member and may only carry scopes the
-- grantor holds when it is created or changed.
CREATE FUNCTION platform.check_delegation() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    missing text;
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM platform.identities i
        JOIN platform.memberships m ON m.identity_id = i.id AND m.workspace_id = NEW.workspace_id
        WHERE i.id = NEW.grantee_identity_id AND i.kind <> 'human' AND m.status = 'active'
    ) THEN
        RAISE EXCEPTION 'delegation grantee must be an active non-human member of the workspace'
            USING ERRCODE = 'check_violation';
    END IF;
    SELECT s INTO missing
    FROM unnest(NEW.scopes::text[]) AS s
    WHERE NOT platform.direct_permission(NEW.grantor_identity_id, s)
    LIMIT 1;
    IF missing IS NOT NULL THEN
        RAISE EXCEPTION 'delegation scope % exceeds the grantor''s permissions', missing
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER delegations_check
    BEFORE INSERT OR UPDATE OF grantor_identity_id, grantee_identity_id, scopes, workspace_id
    ON platform.delegations
    FOR EACH ROW EXECUTE FUNCTION platform.check_delegation();

-- Acting for someone else requires a live delegation from that identity.
CREATE FUNCTION platform.check_execution() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM platform.memberships
        WHERE workspace_id = NEW.workspace_id AND identity_id = NEW.initiated_by AND status = 'active'
    ) THEN
        RAISE EXCEPTION 'execution initiator is not an active member of the workspace'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF NEW.executed_by <> NEW.on_behalf_of THEN
        IF NEW.delegation_id IS NULL OR NOT EXISTS (
            SELECT 1 FROM platform.delegations d
            WHERE d.id = NEW.delegation_id
              AND d.workspace_id = NEW.workspace_id
              AND d.grantee_identity_id = NEW.executed_by
              AND d.grantor_identity_id = NEW.on_behalf_of
              AND d.revoked_at IS NULL
              AND d.expires_at > now()
        ) THEN
            RAISE EXCEPTION 'acting on behalf of another identity requires a live delegation from it'
                USING ERRCODE = 'insufficient_privilege';
        END IF;
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER executions_check
    BEFORE INSERT OR UPDATE OF initiated_by, executed_by, on_behalf_of, delegation_id, workspace_id
    ON platform.executions
    FOR EACH ROW EXECUTE FUNCTION platform.check_execution();

CREATE FUNCTION platform.reject_audit_change() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'audit_events is append-only' USING ERRCODE = 'insufficient_privilege';
END
$$;

CREATE TRIGGER audit_events_append_only
    BEFORE UPDATE OR DELETE ON platform.audit_events
    FOR EACH STATEMENT EXECUTE FUNCTION platform.reject_audit_change();

-- Row-level security. FORCE applies the policies to the table owner as well.
ALTER TABLE platform.identities ENABLE ROW LEVEL SECURITY;
ALTER TABLE platform.identities FORCE ROW LEVEL SECURITY;
CREATE POLICY identities_visible ON platform.identities FOR SELECT
    USING (EXISTS (
        SELECT 1 FROM platform.memberships m
        WHERE m.identity_id = identities.id AND m.workspace_id = platform.current_workspace()
    ));
CREATE POLICY identities_insert ON platform.identities FOR INSERT WITH CHECK (true);

ALTER TABLE platform.workspaces ENABLE ROW LEVEL SECURITY;
ALTER TABLE platform.workspaces FORCE ROW LEVEL SECURITY;
CREATE POLICY workspace_isolation ON platform.workspaces
    USING (id = platform.current_workspace())
    WITH CHECK (id = platform.current_workspace());

DO $$
DECLARE
    t text;
BEGIN
    FOREACH t IN ARRAY ARRAY[
        'memberships', 'roles', 'role_permissions', 'membership_roles', 'policies',
        'delegations', 'approvals', 'executions', 'audit_events'
    ] LOOP
        EXECUTE format('ALTER TABLE platform.%I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('ALTER TABLE platform.%I FORCE ROW LEVEL SECURITY', t);
        EXECUTE format(
            'CREATE POLICY workspace_isolation ON platform.%I '
            'USING (workspace_id = platform.current_workspace()) '
            'WITH CHECK (workspace_id = platform.current_workspace())', t);
    END LOOP;
END
$$;

-- Grants for the runtime role. It owns nothing and cannot alter or disable RLS.
GRANT USAGE ON SCHEMA platform TO platform_api;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA platform TO platform_api;
GRANT SELECT, INSERT ON platform.identities TO platform_api;
GRANT UPDATE (display_name, status) ON platform.identities TO platform_api;
GRANT SELECT, INSERT, UPDATE ON platform.workspaces, platform.memberships, platform.roles,
    platform.policies, platform.delegations, platform.approvals, platform.executions TO platform_api;
GRANT SELECT, INSERT, DELETE ON platform.role_permissions, platform.membership_roles TO platform_api;
GRANT SELECT, INSERT ON platform.audit_events TO platform_api;
