-- 117: Direct work dependency operating policy prompt asset.
-- Created: 2026-07-31
--
-- Scope:
-- - Add an L1 operating policy so chat-direct code/DB work is routed through
--   dependency preflight instead of bypassing Pipeline Runner dependency logic.
-- - Keep the asset global because the risk exists across all AADS projects.

BEGIN;

INSERT INTO prompt_assets (
    slug,
    title,
    layer_id,
    content,
    workspace_scope,
    intent_scope,
    target_models,
    role_scope,
    priority,
    enabled,
    created_by,
    created_at,
    updated_at
) VALUES (
    'global-direct-work-dependency-gate',
    'L1 Global - Direct Work Dependency Gate / 직접 작업 의존성 게이트',
    1,
    $POLICY$
## L1 Global / 직접 작업 의존성 게이트
이 에셋은 채팅 세션에서 직접 코드·DB·운영 조치를 수행할 때 Pipeline Runner 의존성 그래프를 우회해 충돌이 생기는 것을 방지한다.

1. 기본 라우팅: 다중 파일 수정, 60초 이상 걸릴 가능성이 높은 코드 작업, 같은 프로젝트의 독립 하위작업 2개 이상, DB 스키마/대량 데이터 변경, 배포/재시작/푸시는 Pipeline Runner 또는 명시 승인 흐름을 우선한다.
2. 직접 작업 허용 범위: 읽기 전용 확인, 단일 파일 XS 핫픽스, 문서 1개 보강, 긴급 운영 확인처럼 영향 범위가 작고 되돌릴 수 있는 작업만 채팅 직접 작업으로 수행한다.
3. 직접 수정 전 필수 프리플라이트: 대상 repo `git status`, 활성 `pipeline_jobs`, 동일 파일/영역 runner 충돌, `chat_workspace_change_ledger`의 다른 세션 dirty 변경, DB 변경 위험도를 확인한다.
4. 판정: 충돌 없음은 GREEN으로 직접 진행 가능, 같은 repo 다른 파일 dirty는 YELLOW로 파일을 선별해 진행 가능, 동일 파일/활성 runner 충돌은 RED로 Runner `depends_on` 또는 대기, 파괴적 DB/무단 배포/시크릿 경로는 BLOCK으로 중단한다.
5. 세션 간 직접 작업: 서로 다른 채팅 세션에서 같은 프로젝트를 직접 수정할 경우 자동 의존성이 완전히 보장된다고 말하지 않는다. 현재 직접 작업은 ledger와 git lock으로 최종 커밋 충돌을 줄이지만, Runner의 `depends_on`과 자동 파일충돌 연결은 Runner 제출 작업에만 확실히 적용된다.
6. DB 직접 변경: CEO가 명시한 비파괴 정책/설정 반영처럼 범위가 좁은 경우에만 트랜잭션 또는 idempotent upsert로 수행하고, 전후 SELECT 검증과 롤백 가능성을 보고한다. DROP/TRUNCATE/광범위 DELETE/시크릿 노출은 금지한다.
7. 보고 의무: 코드·DB·문서·운영 정책을 다룬 경우 변경 파일 또는 대상 테이블, 실행 검증, 커밋/푸시/배포/문서기록 상태, 미완료 사유를 분리해 보고한다.
8. 후속 자동화: 직접 작업이 RED이면 `pipeline_runner_submit`/batch의 `parallel_group`, `depends_on`, `depends_on_key`를 사용해 의존성을 명시하고, 관측한 파일 경로와 검증 기준을 instruction에 포함한다.
$POLICY$,
    ARRAY['*']::text[],
    ARRAY['code_modify','code_fix','execute','git_ops','deploy','database_query','project_db','pipeline_runner','cto_verify','status_check','report','audit','*']::text[],
    ARRAY['*']::text[],
    ARRAY['*']::text[],
    15,
    TRUE,
    'codex',
    NOW(),
    NOW()
)
ON CONFLICT (slug) DO UPDATE
SET title = EXCLUDED.title,
    layer_id = EXCLUDED.layer_id,
    content = EXCLUDED.content,
    workspace_scope = EXCLUDED.workspace_scope,
    intent_scope = EXCLUDED.intent_scope,
    target_models = EXCLUDED.target_models,
    role_scope = EXCLUDED.role_scope,
    priority = EXCLUDED.priority,
    enabled = TRUE,
    updated_at = NOW();

DO $$
DECLARE
    v_chars INTEGER;
BEGIN
    SELECT char_length(content)
      INTO v_chars
      FROM prompt_assets
     WHERE slug = 'global-direct-work-dependency-gate'
       AND enabled = TRUE;

    IF v_chars IS NULL OR v_chars < 900 THEN
        RAISE EXCEPTION 'global-direct-work-dependency-gate prompt asset was not applied correctly';
    END IF;
END $$;

COMMIT;

