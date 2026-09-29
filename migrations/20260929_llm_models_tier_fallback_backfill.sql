-- llm_models.tier / fallback_group 백필. model_registry._MODEL_TIER_CATALOG 가 아는 3행만, NULL 인 칸만 채운다.
-- is_selectable / is_active / retired_at 은 건드리지 않는다. 여러 번 실행해도 결과가 같다.
UPDATE llm_models AS m
SET tier = COALESCE(m.tier, c.tier),
    fallback_group = COALESCE(m.fallback_group, c.fallback_group),
    updated_at = NOW()
FROM (VALUES
    ('anthropic', 'claude-opus-5-5', 'S', 'premium'),
    ('anthropic', 'claude-opus-5',   'S', 'premium'),
    ('anthropic', 'claude-sonnet-5', 'A', 'standard')
) AS c(provider, model_id, tier, fallback_group)
WHERE m.provider = c.provider
  AND m.model_id = c.model_id
  AND m.retired_at IS NULL
  AND m.discovery_source IS DISTINCT FROM 'accepted_alias'
  AND (m.tier IS NULL OR m.fallback_group IS NULL);
