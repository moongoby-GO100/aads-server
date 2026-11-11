-- 정확한 되돌리기(REVERT_OF) 예외: 러너가 patch-id 로 검증한 결과를 비동기 리뷰 요청과 함께 보존한다.
-- 선택 컬럼이다. 없으면(NULL) 보존 하드게이트는 종전과 동일하다.
ALTER TABLE code_review_requests
    ADD COLUMN IF NOT EXISTS revert_of_verified JSONB;

-- 러너의 검증 기록(신뢰 근거). /api/v1/review 는 인증 면제라 요청 본문의 revert_of_verified 는 믿지 않고,
-- 리뷰어가 이 컬럼과 sha·patch_id 가 같을 때만 면제한다. 쓰기는 러너(DB 직접 접속)만 한다.
ALTER TABLE pipeline_jobs
    ADD COLUMN IF NOT EXISTS revert_of_verified JSONB;
