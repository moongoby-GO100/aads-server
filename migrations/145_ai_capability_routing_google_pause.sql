-- 145_ai_capability_routing_google_pause.sql
-- Extend AI capability routing and pause Google/Gemini commercial routes.

BEGIN;

ALTER TABLE model_routing_preferences
    DROP CONSTRAINT IF EXISTS model_routing_preferences_route_key_chk;

ALTER TABLE model_routing_preferences
    ADD CONSTRAINT model_routing_preferences_route_key_chk
    CHECK (route_key IN (
        'llm',
        'background_llm',
        'runner_llm',
        'search',
        'deep_research',
        'url_analyze',
        'image_analyze',
        'video_analyze',
        'image',
        'edit_image',
        'video',
        'embedding',
        'semantic_search',
        'visual_qa',
        'fact_check',
        'code_exec',
        'audio',
        'music'
    ));

INSERT INTO llm_models (
    provider, model_id, display_name, family, category,
    supports_tools, supports_thinking, supports_vision, supports_coding,
    is_active, activation_source, execution_model_id, discovery_source,
    verification_status, is_selectable, is_executable, metadata,
    capabilities, pricing, updated_at, last_seen_at
)
VALUES
    ('self', 'smart-search-synthesis', 'AADS Native Search/Crawl Synthesis', 'aads-native', 'research', TRUE, FALSE, FALSE, FALSE, TRUE, 'manual', 'smart-search-synthesis', 'manual_seed', 'verified', TRUE, TRUE, '{"routing_note":"Self-hosted SearXNG/Naver/Jina/Crawl4AI collection plus LLM synthesis."}'::jsonb, '{"search":true,"deep_research":true,"fact_check":true}'::jsonb, '{}'::jsonb, NOW(), NOW()),
    ('self', 'jina-crawl4ai-synthesis', 'AADS Native URL Analyzer', 'aads-native', 'research', TRUE, FALSE, FALSE, FALSE, TRUE, 'manual', 'jina-crawl4ai-synthesis', 'manual_seed', 'verified', TRUE, TRUE, '{"routing_note":"Jina Reader/Crawl4AI URL extraction plus LLM synthesis."}'::jsonb, '{"url_analyze":true}'::jsonb, '{}'::jsonb, NOW(), NOW()),
    ('self', 'pgvector-cosine', 'PostgreSQL pgvector Semantic Search', 'aads-native', 'embedding', FALSE, FALSE, FALSE, FALSE, TRUE, 'manual', 'pgvector-cosine', 'manual_seed', 'verified', TRUE, TRUE, '{"routing_note":"Vector search over stored embeddings."}'::jsonb, '{"semantic_search":true}'::jsonb, '{}'::jsonb, NOW(), NOW()),
    ('searxng', 'searxng-local', 'AADS SearXNG Local Search', 'searxng', 'search', TRUE, FALSE, FALSE, FALSE, TRUE, 'manual', 'searxng-local', 'manual_seed', 'verified', TRUE, TRUE, '{"routing_note":"Self-hosted SearXNG container."}'::jsonb, '{"search":true}'::jsonb, '{}'::jsonb, NOW(), NOW()),
    ('naver', 'naver-search', 'Naver Search API', 'naver', 'search', TRUE, FALSE, FALSE, FALSE, TRUE, 'manual', 'naver-search', 'manual_seed', 'verified', TRUE, TRUE, '{"routing_note":"Korean search route; requires configured Naver credentials."}'::jsonb, '{"search":true}'::jsonb, '{}'::jsonb, NOW(), NOW()),
    ('pc_ollama', 'qwen3-embedding:0.6b', 'PC Ollama qwen3 embedding 0.6b', 'qwen3', 'embedding', FALSE, FALSE, FALSE, FALSE, TRUE, 'manual', 'qwen3-embedding:0.6b', 'manual_seed', 'verified', TRUE, TRUE, '{"routing_note":"Local embedding through PC Agent Ollama bridge."}'::jsonb, '{"embedding":true,"local":true}'::jsonb, '{}'::jsonb, NOW(), NOW()),
    ('pc_ollama', 'bge-m3', 'PC Ollama BGE-M3 Embedding', 'bge', 'embedding', FALSE, FALSE, FALSE, FALSE, TRUE, 'manual', 'bge-m3', 'manual_seed', 'verified', TRUE, TRUE, '{"routing_note":"Local embedding fallback through PC Agent Ollama bridge."}'::jsonb, '{"embedding":true,"local":true}'::jsonb, '{}'::jsonb, NOW(), NOW()),
    ('openai', 'text-embedding-3-small', 'OpenAI Text Embedding 3 Small', 'openai', 'embedding', FALSE, FALSE, FALSE, FALSE, FALSE, 'manual', 'text-embedding-3-small', 'manual_seed', 'auth_required', TRUE, FALSE, '{"routing_note":"External fallback; requires active OpenAI key."}'::jsonb, '{"embedding":true}'::jsonb, '{}'::jsonb, NOW(), NOW()),
    ('anthropic', 'claude-vision', 'Claude Vision Route', 'claude', 'vision', TRUE, TRUE, TRUE, FALSE, TRUE, 'manual', 'claude-sonnet-4-6', 'manual_seed', 'verified', TRUE, TRUE, '{"routing_note":"Vision route through central Anthropic auth."}'::jsonb, '{"vision":true,"image_analyze":true,"visual_qa":true}'::jsonb, '{}'::jsonb, NOW(), NOW()),
    ('codex', 'gpt-5.6-sol', 'GPT-5.6 Sol Codex', 'codex', 'coding', TRUE, TRUE, FALSE, TRUE, TRUE, 'manual', 'gpt-5.6-sol', 'manual_seed', 'verified', TRUE, TRUE, '{"routing_note":"Codex route default."}'::jsonb, '{"coding":true,"thinking":true}'::jsonb, '{}'::jsonb, NOW(), NOW())
ON CONFLICT (provider, model_id)
DO UPDATE SET
    display_name = EXCLUDED.display_name,
    family = EXCLUDED.family,
    category = EXCLUDED.category,
    metadata = COALESCE(llm_models.metadata, '{}'::jsonb) || EXCLUDED.metadata,
    capabilities = COALESCE(llm_models.capabilities, '{}'::jsonb) || EXCLUDED.capabilities,
    execution_model_id = COALESCE(llm_models.execution_model_id, EXCLUDED.execution_model_id),
    last_seen_at = NOW(),
    updated_at = NOW();

INSERT INTO model_routing_preferences (
    route_key, provider, model_id, display_order, is_enabled, is_default, notes, updated_at, updated_by
)
VALUES
    ('background_llm','qwen','qwen-turbo',10,true,true,'Background synthesis primary without Google',NOW(),'migration_145'),
    ('background_llm','anthropic','claude-haiku-4-5-20251001',20,true,false,'Background fallback via central auth',NOW(),'migration_145'),
    ('search','searxng','searxng-local',10,true,true,'Self-hosted metasearch default',NOW(),'migration_145'),
    ('search','naver','naver-search',20,true,false,'Korean search route',NOW(),'migration_145'),
    ('deep_research','self','smart-search-synthesis',10,true,true,'AADS native deep research: search/crawl + LLM synthesis',NOW(),'migration_145'),
    ('deep_research','anthropic','claude-sonnet-4-6',20,true,false,'Research synthesis fallback',NOW(),'migration_145'),
    ('url_analyze','self','jina-crawl4ai-synthesis',10,true,true,'Jina/Crawl4AI extraction with LLM synthesis',NOW(),'migration_145'),
    ('image_analyze','anthropic','claude-vision',10,true,true,'Claude vision analysis route',NOW(),'migration_145'),
    ('image_analyze','qwen','qwen-vl-plus',20,true,false,'Qwen vision fallback',NOW(),'migration_145'),
    ('video_analyze','qwen','qwen-vl-plus',10,true,true,'Frame extraction + Qwen/Claude vision route',NOW(),'migration_145'),
    ('embedding','pc_ollama','qwen3-embedding:0.6b',10,true,true,'Local PC Agent Ollama embedding primary',NOW(),'migration_145'),
    ('embedding','pc_ollama','bge-m3',20,true,false,'Local PC Agent Ollama embedding fallback',NOW(),'migration_145'),
    ('embedding','openai','text-embedding-3-small',30,true,false,'External embedding fallback when key is active',NOW(),'migration_145'),
    ('semantic_search','self','pgvector-cosine',10,true,true,'pgvector semantic search over stored embeddings',NOW(),'migration_145'),
    ('visual_qa','anthropic','claude-vision',10,true,true,'Visual QA without Google',NOW(),'migration_145'),
    ('fact_check','self','smart-search-synthesis',10,true,true,'Search/crawl based fact check route',NOW(),'migration_145'),
    ('code_exec','codex','gpt-5.6-sol',10,true,true,'Codex CLI/code execution route',NOW(),'migration_145')
ON CONFLICT (route_key, provider, model_id)
DO UPDATE SET
    display_order = EXCLUDED.display_order,
    is_enabled = EXCLUDED.is_enabled,
    is_default = EXCLUDED.is_default,
    notes = EXCLUDED.notes,
    updated_at = NOW(),
    updated_by = EXCLUDED.updated_by;

UPDATE model_routing_preferences
SET is_enabled = false,
    is_default = false,
    notes = CASE
        WHEN COALESCE(notes, '') = '' THEN 'Google/Gemini commercial routes disabled by CEO policy 2026-09-02 KST'
        WHEN notes LIKE '%Google/Gemini commercial routes disabled by CEO policy 2026-09-02 KST%' THEN notes
        ELSE notes || ' | Google/Gemini commercial routes disabled by CEO policy 2026-09-02 KST'
    END,
    updated_at = NOW(),
    updated_by = 'migration_145_google_disabled'
WHERE provider IN ('google', 'gemini');

UPDATE model_routing_preferences
SET is_enabled = true,
    is_default = true,
    updated_at = NOW(),
    updated_by = 'migration_145_google_disabled'
WHERE route_key = 'image'
  AND provider = 'genspark_ui'
  AND model_id = 'genspark-image-ui';

UPDATE model_routing_preferences
SET is_enabled = true,
    is_default = true,
    updated_at = NOW(),
    updated_by = 'migration_145_google_disabled'
WHERE route_key = 'edit_image'
  AND provider = 'genspark_ui'
  AND model_id = 'genspark-image-ui';

COMMIT;
