-- 165: goal_task_links 바인딩 provenance + 재시도 supersession
--
-- 배경 (Goal Control P0):
--   * 구 `_auto_link_job_to_goal` 은 project 만 보고 활성 목표에 붙였다 → 무관한
--     작업이 목표에 매달렸는데, 왜 붙었는지 구분할 근거가 테이블에 없었다.
--   * 재시도로 대체된 과거 실패 시도가 마일스톤을 영구히 blocked 로 만들었다.
--
-- 이 마이그레이션은 **컬럼만 추가하고 삭제/상태 재작성은 하지 않는다.**
-- 실제 상태 정합화는 goal_link_reconciler 의 dry-run → 승인 → repair 로 한다.
-- 여러 번 실행해도 안전하다 (전부 IF NOT EXISTS / 조건부 UPDATE).

BEGIN;

ALTER TABLE goal_task_links
    ADD COLUMN IF NOT EXISTS bind_source    text,
    ADD COLUMN IF NOT EXISTS bind_evidence  jsonb,
    ADD COLUMN IF NOT EXISTS superseded_by  text,
    ADD COLUMN IF NOT EXISTS superseded_at  timestamptz,
    ADD COLUMN IF NOT EXISTS reconciled_at  timestamptz,
    ADD COLUMN IF NOT EXISTS updated_at     timestamptz;

-- 기존 행은 전부 구 project-only 자동 연결 경로에서 생겼다(신규 경로는 항상
-- bind_source 를 채운다). 이 백필은 결정적이며 행을 지우지 않는다.
UPDATE goal_task_links
   SET bind_source = 'legacy_auto'
 WHERE bind_source IS NULL;

UPDATE goal_task_links
   SET updated_at = COALESCE(updated_at, created_at, NOW())
 WHERE updated_at IS NULL;

-- 재시도 대체 관계 조회용 (supersede 되지 않은 링크만 완료 판정에 쓴다)
CREATE INDEX IF NOT EXISTS idx_goal_task_links_superseded
    ON goal_task_links (milestone_id)
    WHERE superseded_by IS NULL;

CREATE INDEX IF NOT EXISTS idx_goal_task_links_bind_source
    ON goal_task_links (bind_source);

COMMENT ON COLUMN goal_task_links.bind_source IS
    'request | directive | explicit | legacy_auto — 이 연결이 생긴 근거';
COMMENT ON COLUMN goal_task_links.superseded_by IS
    '이 시도를 대체한 재시도 작업의 task_id. NULL 이면 현행 시도.';
COMMENT ON COLUMN goal_task_links.bind_evidence IS
    '정합화/supersede 판정 근거(작업 상태, instruction_hash 일치 등) 감사 기록';

COMMIT;
