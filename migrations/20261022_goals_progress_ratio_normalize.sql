-- AADS-GOAL-PROGRESS-SCALE-UNIFY-20261022
-- goals.progress 의 정본 단위는 0~1 비율이다 (goal_manager._update_goal_progress).
-- work_item 롤업 경로가 0~100 퍼센트를 써서 1(=1%)이 화면에 100% 로 보일 수 있었다.
-- 이 마이그레이션은 이미 퍼센트로 저장된 행(progress > 1)만 /100 한다.
-- 멱등: 한 번 적용되면 모든 행이 <= 1 이라 재실행해도 대상이 없다. 삭제·TRUNCATE 없음.
-- work_items.progress(NUMERIC 0~100)는 건드리지 않는다.

UPDATE goals
   SET progress = LEAST(round((progress / 100.0)::numeric, 2), 1.0)::real
 WHERE progress > 1;
