-- 재작업 소진 주도 판단 요청 장부.
-- 목적: 재작업 실행 한도(2회)를 다 쓴 마일스톤에서 "사람이 확인해야 한다" 로만 끝나던 것을
--       주도 세션 -> (무응답 시) 대표님 순서로 올리되, 소진 시점에는 이 표에 행 1개만 남기고
--       발송은 커밋 뒤 별도 스윕(app/services/goal_retry_exhaust_triage.py)이 한다.
-- first_attempt_at: 첫 발송 시도 직전에 기록. 무응답 판정 기준은 COALESCE(first_attempt_at, sent_at).
-- 멱등: 두 번 실행해도 오류가 없다(IF NOT EXISTS).
-- 되돌림: migrations/rollback/20261106_goal_retry_exhaust_requests.down.sql (DROP TABLE).
--         코드는 이 표가 없으면 기록·스윕을 조용히 건너뛰므로 표를 먼저 지워도 안전하다.
--
-- 이 파일이 만드는 객체(전부 새 이름 — 운영 DB 에 아직 없음):
--   1) 표 goal_retry_exhaust_requests
--   2) 색인 idx_goal_retry_exhaust_requests_kind_status
--   3) 열 first_attempt_at (표 생성문에도 있고, 아래 ADD COLUMN IF NOT EXISTS 가 한 번 더 보장한다)
--
-- 기존 표를 참조·변경하지 않는다(FK·트리거 없음). 잠금은 새로 만드는 빈 표에만 걸린다.
--
-- 트랜잭션: 적용기(apply_migration.sh)와 자동 검증기가 이 파일을 한 트랜잭션으로 감싸고 뒤에
--   schema_migrations 기록을 붙인다. 그래서 파일 안에 BEGIN/COMMIT 을 두지 않는다 — 내부 COMMIT 이
--   있으면 장부 기록과 원자성이 깨진다. SET LOCAL 은 그 트랜잭션 끝에 저절로 풀린다.
--
-- 잠금·시간 제한: lock_timeout 5초, statement_timeout 60초. 새 빈 표라 평소에는 즉시 끝난다.
--   ADD COLUMN IF NOT EXISTS 는 기본값 없는 nullable 열이라 표를 다시 쓰지 않는다.
--
-- 개인정보·시크릿: last_error·delivery_ref·resolution 은 자유 텍스트다. 시크릿을 넣지 않는 것은
--   앱 책임이다(현재 코드는 오류 문자열 300자, 알림 제목만 저장한다).
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';

CREATE TABLE IF NOT EXISTS goal_retry_exhaust_requests (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL,
    goal_id uuid NOT NULL,
    milestone_id uuid NOT NULL,
    lineage text NOT NULL,
    kind text NOT NULL CHECK (kind IN ('lead', 'ceo')),
    status text NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'sent', 'resolved', 'escalated', 'undeliverable')),
    target_session_id uuid,
    milestone_status_at_request text,
    executions int,
    attempts int NOT NULL DEFAULT 0,
    last_error text,
    delivery_ref text,
    first_attempt_at timestamptz,
    sent_at timestamptz,
    resolved_at timestamptz,
    resolution text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, milestone_id, lineage, kind)
);
CREATE INDEX IF NOT EXISTS idx_goal_retry_exhaust_requests_kind_status
    ON goal_retry_exhaust_requests (kind, status, created_at);
ALTER TABLE goal_retry_exhaust_requests ADD COLUMN IF NOT EXISTS first_attempt_at timestamptz;
