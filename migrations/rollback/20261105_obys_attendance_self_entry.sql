-- 비파괴 롤백: 신규 신청 이력과 kind 컬럼은 보존하고 대기 중복 방지 인덱스만 제거한다.
-- 라우터를 이전 릴리스로 되돌리면 kind='new_entry' 행은 record_id 가 NULL 일 수 있어 이전 코드가 읽지 않는다.
BEGIN;
DO $$ BEGIN
    IF current_database() <> 'obys' THEN
        RAISE EXCEPTION 'OBYS business database required';
    END IF;
END $$;
DROP INDEX IF EXISTS public.idx_obys_correction_new_entry_pending;
COMMIT;
