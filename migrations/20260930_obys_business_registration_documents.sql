-- AADS-OBYS-BIZLICENSE-ORIGINAL-OCR-20260930
-- 사업자등록증 원본 보관(버전 이력). 원본 파일은 OBYS_UPLOAD_ROOT 아래
--   <tenant_id>/<sha256(business_id)[:32]>/business_registration/<uuid><ext>
-- 에 있고, 이 표는 그 메타데이터(원본 파일명·크기·확장자·sha256·업로더·시각)다.
--
-- 적용 대상: 오비서 업무 DB(OBYS_DATABASE_URL, 진아서버)만. AADS DB 에 돌리지 않는다.
--   scripts/migrations_auto_apply_baseline.txt 에 올려 자동 적용에서 HOLD 한다.
--   적용: psql "$OBYS_DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/20260930_obys_business_registration_documents.sql
-- 스키마 추가만 한다. 기존 행을 쓰지 않는다. 멱등하다(IF NOT EXISTS).
-- 삭제는 soft delete(deleted_at)만 한다. 이전 버전은 지우지 않는다.
-- 롤백: migrations/rollback/20260930_obys_business_registration_documents.down.sql

BEGIN;

DO $$
BEGIN
    IF to_regclass('public.yeoljeong_businesses') IS NULL
       OR to_regclass('public.yeoljeong_business_tenant_mapping') IS NULL THEN
        RAISE EXCEPTION '오비서 업무 DB 전용이다 (yeoljeong_businesses / yeoljeong_business_tenant_mapping 없음)';
    END IF;
END
$$;

CREATE TABLE IF NOT EXISTS yeoljeong_business_registration_documents (
    id UUID PRIMARY KEY,
    tenant_id UUID NOT NULL,
    business_id TEXT NOT NULL REFERENCES yeoljeong_businesses(id) ON UPDATE CASCADE,
    version INTEGER NOT NULL CHECK (version >= 1),
    original_filename TEXT NOT NULL,
    stored_filename TEXT NOT NULL,
    content_type TEXT NOT NULL,
    extension TEXT NOT NULL CHECK (extension IN ('.pdf','.jpg','.jpeg','.png')),
    byte_size BIGINT NOT NULL CHECK (byte_size BETWEEN 1 AND 10485760),
    sha256 TEXT NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    created_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at TIMESTAMPTZ,
    deleted_by TEXT
);

-- 버전 번호는 삭제된 것까지 포함해 사업자별로 유일하다(재사용하지 않는다).
CREATE UNIQUE INDEX IF NOT EXISTS uq_yeoljeong_business_registration_documents_version
    ON yeoljeong_business_registration_documents (tenant_id, business_id, version);
CREATE INDEX IF NOT EXISTS idx_yeoljeong_business_registration_documents_current
    ON yeoljeong_business_registration_documents (tenant_id, business_id, version DESC)
    WHERE deleted_at IS NULL;

COMMIT;
