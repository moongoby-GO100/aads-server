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

BEGIN;

SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';

-- 삭제 전 객체 존재·정의 확인 가드 (기존 객체 보호)
DO $$
DECLARE
    v_crr_col_exists boolean;
    v_pj_col_exists boolean;
    v_crr_match integer;
    v_pj_match integer;
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

    -- 이미 되돌려진 상태(둘 다 없음): NOTICE 후 정상 종료 (멱등성 보장)
    IF NOT v_crr_col_exists AND NOT v_pj_col_exists THEN
        RAISE NOTICE '20261112: 되돌릴 대상 객체(code_review_requests.revert_of_verified 및 pipeline_jobs.revert_of_verified 열)가 존재하지 않습니다. 이미 되돌려진 상태입니다.';
        RETURN;
    END IF;

    -- 일부만 남은 불일치 상태: EXCEPTION 발생 및 트랜잭션 롤백
    IF v_crr_col_exists <> v_pj_col_exists THEN
        RAISE EXCEPTION '20261112: 되돌림 대상 객체 중 일부만 존재합니다 (code_review_requests=%, pipeline_jobs=%). 상태가 불일치하므로 되돌림을 중단합니다.', v_crr_col_exists, v_pj_col_exists;
    END IF;

    -- a) schema_migrations 에 해당 마이그레이션 파일 기록이 있는가
    IF NOT EXISTS (
        SELECT 1 FROM public.schema_migrations
        WHERE filename IN (
            'migrations/20261112_code_review_requests_revert_of_verified.sql',
            '20261112_code_review_requests_revert_of_verified.sql'
        )
    ) THEN
        RAISE EXCEPTION '20261112: schema_migrations 에 해당 마이그레이션 적용 기록이 없습니다. 이 마이그레이션이 적용된 적 없음 — 같은 이름의 기존 객체일 수 있어 중단합니다.';
    END IF;

    -- b) 대상 객체가 정방향 정의와 같은가 (jsonb, nullable, no default)
    SELECT count(*) INTO v_crr_match
    FROM information_schema.columns
    WHERE table_schema = 'public'
      AND table_name = 'code_review_requests'
      AND column_name = 'revert_of_verified'
      AND data_type = 'jsonb'
      AND is_nullable = 'YES'
      AND column_default IS NULL;

    IF v_crr_match <> 1 THEN
        RAISE EXCEPTION '20261112: code_review_requests.revert_of_verified 열의 정의가 정방향(jsonb, nullable, no default)과 일치하지 않습니다.';
    END IF;

    SELECT count(*) INTO v_pj_match
    FROM information_schema.columns
    WHERE table_schema = 'public'
      AND table_name = 'pipeline_jobs'
      AND column_name = 'revert_of_verified'
      AND data_type = 'jsonb'
      AND is_nullable = 'YES'
      AND column_default IS NULL;

    IF v_pj_match <> 1 THEN
        RAISE EXCEPTION '20261112: pipeline_jobs.revert_of_verified 열의 정의가 정방향(jsonb, nullable, no default)과 일치하지 않습니다.';
    END IF;

    -- c) 삭제 대상 열에 정방향이 만들지 않은 의존 객체(뷰·FK·색인 등, pg_depend 기준)가 없을 것 (CASCADE 금지)
    IF EXISTS (
        SELECT 1 FROM pg_depend dep
        JOIN pg_attribute att ON att.attrelid = dep.refobjid AND att.attnum = dep.refobjsubid
        WHERE dep.refobjid = to_regclass('public.code_review_requests')
          AND att.attname = 'revert_of_verified'
          AND dep.deptype = 'n'
    ) THEN
        RAISE EXCEPTION '20261112: code_review_requests.revert_of_verified 열에 정방향이 만들지 않은 의존 객체(뷰, 색인 등)가 존재합니다. 중단합니다.';
    END IF;

    IF EXISTS (
        SELECT 1 FROM pg_depend dep
        JOIN pg_attribute att ON att.attrelid = dep.refobjid AND att.attnum = dep.refobjsubid
        WHERE dep.refobjid = to_regclass('public.pipeline_jobs')
          AND att.attname = 'revert_of_verified'
          AND dep.deptype = 'n'
    ) THEN
        RAISE EXCEPTION '20261112: pipeline_jobs.revert_of_verified 열에 정방향이 만들지 않은 의존 객체(뷰, 색인 등)가 존재합니다. 중단합니다.';
    END IF;
END $$;

-- 1. code_review_requests 표에서 revert_of_verified 열 삭제
ALTER TABLE IF EXISTS public.code_review_requests DROP COLUMN IF EXISTS revert_of_verified;

-- 2. pipeline_jobs 표에서 revert_of_verified 열 삭제
ALTER TABLE IF EXISTS public.pipeline_jobs DROP COLUMN IF EXISTS revert_of_verified;

COMMIT;
