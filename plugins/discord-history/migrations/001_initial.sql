CREATE SCHEMA IF NOT EXISTS discord_archive;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE IF NOT EXISTS discord_archive.schema_migrations (
 version integer PRIMARY KEY, name text NOT NULL, applied_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS discord_archive.guilds (
 guild_id text PRIMARY KEY, name text NOT NULL, icon_url text,
 first_observed_at timestamptz NOT NULL DEFAULT now(), last_observed_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS discord_archive.channels (
 channel_id text PRIMARY KEY, guild_id text NOT NULL REFERENCES discord_archive.guilds(guild_id),
 parent_channel_id text, channel_type smallint NOT NULL, name text NOT NULL, topic text,
 is_thread boolean NOT NULL DEFAULT false, archived boolean, locked boolean,
 first_observed_at timestamptz NOT NULL DEFAULT now(), last_observed_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS discord_archive.users (
 user_id text PRIMARY KEY, username text, global_name text, is_bot boolean NOT NULL DEFAULT false,
 first_observed_at timestamptz NOT NULL DEFAULT now(), last_observed_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS discord_archive.ingest_runs (
 run_id uuid PRIMARY KEY, channel_id text, mode text NOT NULL CHECK(mode IN ('inventory','backfill','incremental','reconcile')),
 dce_version text NOT NULL, started_at timestamptz NOT NULL, finished_at timestamptz,
 status text NOT NULL CHECK(status IN ('running','ok','partial','error')), source_after timestamptz, source_before timestamptz,
 exported_count bigint NOT NULL DEFAULT 0, inserted_count bigint NOT NULL DEFAULT 0,
 updated_count bigint NOT NULL DEFAULT 0, tombstoned_count bigint NOT NULL DEFAULT 0,
 error_code text, error_detail text
);
CREATE TABLE IF NOT EXISTS discord_archive.ingest_run_scope (
 run_id uuid NOT NULL REFERENCES discord_archive.ingest_runs(run_id) ON DELETE CASCADE, channel_id text NOT NULL,
 channel_kind text NOT NULL CHECK(channel_kind IN ('channel','active_thread','archived_thread')),
 inventory_observed_at timestamptz NOT NULL,
 inventory_state text NOT NULL CHECK(inventory_state IN ('expected','complete','inaccessible','skipped','error')),
 export_state text NOT NULL CHECK(export_state IN ('pending','ok','empty','error')),
 export_after timestamptz, export_before timestamptz, exported_count bigint CHECK(exported_count IS NULL OR exported_count >= 0),
 PRIMARY KEY(run_id,channel_id)
);
CREATE TABLE IF NOT EXISTS discord_archive.inventory_endpoint_manifests (
 run_id uuid NOT NULL REFERENCES discord_archive.ingest_runs(run_id) ON DELETE CASCADE, parent_channel_id text NOT NULL,
 endpoint text NOT NULL CHECK(endpoint IN ('active','public','private','joined_private')),
 state text NOT NULL CHECK(state IN ('running','complete','inaccessible','error')),
 page_count integer NOT NULL DEFAULT 0 CHECK(page_count BETWEEN 0 AND 100), final_cursor text,
 endpoint_thread_ids text[] NOT NULL DEFAULT '{}', global_union_ids_after_endpoint text[] NOT NULL DEFAULT '{}',
 termination_reason text NOT NULL, PRIMARY KEY(run_id,parent_channel_id,endpoint)
);
CREATE TABLE IF NOT EXISTS discord_archive.inventory_pages (
 run_id uuid NOT NULL, parent_channel_id text NOT NULL, endpoint text NOT NULL,
 page_no integer NOT NULL CHECK(page_no BETWEEN 1 AND 100), request_cursor text, response_cursor text,
 has_more boolean NOT NULL, page_fingerprint text NOT NULL, raw_thread_ids text[] NOT NULL,
 PRIMARY KEY(run_id,parent_channel_id,endpoint,page_no),
 FOREIGN KEY(run_id,parent_channel_id,endpoint) REFERENCES discord_archive.inventory_endpoint_manifests(run_id,parent_channel_id,endpoint) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS discord_archive.inventory_parent_unions (
 run_id uuid NOT NULL REFERENCES discord_archive.ingest_runs(run_id) ON DELETE CASCADE, parent_channel_id text NOT NULL,
 state text NOT NULL CHECK(state IN ('running','complete','inaccessible','error')),
 active_thread_ids text[] NOT NULL DEFAULT '{}', archived_thread_ids text[] NOT NULL DEFAULT '{}', all_thread_ids text[] NOT NULL DEFAULT '{}',
 termination_reason text NOT NULL, PRIMARY KEY(run_id,parent_channel_id)
);
CREATE TABLE IF NOT EXISTS discord_archive.messages (
 message_id text PRIMARY KEY, guild_id text NOT NULL REFERENCES discord_archive.guilds(guild_id),
 channel_id text NOT NULL REFERENCES discord_archive.channels(channel_id), author_id text NOT NULL REFERENCES discord_archive.users(user_id),
 created_at timestamptz NOT NULL, edited_at timestamptz, deleted_at timestamptz, content text NOT NULL DEFAULT '',
 content_tsv tsvector GENERATED ALWAYS AS (to_tsvector('simple',coalesce(content,''))) STORED,
 reply_to_message_id text, message_type smallint, flags bigint, is_pinned boolean NOT NULL DEFAULT false,
 has_attachments boolean NOT NULL DEFAULT false, author_name_snapshot text, channel_name_snapshot text,
 raw_json jsonb NOT NULL, content_hash text NOT NULL, source_priority smallint NOT NULL DEFAULT 10,
 source_observed_at timestamptz NOT NULL, first_ingest_run_id uuid NOT NULL REFERENCES discord_archive.ingest_runs(run_id),
 last_ingest_run_id uuid NOT NULL REFERENCES discord_archive.ingest_runs(run_id)
);
CREATE TABLE IF NOT EXISTS discord_archive.message_revisions (
 message_id text NOT NULL REFERENCES discord_archive.messages(message_id) ON DELETE CASCADE, revision_no integer NOT NULL CHECK(revision_no > 0),
 content text NOT NULL, content_hash text NOT NULL, observed_at timestamptz NOT NULL,
 ingest_run_id uuid NOT NULL REFERENCES discord_archive.ingest_runs(run_id), PRIMARY KEY(message_id,revision_no)
);
CREATE TABLE IF NOT EXISTS discord_archive.attachments (
 attachment_id text PRIMARY KEY, message_id text NOT NULL REFERENCES discord_archive.messages(message_id) ON DELETE CASCADE,
 filename text NOT NULL, media_type text, size_bytes bigint, url text, proxy_url text
);
CREATE TABLE IF NOT EXISTS discord_archive.embeds (
 embed_id text PRIMARY KEY, message_id text NOT NULL REFERENCES discord_archive.messages(message_id) ON DELETE CASCADE,
 ordinal integer NOT NULL, embed_type text, title text, description text, url text, raw_json jsonb NOT NULL,
 UNIQUE(message_id,ordinal)
);
CREATE TABLE IF NOT EXISTS discord_archive.message_mentions (
 message_id text NOT NULL REFERENCES discord_archive.messages(message_id) ON DELETE CASCADE,
 mentioned_id text NOT NULL, mention_type text NOT NULL CHECK(mention_type IN ('user','role','channel','everyone')),
 PRIMARY KEY(message_id,mentioned_id,mention_type)
);
CREATE TABLE IF NOT EXISTS discord_archive.message_references (
 message_id text PRIMARY KEY REFERENCES discord_archive.messages(message_id) ON DELETE CASCADE,
 referenced_message_id text, referenced_channel_id text, referenced_guild_id text
);
CREATE TABLE IF NOT EXISTS discord_archive.ingest_cursors (
 channel_id text PRIMARY KEY REFERENCES discord_archive.channels(channel_id), newest_message_id text, newest_created_at timestamptz,
 last_incremental_at timestamptz, last_reconciled_at timestamptz, coverage_start timestamptz, coverage_end timestamptz,
 coverage_state text NOT NULL DEFAULT 'unknown' CHECK(coverage_state IN ('unknown','partial','complete','inaccessible','error')),
 last_run_id uuid REFERENCES discord_archive.ingest_runs(run_id)
);
CREATE TABLE IF NOT EXISTS discord_archive.search_audit (
 audit_id bigserial PRIMARY KEY, requested_at timestamptz NOT NULL DEFAULT now(), principal_user_id text, platform text,
 action text NOT NULL, query_hash text, requested_scope jsonb NOT NULL, result_message_ids text[] NOT NULL DEFAULT '{}', outcome text NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_content_tsv_idx ON discord_archive.messages USING gin(content_tsv);
CREATE INDEX IF NOT EXISTS messages_channel_created_idx ON discord_archive.messages(channel_id,created_at DESC);
CREATE INDEX IF NOT EXISTS messages_author_created_idx ON discord_archive.messages(author_id,created_at DESC);
CREATE INDEX IF NOT EXISTS messages_guild_created_idx ON discord_archive.messages(guild_id,created_at DESC);
CREATE INDEX IF NOT EXISTS users_username_trgm_idx ON discord_archive.users USING gin(username gin_trgm_ops);
CREATE INDEX IF NOT EXISTS channels_name_trgm_idx ON discord_archive.channels USING gin(name gin_trgm_ops);
INSERT INTO discord_archive.schema_migrations(version,name) VALUES (1,'001_initial') ON CONFLICT(version) DO NOTHING;
