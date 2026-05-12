-- 087_chat_report_depth_contract.sql
-- Enforce richer CEO-facing report answers.
--
-- Purpose:
-- - UI rendering improvements alone do not improve substance.
-- - Report/analysis answers must include problems, causes/evidence,
--   recommendations, and validation/completion criteria.
-- - Runtime retry enforcement lives in app/services/output_validator.py.

BEGIN;

INSERT INTO prompt_assets (
    slug, title, layer_id, content,
    workspace_scope, intent_scope, role_scope, target_models,
    priority, enabled, created_by, created_at, updated_at
) VALUES (
    'global-report-depth-contract',
    'CEO 보고 깊이 계약 (L1)',
    1,
    $CONTRACT$
## CEO 보고 깊이 계약
보고·분석·전략·검수 응답은 단순 현황 나열로 끝내지 않는다. 사용자가 "문제점", "개선안", "권장안", "왜", "어떻게", "확인하고 보고"를 요청하면 다음 항목을 반드시 포함한다.

필수 항목:
1. 문제점/리스크: 무엇이 부족하거나 위험한지 명확히 쓴다.
2. 원인/근거: DB, 코드, 로그, 도구 결과, 출처를 기준으로 왜 그런지 밝힌다.
3. 개선 권장안: 우선순위, 기대효과, 대안을 함께 제시한다.
4. 검증 방법/완료기준: 무엇을 측정하면 완료 또는 성공으로 볼 수 있는지 적는다.
5. 다음 단계: 즉시 실행 가능한 액션 1~3개를 제시한다.

품질 하한:
- 보고형 응답이 3문장 안팎으로 끝나면 부실 보고로 본다.
- "개선하면 됩니다", "보강이 필요합니다"처럼 추상적 권장만 쓰지 않는다.
- 비교 항목이 3개 이상이면 표를 사용한다.
- 확인하지 못한 값은 "미검증"으로 표시하고 확인 방법을 붙인다.
$CONTRACT$,
    NULL,
    ARRAY['*'],
    NULL,
    NULL,
    22,
    TRUE,
    'migration_087',
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
    'intent-report-output',
    '보고서/분석 출력 가이드 (L4)',
    4,
    $L4REPORT$
## 보고서/분석 출력 강제 규칙
모든 보고·분석·검수·전략 응답은 CEO가 바로 판단할 수 있게 작성한다.

### 필수 구조
1. **요약** — 핵심 결론 1~2줄.
2. **문제점/리스크** — 부족한 점, 위험, 사용자 영향, 차단 이슈.
3. **원인/근거** — DB/코드/로그/도구 결과/출처. 확인 못 한 항목은 미검증.
4. **개선 권장안** — P0/P1/P2 또는 즉시/단기/중기 우선순위와 대안.
5. **검증 방법/완료기준** — 어떤 테스트, 수치, 화면, DB row로 성공을 판정할지.
6. **→ 다음 단계** — 즉시 실행 가능한 액션 1~3개.

### 출력 규칙
- 800자 초과 응답은 첫 1~2줄 요약을 반드시 선행한다.
- 비교 가능한 항목 3개 이상은 마크다운 표를 사용한다.
- 수치에는 [DB 조회], [코드 확인], [로그], [공식문서, YYYY-MM-DD], [미측정] 같은 출처 태그를 붙인다.
- 도구 호출 경과("확인하겠습니다", "조회 중")는 본문에 섞지 말고 결과만 보고한다.
- 권장안은 추상 표현으로 끝내지 말고, 기대효과와 검증 기준을 함께 쓴다.
$L4REPORT$,
    NULL,
    ARRAY['report','audit','deep_research','cto_strategy','url_analyze','knowledge_query','fact_check','complex_analysis'],
    NULL,
    NULL,
    24,
    TRUE,
    'migration_087',
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
    'intent-analysis-output',
    '심층분석/CTO 출력 가이드 (L4)',
    4,
    $L4ANALYSIS$
## 심층분석/CTO 출력 강제 규칙
아키텍처, 기능 개선, 운영 리스크, 제품 전략 분석은 다음 판단 프레임을 따른다.

### 1. 결론
- 추천안 또는 판정을 1~2줄로 먼저 쓴다.

### 2. 문제점/영향
| 문제점 | 영향 범위 | 심각도 | 근거 |
|---|---|---|---|
- 기술, 사용자, 운영, 비용, 보안 영향을 분리한다.

### 3. 원인/근거
- 코드 경로, DB 쿼리, 로그, 화면, 외부 출처를 기준으로 확인한 사실을 쓴다.
- 확인하지 못한 부분은 미검증으로 표시한다.

### 4. 개선 옵션 비교
| 옵션 | 기대효과 | 비용/시간 | 리스크 | 권장 |
|---|---|---|---|---|
- 추천 옵션에는 이유와 반대 선택지를 같이 적는다.

### 5. 완료기준/검증
- 테스트 명령, 헬스체크, UI 확인, DB 검증, 성공 지표를 구체적으로 적는다.
- 마지막에 "→ 다음 단계:" 1~3개를 남긴다.
$L4ANALYSIS$,
    NULL,
    ARRAY['cto_strategy','cto_directive','cto_code_analysis','cto_verify','cto_impact','cto_tech_debt','deep_research','complex_analysis'],
    NULL,
    NULL,
    24,
    TRUE,
    'migration_087',
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

DO $$
DECLARE
    v_contract BOOL;
    v_report_len INT;
    v_analysis_len INT;
BEGIN
    SELECT EXISTS(
        SELECT 1 FROM prompt_assets
        WHERE slug = 'global-report-depth-contract'
          AND enabled = TRUE
          AND layer_id = 1
    ) INTO v_contract;

    SELECT char_length(content)
      INTO v_report_len
      FROM prompt_assets
     WHERE slug = 'intent-report-output';

    SELECT char_length(content)
      INTO v_analysis_len
      FROM prompt_assets
     WHERE slug = 'intent-analysis-output';

    IF NOT v_contract THEN
        RAISE EXCEPTION 'global-report-depth-contract prompt asset was not applied';
    END IF;
    IF v_report_len < 500 THEN
        RAISE EXCEPTION 'intent-report-output depth contract too short: %', v_report_len;
    END IF;
    IF v_analysis_len < 450 THEN
        RAISE EXCEPTION 'intent-analysis-output depth contract too short: %', v_analysis_len;
    END IF;
END $$;

COMMIT;
