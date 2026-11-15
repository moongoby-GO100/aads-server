-- 릴리스 마이그레이션 객체 정의 정밀 검증 (읽기 전용 SELECT)
-- 대상: migrations/20261112_code_review_requests_revert_of_verified.sql
-- 검증 항목: code_review_requests 및 pipeline_jobs 표의 revert_of_verified 열 정의(타입·NULL여부)

-- 1. code_review_requests 및 pipeline_jobs 의 revert_of_verified 열 속성 확인
SELECT table_name, column_name, data_type, is_nullable, column_default
FROM information_schema.columns
WHERE table_schema = 'public'
  AND table_name IN ('code_review_requests', 'pipeline_jobs')
  AND column_name = 'revert_of_verified'
ORDER BY table_name;

-- 2. 정량적 일치 판정 (두 열 모두 jsonb, nullable 로 존재하면 'VERIFY_OK' 반환)
SELECT
    CASE WHEN
        -- code_review_requests.revert_of_verified 확인
        (SELECT count(*) FROM information_schema.columns
         WHERE table_schema = 'public'
           AND table_name = 'code_review_requests'
           AND column_name = 'revert_of_verified'
           AND data_type = 'jsonb'
           AND is_nullable = 'YES') = 1
        -- pipeline_jobs.revert_of_verified 확인
        AND (SELECT count(*) FROM information_schema.columns
             WHERE table_schema = 'public'
               AND table_name = 'pipeline_jobs'
               AND column_name = 'revert_of_verified'
               AND data_type = 'jsonb'
               AND is_nullable = 'YES') = 1
    THEN 'VERIFY_OK'
    ELSE 'VERIFY_FAIL'
    END AS verification_status;
