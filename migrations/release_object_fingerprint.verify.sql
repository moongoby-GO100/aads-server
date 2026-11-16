-- 공통 세션 전용 함수: 영구 스키마를 변경하지 않는다.
-- search_path를 고정하여 적용/롤백의 PostgreSQL deparser 출력을 동일하게 유지한다.
-- JSON 배열로 경계를 보존하고 원문을 정렬한다. 문자열/리터럴 정규화 금지.
CREATE OR REPLACE FUNCTION pg_temp.release_object_fingerprint(migration text)
RETURNS jsonb LANGUAGE sql STABLE SET search_path = pg_catalog, public AS $fp$
WITH targets(rel, col) AS (
    SELECT 'public.project_document_approval_delegations', NULL::text
    WHERE migration = '20261111_canonical_document_approval_delegation.sql'
    UNION ALL SELECT 'public.project_document_events', 'metadata'
    WHERE migration = '20261111_canonical_document_approval_delegation.sql'
    UNION ALL SELECT 'public.code_review_requests', 'revert_of_verified'
    WHERE migration = '20261112_code_review_requests_revert_of_verified.sql'
    UNION ALL SELECT 'public.pipeline_jobs', 'revert_of_verified'
    WHERE migration = '20261112_code_review_requests_revert_of_verified.sql'
), rels AS (
    SELECT rel, col, to_regclass(rel) AS oid FROM targets
), definitions(obj) AS (
    SELECT jsonb_build_array('table', r.rel, c.relkind, c.relpersistence,
                            c.relrowsecurity, c.relforcerowsecurity, c.reloptions)
    FROM rels r JOIN pg_class c ON c.oid = r.oid WHERE r.col IS NULL
    UNION ALL
    SELECT jsonb_build_array('column', r.rel, a.attname,
        format_type(a.atttypid, a.atttypmod), a.attnotnull,
        pg_get_expr(d.adbin, d.adrelid), a.attidentity, a.attgenerated,
        a.attcollation::regcollation::text)
    FROM rels r JOIN pg_attribute a ON a.attrelid = r.oid
    LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
    WHERE a.attnum > 0 AND NOT a.attisdropped AND (r.col IS NULL OR a.attname = r.col)
    UNION ALL
    SELECT jsonb_build_array('constraint', r.rel, k.conname,
                            pg_get_constraintdef(k.oid, true), k.convalidated)
    FROM rels r JOIN pg_constraint k ON k.conrelid = r.oid
    WHERE r.col IS NULL OR EXISTS (
        SELECT 1 FROM pg_attribute a WHERE a.attrelid = r.oid
        AND a.attname = r.col AND a.attnum = ANY(k.conkey))
    UNION ALL
    SELECT jsonb_build_array('index', r.rel, c.relname, pg_get_indexdef(i.indexrelid), i.indisvalid)
    FROM rels r JOIN pg_index i ON i.indrelid = r.oid
    JOIN pg_class c ON c.oid = i.indexrelid
    WHERE r.col IS NULL OR EXISTS (
        SELECT 1 FROM pg_attribute a WHERE a.attrelid = r.oid
        AND a.attname = r.col AND a.attnum = ANY(i.indkey::int2[]))
    UNION ALL
    SELECT jsonb_build_array('trigger', t.tgrelid::regclass::text, t.tgname, pg_get_triggerdef(t.oid))
    FROM rels r JOIN pg_trigger t ON t.tgrelid = r.oid OR (r.col IS NULL AND t.tgconstraint IN (
        SELECT k.oid FROM pg_constraint k WHERE k.conrelid = r.oid))
    WHERE r.col IS NULL OR EXISTS (
        SELECT 1 FROM pg_attribute a WHERE a.attrelid = r.oid
        AND a.attname = r.col AND a.attnum = ANY(t.tgattr::int2[]))
), snapshot AS (
    SELECT coalesce(jsonb_agg(obj ORDER BY obj::text COLLATE "C"), '[]'::jsonb) AS objects
    FROM definitions
)
SELECT jsonb_build_object('fingerprint', md5(objects::text), 'objects', objects) FROM snapshot;
$fp$;
