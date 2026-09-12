-- 아이디어 메모 삭제. 하드 DELETE 대신 시각을 남긴다.
--
-- status 에 '폐기' 가 이미 있지만 그것은 "논의 끝에 버린 안건"이라는 업무 상태다.
-- 사용자가 목록에서 지우는 행위와 뒤섞으면 둘 다 의미를 잃는다. 별도 컬럼으로
-- 분리하고, 잘못 지웠을 때 되돌릴 수 있게 남긴다.
ALTER TABLE ceo_agenda ADD COLUMN IF NOT EXISTS deleted_at timestamptz;
CREATE INDEX IF NOT EXISTS idx_agenda_not_deleted
    ON ceo_agenda (project, created_at DESC) WHERE deleted_at IS NULL;
