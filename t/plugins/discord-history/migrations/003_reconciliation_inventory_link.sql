ALTER TABLE discord_archive.ingest_runs
    ADD COLUMN IF NOT EXISTS inventory_run_id uuid
    REFERENCES discord_archive.ingest_runs(run_id);

CREATE INDEX IF NOT EXISTS ingest_runs_inventory_run_idx
    ON discord_archive.ingest_runs(inventory_run_id)
    WHERE inventory_run_id IS NOT NULL;
