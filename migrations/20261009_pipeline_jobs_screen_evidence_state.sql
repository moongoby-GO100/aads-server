-- 화면 증거 게이트 순서 교정: 승인 시 검증 계획만 확인하고, 증거는 배포 후 워치독이 만든다.
-- screen_evidence_state: NULL | pending_release | verifying | passed | failed | unverifiable | not_required
-- e2e_spec: 승인 시점에 확정한 배포 후 E2E 계획 {"url": "...", "selectors": ["..."], "source": "..."}
-- 멱등·가산 전용(DROP 없음). 두 컬럼 모두 NULL 허용이라 롤백은 코드가 컬럼을 읽지 않게 되는 것으로 충분하다.
-- 적용은 CEO 별도 승인 후. 컬럼이 없으면 코드는 상태·계획 저장과 워치독을 건너뛴다(screen_columns_ready).
ALTER TABLE pipeline_jobs
    ADD COLUMN IF NOT EXISTS screen_evidence_state TEXT NULL,
    ADD COLUMN IF NOT EXISTS e2e_spec JSONB NULL;
