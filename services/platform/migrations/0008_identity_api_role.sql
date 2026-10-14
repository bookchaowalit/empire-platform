-- platform-identity (migration step 3) reads the identity tables and calls the
-- shared decision point as its own runtime role. Like every runtime role it
-- owns nothing, holds no BYPASSRLS, and sees one workspace at a time through
-- the forced RLS policies. It may only:
--   - call platform.authorize (the same rules the platform endpoint uses), and
--   - read who is an active member, to exchange a provider id_token.
-- No table write grant: it cannot change identities, memberships, roles, or
-- the audit trail directly. platform.authorize is SECURITY DEFINER and appends
-- the decision it makes to platform.authorization_decisions, exactly as for
-- tasks_api; in shadow mode the service rolls that transaction back.
--
-- Rollout: the role must exist before this migration runs. Locally
-- db/bootstrap/roles.sql creates it; hosted, `empire-neon provision` (or
-- ensure_runtime_roles) creates it first and then applies the migrations.

GRANT USAGE ON SCHEMA platform TO identity_api;
GRANT EXECUTE ON FUNCTION platform.current_workspace(), platform.authorize TO identity_api;
GRANT SELECT (id, kind, status, external_subject) ON platform.identities TO identity_api;
GRANT SELECT (workspace_id, identity_id, status) ON platform.memberships TO identity_api;
