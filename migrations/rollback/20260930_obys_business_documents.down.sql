-- Rollback: AADS-OBYS-BUSINESS-DOCUMENTS-20260930
-- 오비서 업무 DB 전용, 수동 적용. AADS DB 에 돌리지 않는다.
-- 주의: 사업자 서류 메타데이터(경로·sha256·발급일·만료일·이력)가 사라진다. 원본 파일은
-- OBYS_UPLOAD_ROOT/<tenant>/business_documents/ 에 그대로 남는다.
-- 되돌리기 전에 표를 덤프해 두어라:
--   pg_dump "$OBYS_DATABASE_URL" -t yeoljeong_business_documents > business_documents.sql

BEGIN;

DO $$
BEGIN
    IF to_regclass('public.yeoljeong_businesses') IS NULL THEN
        RAISE EXCEPTION '오비서 업무 DB 전용이다';
    END IF;
END
$$;

DROP TABLE IF EXISTS yeoljeong_business_documents;

COMMIT;
