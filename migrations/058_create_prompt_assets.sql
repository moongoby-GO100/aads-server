-- 058: prompt_assets + compiled_prompt_provenance 테이블 생성
CREATE TABLE IF NOT EXISTS prompt_assets (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    slug TEXT UNIQUE NOT NULL,
    title TEXT NOT NULL,
    layer_id INTEGER NOT NULL CHECK (layer_id BETWEEN 1 AND 5),
    content TEXT NOT NULL DEFAULT '',
    model_variants JSONB DEFAULT '{}',
    workspace_scope TEXT[] DEFAULT '{*}',
    intent_scope TEXT[] DEFAULT '{*}',
    target_models TEXT[] DEFAULT '{*}',
    role_scope TEXT[] DEFAULT '{*}',
    priority INTEGER DEFAULT 10,
    enabled BOOLEAN DEFAULT TRUE,
    created_by TEXT DEFAULT 'system',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_prompt_assets_layer ON prompt_assets(layer_id);
CREATE INDEX IF NOT EXISTS idx_prompt_assets_enabled ON prompt_assets(enabled);

CREATE TABLE IF NOT EXISTS compiled_prompt_provenance (
    id BIGSERIAL PRIMARY KEY,
    session_id UUID,
    execution_id UUID,
    intent TEXT,
    model TEXT,
    system_prompt_hash TEXT,
    system_prompt_chars INTEGER,
    provenance JSONB,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
