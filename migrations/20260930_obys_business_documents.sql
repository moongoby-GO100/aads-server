-- AADS-OBYS-BUSINESS-DOCUMENTS-20260930
-- 사업자(법인/개인) 단위 서류 보관. 원본 파일은 OBYS_UPLOAD_ROOT 아래
--   <tenant_id>/business_documents/<uuid><ext>
-- 에 있고, 이 표는 그 메타데이터(경로·sha256·크기·발급일·만료일·상태)다. 파일 BLOB 은 넣지 않는다.
--
-- 적용 대상: 오비서 업무 DB(OBYS_DATABASE_URL, 진아서버)만. AADS DB 에 돌리지 않는다.
--   scripts/migrations_auto_apply_baseline.txt 에 올려 자동 적용에서 HOLD 한다.
--   적용: psql "$OBYS_DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/20260930_obys_business_documents.sql
-- 스키마 추가만 한다. 기존 행을 쓰지 않는다. 멱등하다(IF NOT EXISTS).
-- 같은 종류 재업로드는 새 행(리비전)으로 쌓고 이전 행은 status='superseded' 로 남긴다.
-- 삭제는 soft delete(deleted_at)만 한다.
-- 롤백: migrations/rollback/20260930_obys_business_documents.down.sql

BEGIN;

DO $$
BEGIN
    IF to_regclass('public.yeoljeong_businesses') IS NULL
       OR to_regclass('public.yeoljeong_business_tenant_mapping') IS NULL THEN
        RAISE EXCEPTION '오비서 업무 DB 전용이다 (yeoljeong_businesses / yeoljeong_business_tenant_mapping 없음)';
    END IF;
END
$$;

CREATE TABLE IF NOT EXISTS yeoljeong_business_documents (
    id UUID PRIMARY KEY,
    tenant_id UUID NOT NULL,
    business_id TEXT NOT NULL REFERENCES yeoljeong_businesses(id) ON UPDATE CASCADE,
    document_type TEXT NOT NULL,
    document_label TEXT NOT NULL,
    original_filename TEXT NOT NULL,
    content_type TEXT NOT NULL,
    stored_path TEXT NOT NULL,
    sha256 TEXT NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    byte_size BIGINT NOT NULL CHECK (byte_size BETWEEN 1 AND 10485760),
    issue_date DATE,
    expires_at DATE,
    memo TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'current' CHECK (status IN ('current', 'superseded', 'deleted')),
    uploaded_by TEXT NOT NULL,
    uploaded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_yeoljeong_business_documents_tenant_business
    ON yeoljeong_business_documents (tenant_id, business_id);
-- (사업자, 종류)마다 살아 있는 현행본은 1건뿐이다.
CREATE UNIQUE INDEX IF NOT EXISTS uq_yeoljeong_business_documents_current
    ON yeoljeong_business_documents (tenant_id, business_id, document_type)
    WHERE status = 'current' AND deleted_at IS NULL;

COMMIT;
