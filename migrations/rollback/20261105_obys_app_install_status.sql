-- 롤백: 앱 설치 현황은 파생 정보라 테이블째 제거한다. 라우터를 이전 릴리스로 먼저 되돌린 뒤 실행한다.
BEGIN;
DO $$ BEGIN
    IF current_database() <> 'obys' THEN
        RAISE EXCEPTION 'OBYS business database required';
    END IF;
END $$;
DROP TABLE IF EXISTS public.obys_app_install_status;
COMMIT;
