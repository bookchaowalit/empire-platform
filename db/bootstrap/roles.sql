-- Cluster-level roles. Run once per cluster as a superuser (locally) or create
-- the same roles through the Neon console/API (hosted). No passwords live here;
-- hosted credentials come from the secret manager.
--
-- empire_migrator  owns every schema and table and runs migrations only.
-- platform_api     the platform service at runtime: no superuser, no
--                  BYPASSRLS, owns nothing, and cannot assume empire_migrator.

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'empire_migrator') THEN
        CREATE ROLE empire_migrator LOGIN NOSUPERUSER NOCREATEROLE NOBYPASSRLS;
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'platform_api') THEN
        CREATE ROLE platform_api LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS NOINHERIT;
    END IF;
END
$$;
