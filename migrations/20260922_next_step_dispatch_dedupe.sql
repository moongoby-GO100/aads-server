-- Durable, cross-slot idempotency for approved and auto next-step reactions.
-- Nullable column keeps existing reaction producers and old API slots compatible.
ALTER TABLE chat_deferred_reactions
    ADD COLUMN IF NOT EXISTS dedupe_key TEXT;

CREATE INDEX IF NOT EXISTS idx_chat_deferred_reactions_dedupe
    ON chat_deferred_reactions(dedupe_key, created_at DESC)
    WHERE dedupe_key IS NOT NULL;
