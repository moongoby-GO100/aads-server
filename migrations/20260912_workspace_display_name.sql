-- 프로젝트 별칭.
--
-- WorkspaceOut 모델과 사이드바(ws.display_name || ws.name)는 이미 별칭을 쓸
-- 준비가 돼 있는데 DB 에 컬럼이 없어 항상 NULL 이었다. 그래서 별칭을 바꿔도
-- 화면은 늘 원래 name 으로 되돌아갔다.
--
-- name 은 [CEO] 통합지시 처럼 프로젝트 키를 담은 식별용 이름이라 바꾸면
-- 다른 코드가 깨진다. 표시용 별칭을 따로 둔다.
ALTER TABLE chat_workspaces ADD COLUMN IF NOT EXISTS display_name text;
