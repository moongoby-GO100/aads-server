-- M14 security recovery: persist the change-set environment without altering
-- the already released M14 migration. Safe to apply repeatedly.
BEGIN;

ALTER TABLE work_item_change_sets
    ADD COLUMN IF NOT EXISTS environment TEXT NOT NULL DEFAULT 'dev';

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'work_item_change_sets'::regclass
          AND conname = 'ck_work_item_change_set_environment'
    ) THEN
        ALTER TABLE work_item_change_sets
            ADD CONSTRAINT ck_work_item_change_set_environment
            CHECK (environment IN ('dev', 'staging', 'production'));
    END IF;
END $$;

COMMIT;
