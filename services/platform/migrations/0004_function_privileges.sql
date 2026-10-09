-- Functions are executable by PUBLIC by default. Restrict every platform
-- function to the roles that are meant to call it; domain service roles get
-- their entry points in their own migrations.
REVOKE EXECUTE ON ALL FUNCTIONS IN SCHEMA platform FROM PUBLIC;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA platform TO platform_api;
