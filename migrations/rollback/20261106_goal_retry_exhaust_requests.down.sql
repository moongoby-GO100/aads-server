-- 롤백: 재작업 소진 주도 판단 요청 장부 제거. 코드는 표가 없으면 기록·스윕을 건너뛴다.
BEGIN;
DROP TABLE IF EXISTS goal_retry_exhaust_requests;
COMMIT;
