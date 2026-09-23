-- 오비서 진아서버 이전 — 인증 DB 부트스트랩
--
-- 배경 (2026-09-23 리허설 실측):
--   인증 7개 테이블을 `pg_dump -t` 로 옮기면 테이블·인덱스·기본값은 따라오지만
--   **함수는 따라오지 않는다.** `app/auth.py::require_saas_schema_ready` 가
--   `public.aads_internal_tenant_id()` 의 존재를 확인하므로, 함수가 없으면
--   복원 직후 모든 인증 엔드포인트가 503 으로 막힌다.
--   리허설 DB `obys_auth_rehearsal`(PostgreSQL 16.15)에서 실제로 재현했다:
--   테이블 7개·행수 전부 일치(saas_users 106 / tenants 106 /
--   tenant_memberships 144 / saas_user_consents 42 / tenant_plan_limits 4),
--   `internal` 테넌트 행도 존재했지만 함수만 false 였다.
--
-- 적용 대상: 진아서버 인증 DB (이전 대상). 멱등하며 데이터를 바꾸지 않는다.
-- 적용 순서: pg_dump 복원 → 이 파일 → 검증 블록 통과 확인.

BEGIN;

-- 1) 내부 테넌트 조회 함수. AADS 원본과 같은 정의다.
CREATE OR REPLACE FUNCTION public.aads_internal_tenant_id()
RETURNS uuid
LANGUAGE sql
STABLE
AS $function$
    SELECT id
      FROM public.tenants
     WHERE slug = 'internal'
       AND deleted_at IS NULL
     LIMIT 1
$function$;

-- 2) 부트스트랩 검증. 하나라도 어긋나면 커밋하지 않는다.
DO $$
DECLARE
    missing text;
BEGIN
    SELECT string_agg(t, ', ')
      INTO missing
      FROM unnest(ARRAY[
            'saas_users', 'saas_user_consents', 'tenants',
            'tenant_memberships', 'tenant_invites',
            'tenant_plan_limits', 'tenant_usage_overrides'
      ]) AS t
     WHERE to_regclass('public.' || t) IS NULL;

    IF missing IS NOT NULL THEN
        RAISE EXCEPTION '인증 테이블 누락: %', missing;
    END IF;

    IF public.aads_internal_tenant_id() IS NULL THEN
        RAISE EXCEPTION
            'internal 테넌트 행이 없다. require_saas_schema_ready 가 503 을 낸다';
    END IF;
END
$$;

COMMIT;
