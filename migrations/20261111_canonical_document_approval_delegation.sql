-- 정본 문서 승인 위임: 사람이 부여하고 담당 채팅 세션이 교차검수 통과 문서를 승인한다.
-- 사람 승인 경로·자동 승인 기준은 바꾸지 않는다. 추가만 하는 변경(새 표·부분 색인·기존 표에 기본값 없는 nullable 열)이라 릴리스 자동 적용 대상.
-- 멱등: 두 번 실행해도 오류가 없다(IF NOT EXISTS).
-- 되돌림: migrations/rollback/20261111_canonical_document_approval_delegation.down.sql
--         (수동 실행용: DROP TABLE IF EXISTS, ALTER TABLE ... DROP COLUMN IF EXISTS).
--
-- 이 파일이 만드는 객체:
--   1) 표 project_document_approval_delegations
--   2) 색인 project_document_approval_delegations_lookup_idx
--   3) 열 project_document_events.metadata
--
-- 기존 표 영향:
--   project_document_events 에 ADD COLUMN IF NOT EXISTS metadata jsonb 를 수행한다.
--   기본값 없는 nullable 열 추가이므로 표 전체 재작성(rewrite)이 발생하지 않고, 짧은 ACCESS EXCLUSIVE 잠금만 소요된다.
--   action='approved' 는 그대로 쓰므로 기존 승인 집계 쿼리는 영향이 없다.
--
-- 트랜잭션: 적용기(apply_migration.sh)와 자동 검증기가 이 파일을 한 트랜잭션으로 감싸고 뒤에
--   schema_migrations 기록을 붙인다. 그래서 파일 안에 BEGIN/COMMIT 을 두지 않는다 — 내부 COMMIT 이
--   있으면 장부 기록과 원자성이 깨진다. SET LOCAL 은 그 트랜잭션 끝에 저절로 풀린다.
--
-- 잠금·시간 제한: lock_timeout 5초, statement_timeout 60초.
--   새 빈 표와 부분 색인 생성은 즉시 끝나며, project_document_events 열 추가 역시 nullable 기본값 없으므로 즉시 끝난다.
--
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';

CREATE TABLE IF NOT EXISTS project_document_approval_delegations (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    project_key text NOT NULL CHECK (project_key ~ '^[A-Z0-9][A-Z0-9_-]{0,63}$'),
    goal_id uuid,
    grantee text NOT NULL CHECK (length(grantee) BETWEEN 1 AND 128),
    granted_by text NOT NULL CHECK (length(granted_by) > 0),
    expires_at timestamptz NOT NULL,
    revoked_at timestamptz,
    revoked_by text,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS project_document_approval_delegations_lookup_idx
    ON project_document_approval_delegations(tenant_id, project_key, grantee)
    WHERE revoked_at IS NULL;

-- 감사 메타데이터. action='approved' 는 그대로 쓰므로 기존 승인 집계 쿼리는 영향이 없다.
ALTER TABLE project_document_events ADD COLUMN IF NOT EXISTS metadata jsonb;
