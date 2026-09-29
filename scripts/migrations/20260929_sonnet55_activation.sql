-- Claude Sonnet 5.5: keep Sonnet 5 executable for existing sessions, but replace
-- its selector preference and routing defaults. Safe to run more than once.
BEGIN;

DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM llm_models WHERE provider='anthropic' AND model_id='claude-sonnet-5') THEN
    RAISE EXCEPTION 'Sonnet 5 source row missing';
  END IF;
END $$;

INSERT INTO llm_models (
  provider, model_id, display_name, family, category,
  supports_tools, supports_thinking, supports_vision, supports_coding,
  input_cost, output_cost, is_active, activation_source, linked_key_name,
  metadata, execution_model_id, discovery_source, first_seen_at, last_seen_at,
  verification_status, last_verified_at, capabilities, pricing,
  is_selectable, is_executable, tier, fallback_group
)
SELECT provider, 'claude-sonnet-5-5', 'Claude Sonnet 5.5', family, category,
       supports_tools, TRUE, supports_vision, supports_coding,
       2, 10, TRUE, 'manual', linked_key_name,
       (metadata - 'sync_token') || jsonb_build_object(
         'execution_model_id','claude-sonnet-5-5',
         'accepted_aliases',jsonb_build_array('claude-sonnet-5-5'),
         'model_source','manual_runtime_verified',
         'official_source','https://www.anthropic.com/claude-sonnet-5-5'),
       'claude-sonnet-5-5', 'official_release', NOW(), NOW(),
       'discovered', NOW(), capabilities, pricing,
       TRUE, TRUE, tier, fallback_group
FROM llm_models WHERE provider='anthropic' AND model_id='claude-sonnet-5'
ON CONFLICT (provider, model_id) DO UPDATE SET
  display_name=EXCLUDED.display_name,
  input_cost=EXCLUDED.input_cost,
  output_cost=EXCLUDED.output_cost,
  is_active=TRUE,
  is_selectable=TRUE,
  is_executable=TRUE,
  activation_source='manual',
  execution_model_id=EXCLUDED.execution_model_id,
  metadata=EXCLUDED.metadata,
  updated_at=NOW();

-- Partial unique index permits one default per route. Clear the old default
-- before inserting its replacement, within the same transaction.
CREATE TEMP TABLE sonnet5_routing ON COMMIT DROP AS
SELECT * FROM model_routing_preferences
WHERE provider='anthropic' AND model_id='claude-sonnet-5' AND is_enabled=TRUE;

UPDATE model_routing_preferences
SET is_enabled=FALSE, is_default=FALSE, updated_at=NOW(), updated_by='sonnet55_release'
WHERE provider='anthropic' AND model_id='claude-sonnet-5' AND is_enabled=TRUE;

INSERT INTO model_routing_preferences (
  route_key, provider, model_id, display_order, is_enabled, is_default,
  notes, updated_at, updated_by, display_name, family, category
)
SELECT route_key, provider, 'claude-sonnet-5-5', display_order, is_enabled,
       is_default, 'Sonnet 5.5 replaces Sonnet 5; 2026-09-29', NOW(),
       'sonnet55_release', 'Claude Sonnet 5.5', family, category
FROM sonnet5_routing
ON CONFLICT (route_key, provider, model_id) DO UPDATE SET
  display_order=EXCLUDED.display_order,
  is_enabled=EXCLUDED.is_enabled,
  is_default=EXCLUDED.is_default,
  notes=EXCLUDED.notes,
  updated_at=NOW(),
  updated_by=EXCLUDED.updated_by,
  display_name=EXCLUDED.display_name;

UPDATE runner_model_config
SET models=replace(models::text, '"claude-sonnet-5"', '"claude-sonnet-5-5"')::jsonb,
    updated_at=NOW(), updated_by='sonnet55_release'
WHERE models::text LIKE '%"claude-sonnet-5"%';

INSERT INTO chat_model_preferences (
  preference_key, provider, model_id, display_order, is_hidden,
  is_favorite, is_pinned, updated_by, updated_at
) VALUES (
  'anthropic:claude-sonnet-5-5', 'anthropic', 'claude-sonnet-5-5', 40,
  FALSE, TRUE, TRUE, 'sonnet55_release', NOW()
)
ON CONFLICT (preference_key) DO UPDATE SET
  is_hidden=FALSE, is_favorite=TRUE, is_pinned=TRUE,
  display_order=40, updated_by='sonnet55_release', updated_at=NOW();

UPDATE chat_model_preferences
SET is_hidden=TRUE, is_pinned=FALSE, is_favorite=FALSE,
    updated_at=NOW(), updated_by='sonnet55_release'
WHERE provider='anthropic' AND model_id='claude-sonnet-5';

DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM llm_models WHERE provider='anthropic' AND model_id='claude-sonnet-5-5' AND is_active AND is_selectable AND is_executable) THEN
    RAISE EXCEPTION 'Sonnet 5.5 activation failed';
  END IF;
  IF EXISTS (SELECT 1 FROM runner_model_config WHERE models::text LIKE '%"claude-sonnet-5"%') THEN
    RAISE EXCEPTION 'Sonnet 5 remains in runner configuration';
  END IF;
END $$;

COMMIT;
