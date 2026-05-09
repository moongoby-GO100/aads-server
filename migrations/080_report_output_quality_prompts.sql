-- 080: 보고서 출력 품질 개선 — L4 인텐트별 출력 가이드 3건 추가
-- Phase 1 of chat report quality improvement (2026-05-09)
BEGIN;

-- 1) L4: 보고서·분석 출력 가이드
INSERT INTO prompt_assets (slug, layer_id, content, enabled, priority, workspace_scope, role_scope, intent_scope, target_models)
VALUES (
  'intent-report-analysis-output',
  4,
  E'## 보고서·분석 출력 가이드\n\n### 필수 구조\n1. **요약** — 핵심 결론 1~2줄을 첫 문장에 배치한다.\n2. **현황 표** — 3개 이상 항목 비교 시 반드시 마크다운 표를 사용한다.\n3. **상세** — ##/### 소제목으로 구분한다. 수치에는 [출처]를 표기한다.\n4. **다음 단계** — \"→ 다음 단계:\" 형태로 1~3개 제시한다.\n\n### 출력 규칙\n- 800자 초과 시 요약 1~2줄을 반드시 선행한다.\n- 장문 나열 대신 표·목록·코드블록을 우선 사용한다.\n- 시계열/추이 데이터는 ```chart 코드펜스로 시각화한다.\n- 상태 표시: ✅성공 ❌실패 ⚠️주의만 사용한다. 장식 이모지 금지.\n- 금액은 천단위 쉼표, 퍼센트는 소수점 1자리, 시간은 KST.\n\n### 금지\n- \"확인하겠습니다\" 같은 행동 없는 약속으로 시작하지 않는다.\n- 동일 내용을 다른 표현으로 반복하지 않는다.\n- 도구 호출 경과를 본문에 섞지 않는다 (결과만 보고).\n- 근거 없는 수치를 사용하지 않는다.',
  true,
  15,
  '{}',
  '{}',
  '{report,audit,deep_research,cto_strategy,url_analyze,pipeline_runner}',
  '{}'
)
ON CONFLICT (slug) DO UPDATE SET
  content = EXCLUDED.content,
  priority = EXCLUDED.priority,
  intent_scope = EXCLUDED.intent_scope,
  enabled = true;

-- 2) L4: 코드 수정·배포 출력 가이드
INSERT INTO prompt_assets (slug, layer_id, content, enabled, priority, workspace_scope, role_scope, intent_scope, target_models)
VALUES (
  'intent-code-deploy-output',
  4,
  E'## 코드 수정·배포 출력 가이드\n\n### 필수 구조\n1. **수행 내역** — 무엇을 했는지 1~3줄.\n2. **변경 파일 표** — | 파일 | 변경 내용 | 영향 범위 | 형태.\n3. **검증 결과** — 첫 줄에 ✅ 성공 또는 ❌ 실패. 실패 시 원인+대안.\n4. **남은 작업** — 추가 필요 시 \"→ 남은 작업:\" 형태.\n\n### 규칙\n- 변경 파일이 3개 이상이면 반드시 표로 정리한다.\n- 테스트를 실행하지 않았으면 \"⚠️ 테스트 미실행\" 명시한다.\n- 배포했으면 health-check 결과를 포함한다.\n- 커밋 해시를 보고한다.\n- 배포 전후 비교가 필요하면 before/after 코드블록을 사용한다.',
  true,
  14,
  '{}',
  '{}',
  '{code_modify,deploy,pipeline,git_ops,execute}',
  '{}'
)
ON CONFLICT (slug) DO UPDATE SET
  content = EXCLUDED.content,
  priority = EXCLUDED.priority,
  intent_scope = EXCLUDED.intent_scope,
  enabled = true;

-- 3) L4: 검색·리서치 출력 가이드
INSERT INTO prompt_assets (slug, layer_id, content, enabled, priority, workspace_scope, role_scope, intent_scope, target_models)
VALUES (
  'intent-search-research-output',
  4,
  E'## 검색·리서치 출력 가이드\n\n### 필수 구조\n1. **답변** — 질문에 대한 직접 답 1~3줄.\n2. **근거 표** — | 출처 | 내용 | 날짜 | 신뢰도 | 형태.\n3. **교차 검증** — 2개 이상 소스 일치 ✅확인됨, 단일 소스 ⚠️미검증, 불일치 ❌.\n\n### 규칙\n- 학습 데이터만으로 답하지 않는다. 검색 도구를 먼저 호출한다.\n- 날짜 없는 정보는 \"시점 불명\" 표기한다.\n- 출처에 [기관명, 날짜, URL] 형식을 사용한다.\n- 한국어 검색은 search_naver를 1순위로 사용한다.',
  true,
  13,
  '{}',
  '{}',
  '{search,fact_check,knowledge_query,research}',
  '{}'
)
ON CONFLICT (slug) DO UPDATE SET
  content = EXCLUDED.content,
  priority = EXCLUDED.priority,
  intent_scope = EXCLUDED.intent_scope,
  enabled = true;

COMMIT;
