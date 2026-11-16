\set ON_ERROR_STOP on
-- 주의: 이 스크립트는 내부 트랜잭션(BEGIN ... COMMIT)을 포함하므로 psql --single-transaction 옵션과 함께 실행하지 마십시오.
-- 실행 예시: psql -v ON_ERROR_STOP=1 -X -f migrations/rollback/20261112_code_review_requests_revert_of_verified.down.sql
-- (또는: psql -v ON_ERROR_STOP=1 -X -f migrations/20261112_code_review_requests_revert_of_verified_rollback.sql)
--
-- 되돌림 스크립트 (수동 실행용)
-- 대응 마이그레이션: migrations/20261112_code_review_requests_revert_of_verified.sql
-- 정방향 파일의 서술 중 14~16행(러너 참조 관련) 및 39~42행(서버 COPY 백업)의 설명은 이 파일의 설명으로 대체한다.
--
-- ■ 되돌림 범위 및 삭제 객체
--   - 열: public.code_review_requests.revert_of_verified
--   - 열: public.pipeline_jobs.revert_of_verified
--   - 기존 객체(두 표의 기존 열들과 모든 작업·리뷰 행 데이터)는 전혀 삭제하거나 변경하지 않습니다.
--
-- ■ 선행 조건 및 러너 롤백 순서 (CEO 승인 후 운영 세션)
--   구 API 는 이 열을 읽지 않지만, scripts/pipeline-runner.sh(러너, API 롤백 대상 아님)는 계속 이 열을 씁니다.
--   API 만 되돌리고 열을 남기는 1차 롤백은 nullable 열이라 무해하며 down 이 필요 없습니다.
--   스키마에서 열을 영구 삭제(down)해야 할 경우 반드시 다음 순서를 준수합니다:
--     ① 러너 신규 작업 수락 중지
--     ② 실행 중 러너 작업 0건 확인:
--        SELECT count(*) FROM public.pipeline_jobs WHERE status = 'running';
--        (결과가 0 이어야 함)
--     ③ 러너를 이 열을 쓰지 않는 판으로 전환 (러너 배포/동기화)
--     ④ 백업 (아래 \copy 절차)
--     ⑤ down 실행
--   실행 및 승인 주체: CEO 승인 후 운영 세션.
--
-- ■ 데이터 백업 권장 절차 (실행 전 필수)
--   배포 후 운영 중에 러너가 기록한 검증 메타데이터({sha, patch_id, files})가 열과 함께 영구 삭제되므로
--   실행 전 psql 클라이언트 측 \copy 로 호스트 경로에 백업합니다:
--     1) \copy (SELECT id, revert_of_verified FROM public.code_review_requests WHERE revert_of_verified IS NOT NULL) TO '/root/aads/backups/crr_revert_of_verified_before_rollback.csv' WITH CSV HEADER
--     2) \copy (SELECT job_id, revert_of_verified FROM public.pipeline_jobs WHERE revert_of_verified IS NOT NULL) TO '/root/aads/backups/pj_revert_of_verified_before_rollback.csv' WITH CSV HEADER
--   백업 파일 존재 및 행 수 확인 후 down 을 실행하십시오 (예: wc -l /root/aads/backups/*_revert_of_verified_before_rollback.csv).
--
-- ■ 의존 순서: 두 열은 상호 독립적이므로 순서에 제약이 없습니다. IF EXISTS 를 사용합니다.
--
-- ■ 소유권·의존 가드 (2026-11-16 교차검토 보강)
--   - schema_migrations.note 에 {"precheck":"absent"} 가 없으면 삭제하지 않는다(적용 전부터 있던 동명 객체일 수 있음).
--   - pg_depend 는 deptype n·a·i 전체를 보고, 정방향이 만들지 않은 색인·트리거·제약·뷰가 하나라도 있으면 중단한다.
--

BEGIN;

SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';

-- 스크립트 위치 기준 공통 함수(rollback 디렉터리의 파일은 같은 정본의 심볼릭 링크).
-- 실행은 scripts/run_migration_down.sh 로만 한다(지문 함수 선주입)
-- 삭제 전 객체 존재·정의 확인 가드 (기존 객체 보호)
DO $$
DECLARE
    v_crr_col_exists boolean;
    v_pj_col_exists boolean;
    v_recorded jsonb;
    v_current jsonb;
    v_dep_crr integer;
    v_dep_pj integer;
BEGIN
    -- 0. 대상 열 존재 여부 확인 (public 스키마 한정)
    SELECT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'code_review_requests'
          AND column_name = 'revert_of_verified'
    ) INTO v_crr_col_exists;

    SELECT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'pipeline_jobs'
          AND column_name = 'revert_of_verified'
    ) INTO v_pj_col_exists;

    -- 대상이 없으면 지문을 입증할 수 없으므로 DROP 문장까지 진행하지 않는다.
    IF NOT v_crr_col_exists AND NOT v_pj_col_exists THEN
        RAISE EXCEPTION 'fingerprint 대상 객체 없음: 삭제 중단(이미 되돌려진 상태 포함)';
    END IF;

    -- 일부만 남은 불일치 상태: EXCEPTION 발생 및 트랜잭션 롤백
    IF v_crr_col_exists <> v_pj_col_exists THEN
        RAISE EXCEPTION '20261112: 되돌림 대상 객체 중 일부만 존재합니다 (code_review_requests=%, pipeline_jobs=%). 상태가 불일치하므로 되돌림을 중단합니다.', v_crr_col_exists, v_pj_col_exists;
    END IF;

    LOCK TABLE public.code_review_requests, public.pipeline_jobs IN ACCESS EXCLUSIVE MODE;

    -- 소유권 미입증 또는 지문 불일치는 어떤 DROP 보다 먼저 중단한다.
    SELECT note::jsonb INTO STRICT v_recorded FROM public.schema_migrations
    WHERE filename IN ('migrations/20261112_code_review_requests_revert_of_verified.sql', '20261112_code_review_requests_revert_of_verified.sql');
    v_current := pg_temp.release_object_fingerprint('20261112_code_review_requests_revert_of_verified.sql');
    IF v_recorded->>'precheck' IS DISTINCT FROM 'absent'
       OR v_recorded->>'fingerprint' IS NULL
       OR v_recorded->>'fingerprint' IS DISTINCT FROM v_current->>'fingerprint'
       OR v_recorded->'objects' IS DISTINCT FROM v_current->'objects' THEN
        RAISE EXCEPTION '소유권 미입증: fingerprint missing/mismatch — 삭제 중단';
    END IF;

    -- c) 삭제 대상 열에 정방향이 만들지 않은 의존 객체가 없을 것 (CASCADE 금지).
    --    pg_depend 는 deptype 'n'(일반)·'a'(자동: 색인·제약·트리거·기본값)·'i'(내부) 전부, refclassid=pg_class 로 본다.
    --    정방향은 열만 추가하므로 이 열에 걸린 의존은 하나도 없어야 한다.
    SELECT count(*) INTO v_dep_crr FROM pg_depend dep
    JOIN pg_attribute att ON att.attrelid = dep.refobjid AND att.attnum = dep.refobjsubid
    WHERE dep.refclassid = 'pg_class'::regclass
      AND dep.refobjid = to_regclass('public.code_review_requests')
      AND att.attname = 'revert_of_verified'
      AND dep.deptype IN ('n', 'a', 'i');
    IF v_dep_crr <> 0 THEN
        RAISE EXCEPTION '20261112: code_review_requests.revert_of_verified 열에 정방향이 만들지 않은 의존 객체(색인·트리거·제약·뷰 등)가 %개 있습니다. 중단합니다.', v_dep_crr;
    END IF;

    SELECT count(*) INTO v_dep_pj FROM pg_depend dep
    JOIN pg_attribute att ON att.attrelid = dep.refobjid AND att.attnum = dep.refobjsubid
    WHERE dep.refclassid = 'pg_class'::regclass
      AND dep.refobjid = to_regclass('public.pipeline_jobs')
      AND att.attname = 'revert_of_verified'
      AND dep.deptype IN ('n', 'a', 'i');
    IF v_dep_pj <> 0 THEN
        RAISE EXCEPTION '20261112: pipeline_jobs.revert_of_verified 열에 정방향이 만들지 않은 의존 객체(색인·트리거·제약·뷰 등)가 %개 있습니다. 중단합니다.', v_dep_pj;
    END IF;
END $$;

-- 1. code_review_requests 표에서 revert_of_verified 열 삭제
ALTER TABLE IF EXISTS public.code_review_requests DROP COLUMN IF EXISTS revert_of_verified;

-- 2. pipeline_jobs 표에서 revert_of_verified 열 삭제
ALTER TABLE IF EXISTS public.pipeline_jobs DROP COLUMN IF EXISTS revert_of_verified;

COMMIT;
