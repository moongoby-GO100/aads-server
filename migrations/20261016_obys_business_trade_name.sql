-- ACCT-OBYS-SEAL-REGISTERED-TRADE-NAME-20261016
-- 사업장에 사업자등록증 상호(법인명) 칸을 추가한다. 계약서 도장 띠와 새 계약서의 사용자 상호가 이 값을 쓴다.
-- name 은 화면 표시용 지점명이라 등록증 상호와 다를 수 있다(예: name "열정국밥 중화점" / 등록증 법인명 "주식회사 윤희에프엔비").
-- nullable text, 기본값 없음, 기존 행 변경 없음. 멱등(IF NOT EXISTS). DROP/RENAME/NOT NULL 없음.
-- 가드: aads DB 에는 yeoljeong_* 테이블이 없다. 테이블이 없으면 아무것도 하지 않고 통과한다 —
--       아니면 schema_migrations 단계에서 모든 릴리스가 멈춘다(tests/unit/test_migrations_yeoljeong_guard.py).
-- 적용: 배포 시 자동 적용은 aads DB 에서 no-op 이다. 오비서 업무 DB(OBYS_DATABASE_URL, 진아서버)에는
--   배포와 별개로 수동 적용한다.
--   psql "$OBYS_DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/20261016_obys_business_trade_name.sql
-- 코드는 이 컬럼이 아직 없어도 읽기를 깨뜨리지 않는다(to_jsonb 로 읽는다). 쓰기만 컬럼이 있어야 한다.
-- 되돌리기: 코드 커밋 git revert 1회. 추가된 nullable 컬럼은 남아도 무해하다.
BEGIN;

DO $mig$
BEGIN
    IF to_regclass('public.yeoljeong_businesses') IS NULL THEN
        RAISE NOTICE 'skip 20261016_obys_business_trade_name: public.yeoljeong_businesses absent';
        RETURN;
    END IF;

    EXECUTE 'ALTER TABLE yeoljeong_businesses ADD COLUMN IF NOT EXISTS trade_name text';
END
$mig$;

COMMIT;
