-- 정확한 되돌리기(REVERT_OF) 예외: 러너가 patch-id 로 검증한 결과를 비동기 리뷰 요청과 함께 보존한다.
-- 사람 승인 경로·자동 승인 기준은 바꾸지 않는다. 추가만 하는 변경(기존 표 2개에 기본값 없는 nullable 열 추가)이라 릴리스 자동 적용 대상.
-- 멱등: 두 번 실행해도 오류가 없다(IF NOT EXISTS).
-- 되돌림: migrations/rollback/20261112_code_review_requests_revert_of_verified.down.sql 및
--         migrations/20261112_code_review_requests_revert_of_verified_rollback.sql
--         (수동 실행용: ALTER TABLE ... DROP COLUMN IF EXISTS).
--
-- ■ 이 파일이 만드는 객체 (전부 추가형, 멱등):
--   1) 열: code_review_requests.revert_of_verified (jsonb, nullable, no default)
--   2) 열: pipeline_jobs.revert_of_verified (jsonb, nullable, no default)
--
-- ■ 1차 롤백(API 이전 버전 복귀) 시 잔존 객체의 무해성:
--   1) 새 열 참조: 구 버전 코드는 code_review_requests.revert_of_verified 및 pipeline_jobs.revert_of_verified 열을 참조하지 않는다.
--      새 열을 읽고 쓰는 곳은 신규 코드(app/api/code_review.py, scripts/pipeline-runner.sh) 뿐이므로,
--      API 컨테이너를 이전 판으로 되돌려도 잔존 열은 어떤 경로에서도 읽히거나 쓰이지 않고 무해하게 남는다.
--   2) 구 버전 쿼리 영향:
--      구 버전 코드가 실행하는 INSERT INTO code_review_requests(...) 및 INSERT INTO pipeline_jobs(...) 문장은
--      revert_of_verified 열을 지정하지 않는다. 새 열은 기본값 없는 nullable(NULL 허용) 열이므로 PostgreSQL 이
--      자동으로 NULL 을 채워 정상 실행된다.
--      기존 SELECT 쿼리들 역시 필요한 명시적 열 목록만 조회하므로, 잔존 열이 쿼리 결과나 데이터 매핑에 영향을 주지 않는다.
--   3) 외래 키 및 제약조건 영향:
--      새 표, 외래 키, 체크 제약조건, UNIQUE 제약조건, 트리거를 일절 생성하지 않는다.
--      따라서 1차 롤백 시 잔존 열로 인한 외래키 충돌이나 제약조건 위반 가능성은 전혀 없다.
--
-- ■ 잠금 및 실행 시간 제한:
--   SET LOCAL lock_timeout = '5s';
--   SET LOCAL statement_timeout = '60s';
--   pipeline_jobs 및 code_review_requests 는 운영 중 상시 접근되는 핵심 표이다.
--   기본값 없는 nullable 열을 추가하는 ALTER TABLE ... ADD COLUMN IF NOT EXISTS 문은
--   표 전체 재작성(rewrite) 없이 시스템 카탈로그(pg_attribute) 갱신만 수행하므로 매우 짧은 ACCESS EXCLUSIVE 잠금만 요구된다.
--   5초 잠금 제한(lock_timeout = '5s')을 명시하여 다른 트랜잭션의 잠금 대기가 5초를 초과하면
--   즉시 문장이 취소되고 전체 트랜잭션이 롤백되므로, 운영 쿼리 대기열 적체나 서비스 지연을 방지한다.
--
-- ■ 수동 되돌림(down SQL)의 범위와 데이터 백업 책임:
--   되돌림 스크립트는 이번 마이그레이션이 추가한 두 열(code_review_requests.revert_of_verified,
--   pipeline_jobs.revert_of_verified)만 정확히 제거하며, 두 표의 기존 열들과 모든 작업·리뷰 행 데이터는 보존한다.
--   수동 되돌림 실행 시 신규 기능 운영 중 러너가 기록한 검증 메타데이터({sha, patch_id, files})가 열과 함께 영구 삭제되므로,
--   되돌림 실행 전 백업을 권장한다(CEO 승인 사항):
--     COPY (SELECT id, revert_of_verified FROM code_review_requests WHERE revert_of_verified IS NOT NULL)
--       TO '/root/aads/backups/crr_revert_of_verified_before_rollback.csv' WITH CSV HEADER;
--     COPY (SELECT job_id, revert_of_verified FROM pipeline_jobs WHERE revert_of_verified IS NOT NULL)
--       TO '/root/aads/backups/pj_revert_of_verified_before_rollback.csv' WITH CSV HEADER;
--   의존 순서: 두 열은 상호 독립적이므로 순서 제약이 없다. IF EXISTS 를 사용한다.
--
-- ■ 실패 시 복구 책임과 절차:
--   적용기(apply_migration.sh)와 자동 검증기(release_migration_autoverify.py)는 단일 트랜잭션 안에서 이 파일을 실행한다.
--   적용 중 잠금 타임아웃 등 오류 발생 시 즉시 ROLLBACK 되므로 두 표 모두 열이 추가되지 않은 깨끗한 상태로 복구된다.
--   내부 BEGIN/COMMIT 은 두지 않는다(적용기 트랜잭션과의 원자성 유지).
--
-- ■ 객체 정의 검증:
--   적용 후 migrations/20261112_code_review_requests_revert_of_verified.verify.sql 을 실행하여
--   두 표의 revert_of_verified 열 존재, 데이터 타입(jsonb), NULL 허용 여부(YES)를 검증한다.
--
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';

-- 정확한 되돌리기(REVERT_OF) 예외: 러너가 patch-id 로 검증한 결과를 비동기 리뷰 요청과 함께 보존한다.
-- 선택 컬럼이다. 없으면(NULL) 보존 하드게이트는 종전과 동일하다.
ALTER TABLE code_review_requests
    ADD COLUMN IF NOT EXISTS revert_of_verified JSONB;

-- 러너의 검증 기록(신뢰 근거). /api/v1/review 는 인증 면제라 요청 본문의 revert_of_verified 는 믿지 않고,
-- 리뷰어가 이 컬럼과 sha·patch_id 가 같을 때만 면제한다. 쓰기는 러너(DB 직접 접속)만 한다.
ALTER TABLE pipeline_jobs
    ADD COLUMN IF NOT EXISTS revert_of_verified JSONB;
