-- Preserve the existing incomplete/complete recovery contract and record details.
ALTER TABLE run_info ADD COLUMN IF NOT EXISTS outcome TEXT;
ALTER TABLE run_info ADD COLUMN IF NOT EXISTS errors JSONB;
