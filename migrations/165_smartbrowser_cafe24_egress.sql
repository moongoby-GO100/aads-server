-- AADS-SMARTBROWSER-CAFE24-EGRESS-20260922
-- Additive and idempotent: intent is persisted; tunnel credentials remain in Vault.
ALTER TABLE browser_tasks
    ADD COLUMN IF NOT EXISTS egress_policy TEXT NOT NULL DEFAULT 'direct';

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'browser_tasks_egress_policy_check'
           AND conrelid = 'browser_tasks'::regclass
    ) THEN
        ALTER TABLE browser_tasks
            ADD CONSTRAINT browser_tasks_egress_policy_check
            CHECK (egress_policy IN ('direct', 'cafe24', 'auto'));
    END IF;
END $$;
