-- 릴리스 마이그레이션 객체 정의 정밀 검증 (읽기 전용: SELECT 와 RAISE 만 사용)
-- 대상: migrations/20261112_code_review_requests_revert_of_verified.sql
--
-- 실행 위치: scripts/apply_migration.sh 가 정방향 SQL 과 schema_migrations 기록 직후, COMMIT 직전에
--   같은 트랜잭션에서 실행한다. 어긋나면 RAISE EXCEPTION → COMMIT 에 도달하지 못해 마이그레이션과
--   적용 기록이 함께 ROLLBACK 된다.
-- 검증 항목: 두 표의 revert_of_verified 열 data_type=jsonb·is_nullable=YES·column_default 없음,
--   그리고 열에 붙은 색인·제약이 없을 것(정방향은 열만 추가한다).

DO $$
DECLARE
    v_tbl text;
    v_cnt integer;
BEGIN
    FOREACH v_tbl IN ARRAY ARRAY['code_review_requests', 'pipeline_jobs'] LOOP
        SELECT count(*) INTO v_cnt FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = v_tbl AND column_name = 'revert_of_verified'
          AND data_type = 'jsonb' AND is_nullable = 'YES' AND column_default IS NULL;
        IF v_cnt <> 1 THEN
            RAISE EXCEPTION 'VERIFY_FAIL 20261112: public.%.revert_of_verified 가 jsonb·nullable·기본값 없음이 아닙니다', v_tbl;
        END IF;

        -- 정방향은 색인·제약·트리거를 만들지 않는다: 이 열에 걸린 것이 있으면 정의가 다르다.
        SELECT count(*) INTO v_cnt FROM pg_index i
        JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY (i.indkey::int2[])
        WHERE i.indrelid = to_regclass('public.' || v_tbl) AND a.attname = 'revert_of_verified';
        IF v_cnt <> 0 THEN
            RAISE EXCEPTION 'VERIFY_FAIL 20261112: public.%.revert_of_verified 에 색인이 걸려 있습니다', v_tbl;
        END IF;

        SELECT count(*) INTO v_cnt FROM pg_constraint k
        JOIN pg_attribute a ON a.attrelid = k.conrelid AND a.attnum = ANY (k.conkey)
        WHERE k.conrelid = to_regclass('public.' || v_tbl) AND a.attname = 'revert_of_verified';
        IF v_cnt <> 0 THEN
            RAISE EXCEPTION 'VERIFY_FAIL 20261112: public.%.revert_of_verified 에 제약이 걸려 있습니다', v_tbl;
        END IF;
    END LOOP;
END $$;

SELECT 'VERIFY_OK' AS verification_status;
