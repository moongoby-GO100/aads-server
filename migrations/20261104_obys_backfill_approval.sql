-- 오비서(OBYS DB) 백필 승인 기록. 롤백: migrations/rollback/20261104_obys_backfill_approval.down.sql
-- 적용 대상은 OBYS DB 뿐이다(AADS DB 아님). 멱등: 여러 번 실행해도 같은 결과.
-- 백필(과거 구간 일괄 재수집)은 여기 승인된 회사·종류·기간 안에서만 실행된다. 정기 증분 수집은 이 표를 보지 않는다.
BEGIN;
DO $$ BEGIN
    IF current_database() <> 'obys' THEN
        RAISE EXCEPTION 'OBYS business database required';
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS public.obys_backfill_approval (
    approval_id  UUID PRIMARY KEY,
    source       TEXT NOT NULL CHECK (source IN ('clobe', 'delivery')),
    -- clobe: clobe_company_id / delivery: business_id
    scope_id     TEXT NOT NULL CHECK (length(trim(scope_id)) > 0),
    -- delivery 매장명. 빈 값이면 그 사업자의 모든 매장. clobe 는 항상 빈 값.
    branch       TEXT NOT NULL DEFAULT '',
    -- clobe: 수집 종류 / delivery: 플랫폼(service)
    data_kind    TEXT NOT NULL CHECK (length(trim(data_kind)) > 0),
    period_start DATE NOT NULL,
    period_end   DATE NOT NULL,
    approved_by  TEXT NOT NULL CHECK (length(trim(approved_by)) > 0),
    approved_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    reason       TEXT NOT NULL CHECK (length(trim(reason)) >= 5),
    revoked_at   TIMESTAMPTZ,
    revoked_by   TEXT,
    CONSTRAINT obys_backfill_approval_period CHECK (period_start <= period_end)
);
CREATE INDEX IF NOT EXISTS obys_backfill_approval_lookup_idx
    ON public.obys_backfill_approval (source, scope_id, data_kind) WHERE revoked_at IS NULL;

-- 승인·철회 감사 기록. 승인 행은 지우지 않고 철회 표시만 하며, 모든 변경이 여기에도 남는다.
CREATE TABLE IF NOT EXISTS public.obys_backfill_approval_audit (
    audit_id    BIGSERIAL PRIMARY KEY,
    approval_id UUID NOT NULL,
    action      TEXT NOT NULL CHECK (action IN ('approve', 'revoke')),
    actor       TEXT NOT NULL,
    detail      JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS obys_backfill_approval_audit_idx
    ON public.obys_backfill_approval_audit (approval_id, created_at);
COMMIT;
