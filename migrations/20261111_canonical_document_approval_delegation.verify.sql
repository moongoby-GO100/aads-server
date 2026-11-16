-- 릴리스 마이그레이션 객체 정의 정밀 검증 (읽기 전용: SELECT 와 RAISE 만 사용)
-- 대상: migrations/20261111_canonical_document_approval_delegation.sql
--
-- 실행 위치: scripts/apply_migration.sh 가 정방향 SQL 과 schema_migrations 기록 직후, COMMIT 직전에
--   같은 트랜잭션에서 실행한다. 하나라도 어긋나면 RAISE EXCEPTION → ON_ERROR_STOP 으로 COMMIT 에 도달하지
--   못해 마이그레이션과 적용 기록이 함께 ROLLBACK 된다. (이 파일이 없으면 적용기는 경고만 내고 기존 동작 유지.)
-- 검증 항목: (CHECK 는 원문 전체 일치) 표 열 구성(data_type·is_nullable·column_default·열 순서 무관 전체 일치), PK·FK·CHECK 제약,
--   색인(schemaname='public'·대상 표·열 순서·부분 조건·indisvalid), 기존 표에 추가된 metadata 열.

SET LOCAL search_path = pg_catalog, public;

DO $$
DECLARE
    v_rel regclass := to_regclass('public.project_document_approval_delegations');
    v_missing text;
    v_extra text;
    v_cnt integer;
    v_expected text[] := ARRAY[
        'id|uuid|NO|gen_random_uuid()',
        'tenant_id|uuid|NO|',
        'project_key|text|NO|',
        'goal_id|uuid|YES|',
        'grantee|text|NO|',
        'granted_by|text|NO|',
        'expires_at|timestamp with time zone|NO|',
        'revoked_at|timestamp with time zone|YES|',
        'revoked_by|text|YES|',
        'created_at|timestamp with time zone|NO|now()'];
BEGIN
    IF v_rel IS NULL THEN
        RAISE EXCEPTION 'VERIFY_FAIL 20261111: public.project_document_approval_delegations 표가 없습니다';
    END IF;

    -- 1. 열 구성: 기대 집합과 실제 집합이 양방향으로 정확히 일치해야 한다.
    -- 형식: 열|data_type|is_nullable|column_default(없으면 빈 문자열)
    SELECT string_agg(e, ', ') INTO v_missing
    FROM unnest(v_expected) AS e
    WHERE NOT EXISTS (
        SELECT 1 FROM information_schema.columns c
        WHERE c.table_schema = 'public' AND c.table_name = 'project_document_approval_delegations'
          AND c.column_name || '|' || c.data_type || '|' || c.is_nullable || '|' || coalesce(c.column_default, '') = e);
    IF v_missing IS NOT NULL THEN
        RAISE EXCEPTION 'VERIFY_FAIL 20261111: 열 정의(타입·NULL 여부·기본값) 불일치 또는 누락: %', v_missing;
    END IF;

    SELECT string_agg(c.column_name, ', ') INTO v_extra
    FROM information_schema.columns c
    WHERE c.table_schema = 'public' AND c.table_name = 'project_document_approval_delegations'
      AND c.column_name || '|' || c.data_type || '|' || c.is_nullable || '|' || coalesce(c.column_default, '') <> ALL (v_expected);
    IF v_extra IS NOT NULL THEN
        RAISE EXCEPTION 'VERIFY_FAIL 20261111: 정방향이 만들지 않은 열(또는 정의가 다른 열)이 있습니다: %', v_extra;
    END IF;

    -- PostgreSQL 15 ruleutils.c: pg_get_constraintdef(oid, true)는 CHECK 외곽만
    -- 괄호로 감싸고 text 리터럴에는 ::text를 출력한다. pg_get_indexdef 기본값은
    -- pretty=false여서 부분 조건 괄호를 유지한다. search_path=pg_catalog,public 기준.
    -- 리터럴 내부 공백까지 의미가 있으므로 원문 그대로 양방향 집합 비교한다.
    IF (SELECT count(*) FROM pg_constraint WHERE conrelid = v_rel) <> 5
       OR (SELECT count(DISTINCT pg_get_constraintdef(k.oid, true))
           FROM pg_constraint k WHERE k.conrelid = v_rel AND k.convalidated
           AND pg_get_constraintdef(k.oid, true) IN (
             'PRIMARY KEY (id)',
             'FOREIGN KEY (tenant_id) REFERENCES tenants(id)',
             'CHECK (project_key ~ ''^[A-Z0-9][A-Z0-9_-]{0,63}$''::text)',
             'CHECK (length(grantee) >= 1 AND length(grantee) <= 128)',
             'CHECK (length(granted_by) > 0)')) <> 5 THEN
        RAISE EXCEPTION 'VERIFY_FAIL 20261111: 제약 원문/검증 상태 불일치';
    END IF;

    IF (SELECT count(*) FROM pg_index WHERE indrelid = v_rel) <> 2
       OR (SELECT count(DISTINCT pg_get_indexdef(i.indexrelid))
           FROM pg_index i WHERE i.indrelid = v_rel AND i.indisvalid
           AND pg_get_indexdef(i.indexrelid) IN (
             'CREATE UNIQUE INDEX project_document_approval_delegations_pkey ON public.project_document_approval_delegations USING btree (id)',
             'CREATE INDEX project_document_approval_delegations_lookup_idx ON public.project_document_approval_delegations USING btree (tenant_id, project_key, grantee) WHERE (revoked_at IS NULL)')) <> 2 THEN
        RAISE EXCEPTION 'VERIFY_FAIL 20261111: 색인 원문/유효성 불일치';
    END IF;

    -- 4. 기존 표에 추가된 열: jsonb, nullable, 기본값 없음.
    SELECT count(*) INTO v_cnt FROM information_schema.columns
    WHERE table_schema = 'public' AND table_name = 'project_document_events' AND column_name = 'metadata'
      AND data_type = 'jsonb' AND is_nullable = 'YES' AND column_default IS NULL;
    IF v_cnt <> 1 THEN
        RAISE EXCEPTION 'VERIFY_FAIL 20261111: public.project_document_events.metadata 가 jsonb·nullable·기본값 없음이 아닙니다';
    END IF;
END $$;

SELECT 'VERIFY_OK' AS verification_status;
