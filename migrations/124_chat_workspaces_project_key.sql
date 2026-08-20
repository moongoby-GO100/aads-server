-- [C안 2/4] chat_workspaces.project_key 신설 + 백필
-- 목적: 워크스페이스 표시명(예: '[GO100] 백억이')과 프로젝트 정규 키(GO100)를 분리.
--       app/core/project_config.normalize_project_label() 과 동일한 규칙을 SQL로 구현.
-- 안전: ADD COLUMN IF NOT EXISTS / 백필은 NULL 인 행만. 파괴적 변경 없음.

BEGIN;

ALTER TABLE chat_workspaces
    ADD COLUMN IF NOT EXISTS project_key VARCHAR(32);

-- 백필 1) '[TOKEN] 표시명' 패턴 → TOKEN 대문자
UPDATE chat_workspaces
   SET project_key = UPPER(TRIM((regexp_match(name, '^\[([^\]]+)\]'))[1]))
 WHERE project_key IS NULL
   AND name ~ '^\[[^\]]+\]';

-- 백필 2) 대괄호가 없는 이름 → 이름 원본(trim)
UPDATE chat_workspaces
   SET project_key = NULLIF(TRIM(name), '')
 WHERE project_key IS NULL;

-- 백필 3) 별칭 정규화 (project_config.PROJECT_MAP aliases 와 동일)
UPDATE chat_workspaces SET project_key = 'NTV2'
 WHERE UPPER(project_key) IN ('NEWTALK', 'NEWTALK-V2', 'NTV2');
UPDATE chat_workspaces SET project_key = 'SF'
 WHERE UPPER(project_key) IN ('SHORTFLOW', 'SF');
UPDATE chat_workspaces SET project_key = 'GO100'
 WHERE UPPER(project_key) IN ('GO100', '백억이');
UPDATE chat_workspaces SET project_key = 'KIS'
 WHERE UPPER(project_key) IN ('KIS', 'KIS-AUTOTRADE');
UPDATE chat_workspaces SET project_key = 'AADS'
 WHERE UPPER(project_key) = 'AADS';

CREATE INDEX IF NOT EXISTS idx_chat_workspaces_project_key
    ON chat_workspaces (project_key);

COMMIT;
