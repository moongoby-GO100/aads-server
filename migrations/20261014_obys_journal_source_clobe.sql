-- OBYS 전표 source_type 에 'clobe' 추가 (클로브 확정 거래 → 전표 초안, 파일럿).
-- 허용 값을 넓히기만 한다: 기존 4개 값은 그대로이고 데이터는 건드리지 않는다. 멱등.
-- DROP CONSTRAINT 는 CHECK 교체에만 쓴다(20260921_obys_journal_vouchers_r3.sql 과 같은 방식). 테이블·컬럼·행은 지우지 않는다.
-- 가드: aads DB 에는 yeoljeong_* 테이블이 없다(115_yeoljeong_finance_hr_ledgers.sql 은 ledger 에만 backfill 기록).
--       테이블이 없으면 아무것도 하지 않고 통과한다 — 아니면 schema_migrations 단계에서
--       모든 릴리스가 멈춘다(2026-10-14 deploy_run 5747). 테이블이 있는 DB(obys)에서는 가드 없던 때와 같은 결과다.
BEGIN;

DO $mig$
BEGIN
    IF to_regclass('public.yeoljeong_journal_vouchers') IS NULL THEN
        RAISE NOTICE 'skip 20261014_obys_journal_source_clobe: public.yeoljeong_journal_vouchers absent';
        RETURN;
    END IF;

    EXECUTE 'ALTER TABLE yeoljeong_journal_vouchers DROP CONSTRAINT IF EXISTS yeoljeong_journal_vouchers_source_type_check';
    EXECUTE $sql$ALTER TABLE yeoljeong_journal_vouchers ADD CONSTRAINT yeoljeong_journal_vouchers_source_type_check
    CHECK (source_type IN ('uploaded_ledger_row','manual_ledger_entry','card_transaction','bank_transaction','clobe')) NOT VALID$sql$;
    EXECUTE 'ALTER TABLE yeoljeong_journal_vouchers VALIDATE CONSTRAINT yeoljeong_journal_vouchers_source_type_check';
END
$mig$;

COMMIT;
