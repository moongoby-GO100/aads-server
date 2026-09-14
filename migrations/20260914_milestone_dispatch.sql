-- 마일스톤 착수 지시 발송 기록.
--
-- 2026-09-14. 골 오케스트레이션이 상태만 옮기고 **담당에게 말을 걸지
-- 않았다.** 마일스톤이 in_progress 로 바뀌어도 그 일을 할 세션은 아무것도
-- 모른다. 대표님이나 주도가 손으로 말을 걸어야 움직였다.
--
-- 자동으로 말을 걸려면 "이미 보냈는가" 를 알아야 한다. 없으면 스케줄러가
-- 2~3분마다 같은 지시를 다시 보낸다 — 폭주한다.
ALTER TABLE milestones ADD COLUMN IF NOT EXISTS dispatched_at timestamptz;
ALTER TABLE milestones ADD COLUMN IF NOT EXISTS dispatched_session_id uuid;

-- 보낼 대상을 고르는 질의가 전체를 훑지 않도록.
CREATE INDEX IF NOT EXISTS idx_milestones_dispatch_pending
    ON milestones (goal_id)
    WHERE status = 'in_progress' AND dispatched_at IS NULL;

-- 보낸 것과 **답이 온 것**은 다르다.
--
-- 2026-09-14 실측. 운영인프라담당에게 지시를 넣은 직후 배포로 슬롯이
-- 바뀌면서 스트림이 끊겼고, 세션에는 이것만 남았다:
--
--   "⚠️ 응답 생성이 중단되어 여기까지 보존된 내용이 없습니다."
--
-- 보낸 기록만 남기면 이런 건이 **영원히 멈춘 채로 조용히 방치된다.**
-- 그래서 횟수를 세고, 답이 없으면 다시 보내고, 한도를 넘으면 사람에게
-- 넘긴다. 조용히 포기하지 않는다.
ALTER TABLE milestones ADD COLUMN IF NOT EXISTS dispatch_count integer NOT NULL DEFAULT 0;
ALTER TABLE milestones ADD COLUMN IF NOT EXISTS dispatch_note text;
