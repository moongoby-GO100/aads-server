-- 정본 문서 승인 위임: 사람이 부여하고 담당 채팅 세션이 교차검수 통과 문서를 승인한다.
-- 사람 승인 경로·자동 승인 기준은 바꾸지 않는다. 적용은 승인 후 점검 창구에서만.
BEGIN;

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

COMMIT;
