-- 백필 직전 실측 상태로 되돌린다: opus-5-5 는 tier/fallback_group 모두 NULL, opus-5 와 sonnet-5 는 fallback_group 만 NULL 이었다.
-- 값이 백필값과 같은 경우에만 지우므로 그 사이 사람이 바꾼 값은 남는다.
UPDATE llm_models
SET tier = NULL, updated_at = NOW()
WHERE provider = 'anthropic' AND model_id = 'claude-opus-5-5' AND tier = 'S';

UPDATE llm_models
SET fallback_group = NULL, updated_at = NOW()
WHERE (provider, model_id, fallback_group) IN (
    ('anthropic', 'claude-opus-5-5', 'premium'),
    ('anthropic', 'claude-opus-5',   'premium'),
    ('anthropic', 'claude-sonnet-5', 'standard')
);
