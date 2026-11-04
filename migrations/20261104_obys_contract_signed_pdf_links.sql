-- AADS-OBYS-CONTRACT-PDF-MOBILE-DOWNLOAD-20261104
-- 서명본 PDF 1회용 다운로드 링크. 토큰 원문은 저장하지 않고 sha256 해시만 둔다.
--
-- 적용 대상: 오비서 업무 DB(OBYS_DATABASE_URL, 진아서버)만. AADS DB 에 돌리지 않는다.
--   scripts/migrations_auto_apply_baseline.txt 에 올려 자동 적용에서 HOLD 한다.
--   적용: psql "$OBYS_DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/20261104_obys_contract_signed_pdf_links.sql
-- used_by 는 최초 사용 요청자의 sha256(네트워크 주소) — Range 이어받기를 같은 요청자에게만 허용하는 데 쓴다.
-- range_hits 는 사용 뒤 이어받기 허용 횟수 상한 카운터.
-- 신규 테이블 하나만 만든다. 기존 테이블·행은 바꾸지 않는다. 멱등하다(IF NOT EXISTS).
-- 테이블이 없으면 링크 발급이 503 이 되고 화면은 종전 blob 다운로드로 되돌아간다.
-- 롤백: migrations/rollback/20261104_obys_contract_signed_pdf_links.down.sql

BEGIN;

DO $$
BEGIN
    IF to_regclass('public.yeoljeong_contracts') IS NULL THEN
        RAISE EXCEPTION '오비서 업무 DB 전용이다 (yeoljeong_contracts 없음)';
    END IF;
END
$$;

CREATE TABLE IF NOT EXISTS yeoljeong_contract_pdf_links (
    token_hash TEXT PRIMARY KEY,
    tenant_id UUID NOT NULL,
    contract_id TEXT NOT NULL,
    record_delivery BOOLEAN NOT NULL DEFAULT FALSE,
    expires_at TIMESTAMPTZ NOT NULL,
    used_at TIMESTAMPTZ,
    used_by TEXT NOT NULL DEFAULT '',
    range_hits INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_yf_contract_pdf_links_expires
    ON yeoljeong_contract_pdf_links (expires_at);

COMMIT;
