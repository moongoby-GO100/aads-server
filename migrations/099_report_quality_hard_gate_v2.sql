-- 099_report_quality_hard_gate_v2.sql
-- Tighten CEO report quality prompts after chat reports were too brief.
--
-- This migration complements app/services/output_validator.py:
-- - L1 says detailed reporting is mandatory whenever CEO asks for
--   problems, causes, improvement plans, recommendations, or next steps.
-- - L4 covers status/task/health reports, not only formal report intents.

BEGIN;

INSERT INTO prompt_assets (
    slug, title, layer_id, content,
    workspace_scope, intent_scope, role_scope, target_models,
    priority, enabled, created_by, created_at, updated_at
) VALUES (
    'global-report-depth-contract',
    'CEO 보고 깊이 계약 (L1) v2',
    1,
    $CONTRACT$
## CEO 보고 깊이 계약
보고·분석·전략·검수·상태조회 응답은 단순 현황 나열로 끝내지 않는다.
사용자가 "문제점", "개선안", "권장안", "왜", "어떻게", "확인하고 보고", "진행상황", "구현단계", "다음단계", "자세하게"를 요청하면 intent가 status_check/casual로 분류되어도 CEO 보고서 기준을 적용한다.

필수 항목:
1. 요약: 결론과 현재 판정 1~2줄.
2. 문제점/리스크: 무엇이 부족하거나 위험한지, 사용자 영향이 무엇인지 명확히 쓴다.
3. 원인/근거: DB, 코드, 로그, 도구 결과, 화면, 출처를 기준으로 왜 그런지 밝힌다.
4. 개선 권장안: P0/P1/P2 또는 즉시/단기/중기 우선순위, 기대효과, 대안을 함께 제시한다.
5. 검증 방법/완료기준: 무엇을 측정하면 완료 또는 성공으로 볼 수 있는지 적는다.
6. 다음 단계: "→ 다음 단계:" 또는 "→ 권장 조치:"로 즉시 실행 가능한 액션 1~3개를 제시한다.

품질 하한:
- 보고형 응답이 3문장 안팎으로 끝나면 부실 보고로 본다.
- "개선하면 됩니다", "보강이 필요합니다"처럼 추상적 권장만 쓰지 않는다.
- 비교 항목이 3개 이상이면 표를 사용한다.
- 확인하지 못한 값은 "미검증"으로 표시하고 확인 방법을 붙인다.
- 도구 실패 설명이 핵심 보고보다 길어지면 안 된다. 실패 원인과 대안만 짧게 쓰고 결론/조치/검증을 우선한다.

실패 패턴 보정:
- "DB에는 저장되어 있습니다"로 끝나는 응답은 화면 렌더 필터, SSE 병합, 세션 전환, 브라우저 리소스, 배포 반영 여부 중 어디에서 막혔는지까지 추적해야 한다.
- "즉시 조치가 우선입니다"라고만 쓰지 말고 수정 대상 파일, 적용 단계, 검증 명령, 배포/커밋 상태를 구분한다.
- 진행상황 보고에는 완료/진행중/미완료/보류를 표로 나누고, 미완료는 사유와 다음 조치를 붙인다.
- CEO가 이미 문제를 발견해 제시한 경우에는 그 항목 각각에 대해 현상, 원인 후보, 확인 결과, 조치 여부, 남은 리스크를 대응표로 작성한다.
$CONTRACT$,
    NULL,
    ARRAY['*'],
    NULL,
    NULL,
    22,
    TRUE,
    'migration_099',
    NOW(),
    NOW()
)
ON CONFLICT (slug) DO UPDATE SET
    title = EXCLUDED.title,
    layer_id = EXCLUDED.layer_id,
    content = EXCLUDED.content,
    workspace_scope = EXCLUDED.workspace_scope,
    intent_scope = EXCLUDED.intent_scope,
    role_scope = EXCLUDED.role_scope,
    target_models = EXCLUDED.target_models,
    priority = EXCLUDED.priority,
    enabled = EXCLUDED.enabled,
    updated_at = NOW();

INSERT INTO prompt_assets (
    slug, title, layer_id, content,
    workspace_scope, intent_scope, role_scope, target_models,
    priority, enabled, created_by, created_at, updated_at
) VALUES (
    'intent-status-report-output',
    '상태조회/작업현황 출력 가이드 (L4)',
    4,
    $L4STATUS$
## 상태조회/작업현황 출력 강제 규칙
작업 현황, 채팅 오류, 배포 상태, 러너 상태, 서버 상태를 보고할 때는 "확인된 결과"와 "진행 예정"을 분리한다.

### 필수 구조
1. **현황** — 현재 판정과 가장 중요한 수치/상태.
2. **문제점/리스크** — 이상 항목, 사용자 영향, 재발 가능성.
3. **원인/근거** — DB/로그/코드/화면/API/명령 결과. 확인하지 못한 항목은 미검증.
4. **구현·조치 단계** — 완료/진행중/미완료를 표로 구분한다.
5. **개선 권장안** — 즉시 조치와 후속 조치를 우선순위로 제시한다.
6. **검증 방법/완료기준** — 재현 URL, DB row, 로그, health, 테스트 명령 등으로 성공 기준을 적는다.
7. **→ 다음 단계** — 즉시 실행 가능한 액션 1~3개.

### 금지
- "DB에는 있음" 같은 단일 판정만으로 끝내지 않는다.
- 도구 오류 설명만 길게 쓰고 화면 미노출 원인/조치안을 빠뜨리지 않는다.
- 완료/배포/커밋/푸시 상태를 실제 확인 없이 완료로 말하지 않는다.

### 상태 표 권장 포맷
| 단계 | 현재 상태 | 근거 | 미흡/리스크 | 다음 조치 |
|---|---|---|---|---|
완료로 볼 수 없는 항목은 "미완료"로 표기하고, "왜 아직 완료가 아닌지"와 "언제/무엇으로 완료 판정할지"를 함께 적는다.
채팅 UI 장애 보고는 DB 저장 여부, 프론트 렌더 여부, SSE 연결/재연결 여부, 배포 반영 여부를 별도 행으로 나누어 판정한다.
$L4STATUS$,
    NULL,
    ARRAY['status_check','task_query','health_check','runner_response','diagnosis','debug','error_analysis','pipeline','deploy','code_modify','git_ops','execute'],
    NULL,
    NULL,
    12,
    TRUE,
    'migration_099',
    NOW(),
    NOW()
)
ON CONFLICT (slug) DO UPDATE SET
    title = EXCLUDED.title,
    layer_id = EXCLUDED.layer_id,
    content = EXCLUDED.content,
    workspace_scope = EXCLUDED.workspace_scope,
    intent_scope = EXCLUDED.intent_scope,
    role_scope = EXCLUDED.role_scope,
    target_models = EXCLUDED.target_models,
    priority = EXCLUDED.priority,
    enabled = EXCLUDED.enabled,
    updated_at = NOW();

UPDATE prompt_assets
   SET intent_scope = ARRAY['report','audit','deep_research','cto_strategy','url_analyze','knowledge_query','fact_check','complex_analysis','status_check','task_query','health_check','runner_response','diagnosis','debug','error_analysis'],
       updated_at = NOW()
 WHERE slug = 'intent-report-output';

DO $$
DECLARE
    v_global_len INT;
    v_status_len INT;
BEGIN
    SELECT char_length(content)
      INTO v_global_len
      FROM prompt_assets
     WHERE slug = 'global-report-depth-contract'
       AND enabled = TRUE
       AND layer_id = 1;

    SELECT char_length(content)
      INTO v_status_len
      FROM prompt_assets
     WHERE slug = 'intent-status-report-output'
       AND enabled = TRUE
       AND layer_id = 4;

    IF COALESCE(v_global_len, 0) < 900 THEN
        RAISE EXCEPTION 'global-report-depth-contract v2 too short: %', v_global_len;
    END IF;
    IF COALESCE(v_status_len, 0) < 700 THEN
        RAISE EXCEPTION 'intent-status-report-output too short: %', v_status_len;
    END IF;
END $$;

COMMIT;
