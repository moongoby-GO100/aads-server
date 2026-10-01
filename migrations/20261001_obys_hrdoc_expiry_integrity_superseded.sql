-- AADS-OBYS-O2-3-HRDOC-BACKEND-20261001
-- 직원 입사서류(yeoljeong_onboarding_documents) 에 만료일·무결성·대체 이력 컬럼을 추가한다.
--
-- 적용 대상: 오비서 업무 DB(OBYS_DATABASE_URL)만. AADS DB 에 돌리지 않는다.
--   scripts/migrations_auto_apply_baseline.txt 에 올려 자동 적용에서 HOLD 한다.
--   적용: psql "$OBYS_DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/20261001_obys_hrdoc_expiry_integrity_superseded.sql
--   contabo116 사본: docker exec -i aads-postgres psql -U aads -d obys -v ON_ERROR_STOP=1 < 이 파일
--   진아서버 운영 반영은 별도 승인 릴리스에서 한다.
--
-- 컬럼 추가만 한다. 기존 행은 건드리지 않는다(백필 없음 — 기존 서류는 NULL/'' 로 남는다).
--   expires_at     DATE NULL                 서류 만료일
--   sha256         TEXT NOT NULL DEFAULT ''  업로드 시점 원본 해시(다운로드 때 대조). 옛 행은 '' = 대조 안 함
--   stored_path    TEXT NOT NULL DEFAULT ''  저장 위치(업로드 디렉터리 기준 상대경로)
--   superseded_by  TEXT NOT NULL DEFAULT ''  재제출로 이 행을 대체한 새 행 id
-- 멱등하다(ADD COLUMN IF NOT EXISTS).
-- 이 마이그레이션이 새 코드보다 먼저 적용돼야 한다. 새 코드의 INSERT/UPSERT 가 이 컬럼들을 쓴다.
-- 롤백: migrations/rollback/20261001_obys_hrdoc_expiry_integrity_superseded.down.sql

BEGIN;

DO $$
BEGIN
    IF to_regclass('public.yeoljeong_onboarding_documents') IS NULL THEN
        RAISE EXCEPTION '오비서 업무 DB 전용이다 (yeoljeong_onboarding_documents 없음)';
    END IF;
END
$$;

ALTER TABLE yeoljeong_onboarding_documents ADD COLUMN IF NOT EXISTS expires_at DATE NULL;
ALTER TABLE yeoljeong_onboarding_documents ADD COLUMN IF NOT EXISTS sha256 TEXT NOT NULL DEFAULT '';
ALTER TABLE yeoljeong_onboarding_documents ADD COLUMN IF NOT EXISTS stored_path TEXT NOT NULL DEFAULT '';
ALTER TABLE yeoljeong_onboarding_documents ADD COLUMN IF NOT EXISTS superseded_by TEXT NOT NULL DEFAULT '';

COMMIT;
