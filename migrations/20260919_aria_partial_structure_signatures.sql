-- G4 ARIA partial-structure revisit evidence. Browser recipes remain the page-template canon.
-- Only normalized roles/states/semantic relations and one-way name hashes are stored.
CREATE TABLE IF NOT EXISTS browser_page_structure_signatures (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    recipe_id TEXT NOT NULL,
    recipe_version TEXT NOT NULL,
    page_key TEXT NOT NULL,
    area_key TEXT NOT NULL,
    signature_version TEXT NOT NULL,
    signature_hash TEXT,
    normalized_structure JSONB NOT NULL DEFAULT '{}'::jsonb,
    similarity DOUBLE PRECISION NOT NULL DEFAULT 0,
    decision TEXT NOT NULL,
    reason TEXT NOT NULL,
    human_gateway_required BOOLEAN NOT NULL DEFAULT FALSE,
    observed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (similarity >= 0 AND similarity <= 1),
    CHECK (decision IN ('reuse', 'rediscover', 'human_gateway'))
);

CREATE INDEX IF NOT EXISTS idx_browser_page_structure_signatures_revisit
    ON browser_page_structure_signatures
       (tenant_id, recipe_id, recipe_version, page_key, area_key, observed_at DESC);

CREATE INDEX IF NOT EXISTS idx_browser_page_structure_signatures_human_gateway
    ON browser_page_structure_signatures (tenant_id, observed_at DESC)
    WHERE human_gateway_required;
