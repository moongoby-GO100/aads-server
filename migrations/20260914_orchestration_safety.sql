-- 오케스트레이션 안전장치. 배경은 docs/prd/20260914_ORCHESTRATION_SAFETY_PRD.md
--
-- 가장 큰 것부터: **완료 판정이 없어서 첫 마일스톤에서 멈춘다.**
-- check_milestone_completion 은 goal_task_links 의 상태로 판정하는데
-- #310 의 링크는 milestone_id NULL · task_type chat_session 이라 마일스톤에
-- 묶인 작업이 0건이다. 담당이 끝내도 no_linked_tasks 가 돌아오고 영원히
-- in_progress 에 남는다.

-- ── ① 완료 판정: 담당 신고 → review → 주도 확인 ──────────────────────────
ALTER TABLE milestones ADD COLUMN IF NOT EXISTS reported_at    timestamptz;
ALTER TABLE milestones ADD COLUMN IF NOT EXISTS reported_by    uuid;
ALTER TABLE milestones ADD COLUMN IF NOT EXISTS evidence       jsonb;
ALTER TABLE milestones ADD COLUMN IF NOT EXISTS review_note    text;
-- 주도가 확인을 안 하면 또 멈춘다. 지시와 같은 재알림을 걸기 위한 기록.
ALTER TABLE milestones ADD COLUMN IF NOT EXISTS review_asked_at    timestamptz;
ALTER TABLE milestones ADD COLUMN IF NOT EXISTS review_ask_count   integer NOT NULL DEFAULT 0;

-- ── ② A/B ────────────────────────────────────────────────────────────────
ALTER TABLE milestones ADD COLUMN IF NOT EXISTS variant       text;
ALTER TABLE milestones ADD COLUMN IF NOT EXISTS ab_criterion  text;
ALTER TABLE milestones ADD COLUMN IF NOT EXISTS ab_result     jsonb;

-- ── ③ 완료 기준 변경 이력 ────────────────────────────────────────────────
-- 이력이 없으면 그 마일스톤이 왜 통과됐는지 설명할 수 없다. 기준을 느슨하게
-- 바꿔서 통과시킨 것과 근거가 생겨 바꾼 것이 구분되지 않는다.
ALTER TABLE milestones ADD COLUMN IF NOT EXISTS criteria_history jsonb NOT NULL DEFAULT '[]'::jsonb;

-- ── ④ 롤백 근거 ──────────────────────────────────────────────────────────
ALTER TABLE milestones ADD COLUMN IF NOT EXISTS rollback_ref text;

-- ── ⑤ 비용 상한 ──────────────────────────────────────────────────────────
ALTER TABLE goals ADD COLUMN IF NOT EXISTS cost_limit_usd numeric(10,2);
ALTER TABLE goals ADD COLUMN IF NOT EXISTS cost_spent_usd numeric(12,4) NOT NULL DEFAULT 0;
ALTER TABLE goals ADD COLUMN IF NOT EXISTS cost_warned_at timestamptz;

-- ── ⑥ 진행 중 의견·수정 요청 ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS milestone_notes (
    id              bigserial PRIMARY KEY,
    milestone_id    uuid NOT NULL REFERENCES milestones(id) ON DELETE CASCADE,
    kind            text NOT NULL,          -- 'note' | 'change'
    body            text NOT NULL,
    criteria_before text,
    criteria_after  text,
    created_by      text NOT NULL DEFAULT 'ceo',
    created_at      timestamptz NOT NULL DEFAULT NOW(),
    delivered_at    timestamptz,
    dispatch_count  integer NOT NULL DEFAULT 0,
    answered_at     timestamptz,
    answer_excerpt  text
);
CREATE INDEX IF NOT EXISTS idx_milestone_notes_open
    ON milestone_notes (milestone_id) WHERE answered_at IS NULL;

-- ── ⑦ 담당별 멈춤 ────────────────────────────────────────────────────────
-- 목표 단위다. 같은 담당이 두 목표에 참여하면 한쪽만 멈출 수 있어야 한다.
CREATE TABLE IF NOT EXISTS owner_pause (
    goal_id    uuid NOT NULL REFERENCES goals(id) ON DELETE CASCADE,
    session_id uuid NOT NULL,
    reason     text,
    paused_by  text NOT NULL DEFAULT 'ceo',
    paused_at  timestamptz NOT NULL DEFAULT NOW(),
    PRIMARY KEY (goal_id, session_id)
);

-- 발송 대상 고르는 질의가 전체를 훑지 않도록.
CREATE INDEX IF NOT EXISTS idx_milestones_status_seq
    ON milestones (goal_id, status, sequence_order);
