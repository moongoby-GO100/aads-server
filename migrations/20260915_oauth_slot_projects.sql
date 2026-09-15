-- 슬롯별 프로젝트 배정 (2026-09-15 대표님 지시)
--
-- "슬롯에 복수의 프로젝트를 지정하고 지정된 프로젝트는 해당 슬롯을 우선
--  사용 하게 가능하지? 미설정시 전체 프로젝트에서 사용가능하게 하고"
--
-- 규칙 셋.
--
--   1. 배정이 **없는** 슬롯 → 모든 프로젝트가 쓴다 (지금까지의 동작 그대로).
--   2. 배정이 **있는** 슬롯 → 그 프로젝트들만 쓰고, 그 프로젝트는 이 슬롯을
--      **먼저** 집는다.
--   3. 배정된 프로젝트 안에서는 최후 수단 스위치(`slot_gate`)를 묻지 않는다.
--      배정한 것 자체가 허락이다. 배정 밖 프로젝트에서는 스위치와 무관하게
--      아예 후보가 아니다.
--
-- 슬롯 번호를 text 로 두는 이유: 코드 전체가 슬롯을 "1"/"2"/"3" 문자열로
-- 다룬다. 여기서만 정수로 두면 비교할 때마다 변환이 끼어들고, 그 변환을
-- 빠뜨리는 자리가 반드시 생긴다.
CREATE TABLE IF NOT EXISTS oauth_slot_projects (
    slot        text NOT NULL,
    project_key text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    created_by  text NOT NULL DEFAULT 'CEO',
    PRIMARY KEY (slot, project_key)
);

CREATE INDEX IF NOT EXISTS idx_oauth_slot_projects_project
    ON oauth_slot_projects (project_key);
