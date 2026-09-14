ALTER TABLE discord_archive.search_audit
    ADD COLUMN IF NOT EXISTS principal_user_hmac text;

ALTER TABLE discord_archive.search_audit
    DROP COLUMN IF EXISTS principal_user_id;

ALTER TABLE discord_archive.search_audit
    DROP CONSTRAINT IF EXISTS search_audit_principal_hmac_format;
ALTER TABLE discord_archive.search_audit
    ADD CONSTRAINT search_audit_principal_hmac_format
    CHECK (principal_user_hmac IS NULL OR principal_user_hmac ~ '^[0-9a-f]{64}$');

CREATE OR REPLACE FUNCTION discord_archive.reject_search_audit_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'search_audit is append-only';
END;
$$;

DROP TRIGGER IF EXISTS search_audit_append_only ON discord_archive.search_audit;
CREATE TRIGGER search_audit_append_only
    BEFORE UPDATE OR DELETE ON discord_archive.search_audit
    FOR EACH ROW EXECUTE FUNCTION discord_archive.reject_search_audit_mutation();

REVOKE UPDATE, DELETE, TRUNCATE ON discord_archive.search_audit FROM PUBLIC;
