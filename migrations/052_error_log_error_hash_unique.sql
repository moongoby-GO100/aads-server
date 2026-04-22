-- 052_error_log_error_hash_unique.sql
-- ON CONFLICT (error_hash) 가 동작하도록 UNIQUE 제약 부착
-- 2026-04-22: watchdog 관측 공백(4/1 이후 21일간 insert 실패) 복구

-- 1) 혹시 남은 중복 정리: 동일 error_hash 는 최신 id 만 보존
DELETE FROM error_log e1
USING error_log e2
WHERE e1.error_hash = e2.error_hash
  AND e1.id < e2.id;

-- 2) 무중단 UNIQUE 인덱스 생성 (쓰기 락 없음)
CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS idx_error_log_hash_unique
  ON error_log(error_hash);

-- 3) 제약으로 승격 (ON CONFLICT 타겟 자격 부여)
ALTER TABLE error_log
  ADD CONSTRAINT error_log_error_hash_key UNIQUE USING INDEX idx_error_log_hash_unique;

-- 4) 중복되는 구 non-unique 인덱스 제거
DROP INDEX IF EXISTS idx_error_log_hash;
