\set ON_ERROR_STOP on

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_roles WHERE rolname = 'discord_history_audit_owner'
    ) THEN
        CREATE ROLE discord_history_audit_owner NOLOGIN;
    END IF;
END
$$;

ALTER TABLE discord_archive.search_audit OWNER TO discord_history_audit_owner;
ALTER FUNCTION discord_archive.reject_search_audit_mutation()
    OWNER TO discord_history_audit_owner;

REVOKE ALL ON discord_archive.search_audit FROM discord_history_app;
GRANT SELECT, INSERT ON discord_archive.search_audit TO discord_history_app;
GRANT USAGE, SELECT ON SEQUENCE discord_archive.search_audit_audit_id_seq
    TO discord_history_app;
