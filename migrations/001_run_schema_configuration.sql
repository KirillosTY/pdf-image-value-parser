-- Apply explicitly to an existing parser database before running the new code.
-- Fresh databases use tables.metadata.create_all(engine).
BEGIN;

CREATE TABLE IF NOT EXISTS schema_versions (
    schema_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    definition JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Historical runs remain NULL until their actual approved configuration and
-- schema are identified. New runs always insert both fields through start_run.
ALTER TABLE run_info ADD COLUMN IF NOT EXISTS schema_id TEXT REFERENCES schema_versions(schema_id);
ALTER TABLE run_info ADD COLUMN IF NOT EXISTS config_snapshot JSONB;

ALTER TABLE images ADD COLUMN IF NOT EXISTS failed BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE images ALTER COLUMN raw_output DROP NOT NULL;
ALTER TABLE extraction_details ALTER COLUMN formatter_model DROP NOT NULL;
ALTER TABLE extraction_details ALTER COLUMN schema DROP NOT NULL;
ALTER TABLE extraction_details ALTER COLUMN model_response DROP NOT NULL;
ALTER TABLE extraction_details ALTER COLUMN formatted_data DROP NOT NULL;
ALTER TABLE extraction_details ALTER COLUMN unmapped_observations DROP NOT NULL;

COMMIT;
