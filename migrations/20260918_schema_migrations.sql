CREATE TABLE IF NOT EXISTS schema_migrations (
  filename    text PRIMARY KEY,
  sha256      text NOT NULL,
  applied_at  timestamptz NOT NULL DEFAULT now(),
  applied_by  text,
  source      text NOT NULL DEFAULT 'runtime' CHECK (source IN ('runtime','backfill')),
  duration_ms integer,
  note        text
);

CREATE INDEX IF NOT EXISTS idx_schema_migrations_applied_at
  ON schema_migrations(applied_at DESC);
