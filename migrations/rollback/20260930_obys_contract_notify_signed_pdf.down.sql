-- Rollback: AADS-OBYS-CONTRACT-NOTIFY-PDF-20260930
-- 오비서 업무 DB 전용, 수동 적용. AADS DB 에 돌리지 않는다.
-- 주의: 발송 이력과 PDF 보관·교부 컬럼 사본이 사라진다. 같은 값은
-- yeoljeong_contracts.contract_payload(jsonb) 에 남아 있고, PDF 파일 자체는
-- OBYS_UPLOAD_ROOT/<tenant>/contracts/ 에 그대로 있다.

BEGIN;

DO $$
BEGIN
    IF to_regclass('public.yeoljeong_contracts') IS NULL THEN
        RAISE EXCEPTION '오비서 업무 DB 전용이다';
    END IF;
END
$$;

DROP TABLE IF EXISTS yeoljeong_contract_notifications;
ALTER TABLE yeoljeong_contracts DROP COLUMN IF EXISTS signed_pdf_path;
ALTER TABLE yeoljeong_contracts DROP COLUMN IF EXISTS signed_pdf_sha256;
ALTER TABLE yeoljeong_contracts DROP COLUMN IF EXISTS signed_pdf_bytes;
ALTER TABLE yeoljeong_contracts DROP COLUMN IF EXISTS delivered_at;
ALTER TABLE yeoljeong_contracts DROP COLUMN IF EXISTS delivery_channel;

COMMIT;
