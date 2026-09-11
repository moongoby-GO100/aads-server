-- AADS-LLM-FALLBACK-OPS Phase 2: 폴백 체인 DB 시드
-- model_routing_preferences에 agent_*/intent_* route_key 추가
-- ON CONFLICT DO NOTHING: 이미 존재하면 건너뜀

BEGIN;

-- ═══ 에이전트 역할별 폴백 ═══

-- supervisor: opus → sonnet → haiku
INSERT INTO model_routing_preferences (route_key, provider, model_id, display_order, is_enabled, is_default, notes, updated_at, updated_by)
VALUES
  ('agent_supervisor', 'anthropic', 'claude-opus-4-6', 10, true, true, 'Supervisor primary', NOW(), 'seed_fallback'),
  ('agent_supervisor', 'anthropic', 'claude-sonnet-4-6', 20, true, false, 'Supervisor fallback', NOW(), 'seed_fallback'),
  ('agent_supervisor', 'anthropic', 'claude-haiku-4-5', 30, true, false, 'Supervisor error fallback', NOW(), 'seed_fallback')
ON CONFLICT DO NOTHING;

-- architect: opus → sonnet → haiku
INSERT INTO model_routing_preferences (route_key, provider, model_id, display_order, is_enabled, is_default, notes, updated_at, updated_by)
VALUES
  ('agent_architect', 'anthropic', 'claude-opus-4-6', 10, true, true, 'Architect primary', NOW(), 'seed_fallback'),
  ('agent_architect', 'anthropic', 'claude-sonnet-4-6', 20, true, false, 'Architect fallback', NOW(), 'seed_fallback'),
  ('agent_architect', 'anthropic', 'claude-haiku-4-5', 30, true, false, 'Architect error fallback', NOW(), 'seed_fallback')
ON CONFLICT DO NOTHING;

-- pm: sonnet → codex:gpt-5.6-sol → haiku
INSERT INTO model_routing_preferences (route_key, provider, model_id, display_order, is_enabled, is_default, notes, updated_at, updated_by)
VALUES
  ('agent_pm', 'anthropic', 'claude-sonnet-4-6', 10, true, true, 'PM primary', NOW(), 'seed_fallback'),
  ('agent_pm', 'codex', 'gpt-5.6-sol', 20, true, false, 'PM fallback (Codex CLI)', NOW(), 'seed_fallback'),
  ('agent_pm', 'anthropic', 'claude-haiku-4-5', 30, true, false, 'PM error fallback', NOW(), 'seed_fallback')
ON CONFLICT DO NOTHING;

-- developer: sonnet → codex:gpt-5.6-sol → haiku
INSERT INTO model_routing_preferences (route_key, provider, model_id, display_order, is_enabled, is_default, notes, updated_at, updated_by)
VALUES
  ('agent_developer', 'anthropic', 'claude-sonnet-4-6', 10, true, true, 'Developer primary', NOW(), 'seed_fallback'),
  ('agent_developer', 'codex', 'gpt-5.6-sol', 20, true, false, 'Developer fallback (Codex CLI)', NOW(), 'seed_fallback'),
  ('agent_developer', 'anthropic', 'claude-haiku-4-5', 30, true, false, 'Developer error fallback', NOW(), 'seed_fallback')
ON CONFLICT DO NOTHING;

-- qa: sonnet → haiku
INSERT INTO model_routing_preferences (route_key, provider, model_id, display_order, is_enabled, is_default, notes, updated_at, updated_by)
VALUES
  ('agent_qa', 'anthropic', 'claude-sonnet-4-6', 10, true, true, 'QA primary', NOW(), 'seed_fallback'),
  ('agent_qa', 'anthropic', 'claude-haiku-4-5', 20, true, false, 'QA fallback', NOW(), 'seed_fallback')
ON CONFLICT DO NOTHING;

-- judge: sonnet → codex:gpt-5.6-sol → haiku
INSERT INTO model_routing_preferences (route_key, provider, model_id, display_order, is_enabled, is_default, notes, updated_at, updated_by)
VALUES
  ('agent_judge', 'anthropic', 'claude-sonnet-4-6', 10, true, true, 'Judge primary', NOW(), 'seed_fallback'),
  ('agent_judge', 'codex', 'gpt-5.6-sol', 20, true, false, 'Judge fallback (Codex CLI)', NOW(), 'seed_fallback'),
  ('agent_judge', 'anthropic', 'claude-haiku-4-5', 30, true, false, 'Judge error fallback', NOW(), 'seed_fallback')
ON CONFLICT DO NOTHING;

-- devops: codex:gpt-5-mini → haiku → sonnet
INSERT INTO model_routing_preferences (route_key, provider, model_id, display_order, is_enabled, is_default, notes, updated_at, updated_by)
VALUES
  ('agent_devops', 'codex', 'gpt-5-mini', 10, true, true, 'DevOps primary (Codex CLI)', NOW(), 'seed_fallback'),
  ('agent_devops', 'anthropic', 'claude-haiku-4-5', 20, true, false, 'DevOps fallback', NOW(), 'seed_fallback'),
  ('agent_devops', 'anthropic', 'claude-sonnet-4-6', 30, true, false, 'DevOps error fallback', NOW(), 'seed_fallback')
ON CONFLICT DO NOTHING;

-- researcher: haiku → sonnet
INSERT INTO model_routing_preferences (route_key, provider, model_id, display_order, is_enabled, is_default, notes, updated_at, updated_by)
VALUES
  ('agent_researcher', 'anthropic', 'claude-haiku-4-5', 10, true, true, 'Researcher primary', NOW(), 'seed_fallback'),
  ('agent_researcher', 'anthropic', 'claude-sonnet-4-6', 20, true, false, 'Researcher fallback', NOW(), 'seed_fallback')
ON CONFLICT DO NOTHING;

-- strategist_collect: haiku → sonnet
INSERT INTO model_routing_preferences (route_key, provider, model_id, display_order, is_enabled, is_default, notes, updated_at, updated_by)
VALUES
  ('agent_strategist_collect', 'anthropic', 'claude-haiku-4-5', 10, true, true, 'Strategist collect primary', NOW(), 'seed_fallback'),
  ('agent_strategist_collect', 'anthropic', 'claude-sonnet-4-6', 20, true, false, 'Strategist collect fallback', NOW(), 'seed_fallback')
ON CONFLICT DO NOTHING;

-- strategist_analyze: opus → sonnet → haiku
INSERT INTO model_routing_preferences (route_key, provider, model_id, display_order, is_enabled, is_default, notes, updated_at, updated_by)
VALUES
  ('agent_strategist_analyze', 'anthropic', 'claude-opus-4-6', 10, true, true, 'Strategist analyze primary', NOW(), 'seed_fallback'),
  ('agent_strategist_analyze', 'anthropic', 'claude-sonnet-4-6', 20, true, false, 'Strategist analyze fallback', NOW(), 'seed_fallback'),
  ('agent_strategist_analyze', 'anthropic', 'claude-haiku-4-5', 30, true, false, 'Strategist analyze error fallback', NOW(), 'seed_fallback')
ON CONFLICT DO NOTHING;

-- planner: sonnet → haiku
INSERT INTO model_routing_preferences (route_key, provider, model_id, display_order, is_enabled, is_default, notes, updated_at, updated_by)
VALUES
  ('agent_planner', 'anthropic', 'claude-sonnet-4-6', 10, true, true, 'Planner primary', NOW(), 'seed_fallback'),
  ('agent_planner', 'anthropic', 'claude-haiku-4-5', 20, true, false, 'Planner fallback', NOW(), 'seed_fallback')
ON CONFLICT DO NOTHING;

-- ═══ 인텐트별 라우팅 ═══

INSERT INTO model_routing_preferences (route_key, provider, model_id, display_order, is_enabled, is_default, notes, updated_at, updated_by)
VALUES
  ('intent_casual', 'anthropic', 'claude-haiku', 10, true, true, 'Casual intent default', NOW(), 'seed_fallback'),
  ('intent_search', 'anthropic', 'claude-haiku', 10, true, true, 'Search intent default', NOW(), 'seed_fallback'),
  ('intent_deep_research', 'anthropic', 'claude-sonnet', 10, true, true, 'Deep research intent', NOW(), 'seed_fallback'),
  ('intent_url_analyze', 'anthropic', 'claude-haiku', 10, true, true, 'URL analyze intent', NOW(), 'seed_fallback'),
  ('intent_video_analyze', 'anthropic', 'claude-sonnet', 10, true, true, 'Video analyze intent', NOW(), 'seed_fallback'),
  ('intent_image_analyze', 'anthropic', 'claude-sonnet', 10, true, true, 'Image analyze intent', NOW(), 'seed_fallback'),
  ('intent_planning', 'anthropic', 'claude-sonnet', 10, true, true, 'Planning intent', NOW(), 'seed_fallback'),
  ('intent_decision', 'anthropic', 'claude-opus', 10, true, true, 'Decision intent', NOW(), 'seed_fallback'),
  ('intent_code_exec', 'codex', 'gpt-5.6-sol', 10, true, true, 'Code exec intent (Codex CLI)', NOW(), 'seed_fallback'),
  ('intent_directive_gen', 'anthropic', 'claude-sonnet', 10, true, true, 'Directive gen intent', NOW(), 'seed_fallback'),
  ('intent_memory_recall', 'anthropic', 'claude-haiku', 10, true, true, 'Memory recall intent', NOW(), 'seed_fallback'),
  ('intent_dashboard', 'anthropic', 'claude-haiku', 10, true, true, 'Dashboard intent', NOW(), 'seed_fallback'),
  ('intent_diagnosis', 'anthropic', 'claude-sonnet', 10, true, true, 'Diagnosis intent', NOW(), 'seed_fallback'),
  ('intent_research', 'anthropic', 'claude-haiku', 10, true, true, 'Research intent', NOW(), 'seed_fallback'),
  ('intent_execute', 'anthropic', 'claude-sonnet', 10, true, true, 'Execute intent', NOW(), 'seed_fallback'),
  ('intent_browser', 'anthropic', 'claude-sonnet', 10, true, true, 'Browser intent', NOW(), 'seed_fallback'),
  ('intent_strategy', 'anthropic', 'claude-opus', 10, true, true, 'Strategy intent', NOW(), 'seed_fallback'),
  ('intent_qa', 'anthropic', 'claude-sonnet', 10, true, true, 'QA intent', NOW(), 'seed_fallback'),
  ('intent_design', 'anthropic', 'claude-sonnet', 10, true, true, 'Design intent', NOW(), 'seed_fallback'),
  ('intent_design_fix', 'anthropic', 'claude-sonnet', 10, true, true, 'Design fix intent', NOW(), 'seed_fallback'),
  ('intent_architect', 'anthropic', 'claude-opus', 10, true, true, 'Architect intent', NOW(), 'seed_fallback'),
  ('intent_execution_verify', 'anthropic', 'claude-sonnet', 10, true, true, 'Execution verify intent', NOW(), 'seed_fallback'),
  ('intent_health_check', 'anthropic', 'claude-haiku', 10, true, true, 'Health check intent', NOW(), 'seed_fallback')
ON CONFLICT DO NOTHING;

COMMIT;
