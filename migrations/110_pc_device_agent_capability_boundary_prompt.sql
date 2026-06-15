-- AADS PC/Android Agent capability boundary prompt.
-- Keeps future status reports from confusing feature support with current connectivity.

INSERT INTO prompt_assets (
  slug,
  title,
  layer_id,
  content,
  model_variants,
  workspace_scope,
  intent_scope,
  target_models,
  priority,
  enabled,
  created_by,
  role_scope
)
VALUES (
  'project-aads-pc-device-agent-capability-boundary',
  'AADS PC/Android Agent 기능 판정 기준',
  2,
  $$## AADS PC/Android Agent 기능 판정 기준
- PC Agent는 기능상 Windows shell 명령, PowerShell/CMD 호출, 파일/프로세스/스크린샷/브라우저/CDP 제어를 지원한다. 단, 실행 가능 여부는 현재 /api/v1/pc-agent/status online_count와 route-execute 실측 결과로 판정한다.
- Browser Bridge/CDP 세션과 PC Agent 본체 WebSocket은 별도 계층이다. Browser Bridge가 연결되어도 Windows shell 명령이 가능하다고 단정하지 말고, shell/system_info route-execute로 검증한다.
- PC Agent가 offline이면 "Windows 접근 기능 자체가 불가"가 아니라 "현재 연결된 PC Agent가 없어 명령 라우팅이 불가"로 보고한다.
- Android Agent는 /api/v1/devices WebSocket, foreground service, boot receiver, reconnect/watchdog 기준으로 분리 판정한다. 현재 연결 여부는 device_manager/API/DB last_used를 실측한다.
- 기능 한계 보고 시 가능/현재불가/미구현/미검증을 분리하고, 커밋/푸시/배포/문서 상태를 마지막에 명시한다.$$,
  '{}'::jsonb,
  ARRAY['AADS'],
  ARRAY['status_check','diagnosis','execute','browser','cto_strategy','*'],
  ARRAY['*'],
  21,
  true,
  'codex-runtime-fix',
  ARRAY['*']
)
ON CONFLICT (slug) DO UPDATE SET
  title = EXCLUDED.title,
  content = EXCLUDED.content,
  model_variants = EXCLUDED.model_variants,
  workspace_scope = EXCLUDED.workspace_scope,
  intent_scope = EXCLUDED.intent_scope,
  target_models = EXCLUDED.target_models,
  priority = EXCLUDED.priority,
  enabled = EXCLUDED.enabled,
  role_scope = EXCLUDED.role_scope,
  updated_at = NOW();
