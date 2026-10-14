-- Down migration for migrations/20261014_obys_journal_source_clobe.sql (수동, 소유자 역할로. 허용목록 밖).
-- source_type CHECK 를 'clobe' 추가 이전 값으로 되돌린다. 전표 행은 지우지 않는다.
-- source_type='clobe' 전표가 남아 있으면 예외로 멈춘다 — 먼저 그 전표를 어떻게 할지 CEO/회계 확인 후 따로 정리한다.
BEGIN;

DO $guard$
BEGIN
    IF EXISTS (SELECT 1 FROM yeoljeong_journal_vouchers WHERE source_type = 'clobe') THEN
        RAISE EXCEPTION 'source_type=clobe 전표가 남아 있어 롤백하지 않는다 (건수: %)',
            (SELECT count(*) FROM yeoljeong_journal_vouchers WHERE source_type = 'clobe');
    END IF;
END
$guard$;

ALTER TABLE yeoljeong_journal_vouchers DROP CONSTRAINT IF EXISTS yeoljeong_journal_vouchers_source_type_check;
ALTER TABLE yeoljeong_journal_vouchers ADD CONSTRAINT yeoljeong_journal_vouchers_source_type_check
    CHECK (source_type IN ('uploaded_ledger_row','manual_ledger_entry','card_transaction','bank_transaction')) NOT VALID;
ALTER TABLE yeoljeong_journal_vouchers VALIDATE CONSTRAINT yeoljeong_journal_vouchers_source_type_check;

COMMIT;
