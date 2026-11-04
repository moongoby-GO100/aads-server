-- 마일스톤 반려 시각. 주도가 반려한 뒤 새 연결 작업이나 담당의 직접 신고가 있을 때만 재신고한다.
-- 적용 전에도 앱은 열 부재를 감지해 기존 동작으로 폴백한다. 두 열 모두 nullable 이라 되돌린 뒤 남겨 둬도 무해하다.
--
-- milestones.review_rejected_at: 주도가 반려한 시각. 담당이 직접 신고하거나 새 연결로 재신고되면 NULL.
-- goal_task_links.milestone_linked_at: 작업이 "이 마일스톤에" 연결된 시각. created_at 은 같은 목표에
--   먼저 연결해 둔 작업을 나중에 마일스톤으로 옮겨도 그대로라서 "반려 이후의 새 연결" 판정에 쓸 수 없다.
--   모든 쓰기 경로(goal_manager / pipeline_runner_service 등)가 같은 규칙을 따르도록 트리거가 찍는다.
--   NULL 이면 앱은 created_at 으로 폴백한다(이 마이그레이션 이전에 만든 연결).
--
-- 롤백(필요 시에만, 앱을 먼저 되돌린 뒤):
--   DROP TRIGGER IF EXISTS trg_goal_task_links_milestone_linked_at ON goal_task_links;
--   DROP FUNCTION IF EXISTS goal_task_links_stamp_milestone_linked_at();
--   ALTER TABLE goal_task_links DROP COLUMN IF EXISTS milestone_linked_at;
--   ALTER TABLE milestones DROP COLUMN IF EXISTS review_rejected_at;
ALTER TABLE milestones ADD COLUMN IF NOT EXISTS review_rejected_at timestamptz NULL;
ALTER TABLE goal_task_links ADD COLUMN IF NOT EXISTS milestone_linked_at timestamptz NULL;

CREATE OR REPLACE FUNCTION goal_task_links_stamp_milestone_linked_at() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.milestone_id IS NOT NULL AND NEW.milestone_linked_at IS NULL THEN
            NEW.milestone_linked_at := NOW();
        END IF;
    ELSIF NEW.milestone_id IS DISTINCT FROM OLD.milestone_id THEN
        NEW.milestone_linked_at := CASE WHEN NEW.milestone_id IS NULL THEN NULL ELSE NOW() END;
    END IF;
    RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS trg_goal_task_links_milestone_linked_at ON goal_task_links;
CREATE TRIGGER trg_goal_task_links_milestone_linked_at
    BEFORE INSERT OR UPDATE OF milestone_id ON goal_task_links
    FOR EACH ROW EXECUTE FUNCTION goal_task_links_stamp_milestone_linked_at();
