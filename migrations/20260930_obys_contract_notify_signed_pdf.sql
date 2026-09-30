-- AADS-OBYS-CONTRACT-NOTIFY-PDF-20260930
-- 계약서 서명요청 알림 발송 이력 + 서명 완료본 PDF 보관·교부 컬럼.
--
-- 적용 대상: 오비서 업무 DB(OBYS_DATABASE_URL, 진아서버)만. AADS DB 에 돌리지 않는다.
--   scripts/migrations_auto_apply_baseline.txt 에 올려 자동 적용에서 HOLD 한다.
--   적용: psql "$OBYS_DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/20260930_obys_contract_notify_signed_pdf.sql
-- 스키마 추가만 한다. 기존 계약서 행(tenant 15055cac-…, draft 6건)의 값은 바꾸지 않는다.
--   ADD COLUMN 은 전부 NULL 허용·기본값 없음이라 기존 행을 다시 쓰지 않는다.
-- 멱등하다(IF NOT EXISTS).
-- 롤백: migrations/rollback/20260930_obys_contract_notify_signed_pdf.down.sql

BEGIN;

DO $$
BEGIN
    IF to_regclass('public.yeoljeong_contracts') IS NULL
       OR to_regclass('public.yeoljeong_business_tenant_mapping') IS NULL THEN
        RAISE EXCEPTION '오비서 업무 DB 전용이다 (yeoljeong_contracts / yeoljeong_business_tenant_mapping 없음)';
    END IF;
END
$$;

-- 발송 이력. 수신번호·이메일 원문은 저장하지 않고 마스킹 값만 둔다.
CREATE TABLE IF NOT EXISTS yeoljeong_contract_notifications (
    id BIGSERIAL PRIMARY KEY,
    tenant_id UUID NOT NULL,
    contract_id TEXT NOT NULL,
    event TEXT NOT NULL DEFAULT 'signature_requested'
        CHECK (event IN ('signature_requested', 'signed', 'delivered')),
    channel TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('sent', 'failed', 'skipped')),
    target_masked TEXT NOT NULL DEFAULT '',
    error_detail TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_yf_contract_notifications_contract
    ON yeoljeong_contract_notifications (tenant_id, contract_id, created_at DESC);

-- 서명본 PDF 보관·교부. 값의 원본은 contract_payload 이고 컬럼은 조회용 사본이다.
ALTER TABLE yeoljeong_contracts ADD COLUMN IF NOT EXISTS signed_pdf_path TEXT;
ALTER TABLE yeoljeong_contracts ADD COLUMN IF NOT EXISTS signed_pdf_sha256 TEXT;
ALTER TABLE yeoljeong_contracts ADD COLUMN IF NOT EXISTS signed_pdf_bytes BIGINT;
ALTER TABLE yeoljeong_contracts ADD COLUMN IF NOT EXISTS delivered_at TIMESTAMPTZ;
ALTER TABLE yeoljeong_contracts ADD COLUMN IF NOT EXISTS delivery_channel TEXT;

COMMIT;
