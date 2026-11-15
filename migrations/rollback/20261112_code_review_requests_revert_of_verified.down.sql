-- 되돌림 스크립트 (수동 실행용)
-- 대응 마이그레이션: migrations/20261112_code_review_requests_revert_of_verified.sql
--
-- ■ 되돌림 범위 및 삭제 객체
--   - 열: code_review_requests.revert_of_verified
--   - 열: pipeline_jobs.revert_of_verified
--   - 기존 객체(두 표의 기존 열들과 모든 작업·리뷰 행 데이터)는 전혀 삭제하거나 변경하지 않습니다.
--
-- ■ 데이터 백업 권장 절차 (실행 전 필수)
--   배포 후 운영 중에 러너가 기록한 검증 메타데이터({sha, patch_id, files})가 열과 함께 삭제되므로 실행 전 백업을 권장합니다:
--     1) COPY (SELECT id, revert_of_verified FROM code_review_requests WHERE revert_of_verified IS NOT NULL)
--        TO '/root/aads/backups/crr_revert_of_verified_before_rollback.csv' WITH CSV HEADER;
--     2) COPY (SELECT job_id, revert_of_verified FROM pipeline_jobs WHERE revert_of_verified IS NOT NULL)
--        TO '/root/aads/backups/pj_revert_of_verified_before_rollback.csv' WITH CSV HEADER;
--
-- ■ 선행 조건: API 및 러너 코드를 구 버전으로 먼저 롤백한 후 실행합니다 (CEO 승인 사항).
-- ■ 의존 순서: 두 열은 상호 독립적이므로 순서에 제약이 없습니다. IF EXISTS 를 사용합니다.
--
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';

-- 1. code_review_requests 표에서 revert_of_verified 열 삭제
ALTER TABLE IF EXISTS code_review_requests DROP COLUMN IF EXISTS revert_of_verified;

-- 2. pipeline_jobs 표에서 revert_of_verified 열 삭제
ALTER TABLE IF EXISTS pipeline_jobs DROP COLUMN IF EXISTS revert_of_verified;
