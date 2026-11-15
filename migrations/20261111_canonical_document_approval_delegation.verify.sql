-- 릴리스 마이그레이션 객체 정의 정밀 검증 (읽기 전용 SELECT)
-- 대상: migrations/20261111_canonical_document_approval_delegation.sql
-- 검증 항목: 표·열 정의(타입·NULL여부·기본값), 제약조건(PK·FK·CHECK), 색인 정의(부분조건 포함)

-- 1. 신규 표 project_document_approval_delegations 의 열 구성 검증
SELECT table_name, column_name, data_type, is_nullable, column_default
FROM information_schema.columns
WHERE table_schema = 'public' AND table_name = 'project_document_approval_delegations'
ORDER BY ordinal_position;

-- 2. 신규 표 project_document_approval_delegations 의 제약조건 (PK, FK, CHECK) 검증
SELECT conname AS constraint_name,
       contype AS constraint_type,
       pg_get_constraintdef(oid) AS constraint_definition
FROM pg_constraint
WHERE conrelid = 'public.project_document_approval_delegations'::regclass
ORDER BY conname;

-- 3. 신규 부분 색인 project_document_approval_delegations_lookup_idx 정의 검증
SELECT indexname, indexdef
FROM pg_indexes
WHERE schemaname = 'public'
  AND tablename = 'project_document_approval_delegations'
  AND indexname = 'project_document_approval_delegations_lookup_idx';

-- 4. 기존 표 project_document_events 에 추가된 metadata 열 검증
SELECT table_name, column_name, data_type, is_nullable, column_default
FROM information_schema.columns
WHERE table_schema = 'public'
  AND table_name = 'project_document_events'
  AND column_name = 'metadata';

-- 5. 정량적 일치 판정 (모든 기대 객체·정의가 일치하면 'VERIFY_OK' 반환)
SELECT
    CASE WHEN
        -- 열 10개 존재 확인
        (SELECT count(*) FROM information_schema.columns
         WHERE table_schema = 'public' AND table_name = 'project_document_approval_delegations') = 10
        -- PK 제약조건 확인
        AND (SELECT count(*) FROM pg_constraint
             WHERE conrelid = 'public.project_document_approval_delegations'::regclass AND contype = 'p') = 1
        -- tenants(id) FK 제약조건 확인
        AND (SELECT count(*) FROM pg_constraint
             WHERE conrelid = 'public.project_document_approval_delegations'::regclass AND contype = 'f'
               AND confrelid = 'public.tenants'::regclass) = 1
        -- CHECK 제약조건 3개 (project_key, grantee, granted_by) 확인
        AND (SELECT count(*) FROM pg_constraint
             WHERE conrelid = 'public.project_document_approval_delegations'::regclass AND contype = 'c') >= 3
        -- 부분 색인 (WHERE revoked_at IS NULL) 확인
        AND (SELECT count(*) FROM pg_indexes
             WHERE tablename = 'project_document_approval_delegations'
               AND indexname = 'project_document_approval_delegations_lookup_idx'
               AND indexdef LIKE '%WHERE (revoked_at IS NULL)%') = 1
        -- metadata jsonb 열 확인
        AND (SELECT count(*) FROM information_schema.columns
             WHERE table_schema = 'public' AND table_name = 'project_document_events'
               AND column_name = 'metadata' AND data_type = 'jsonb') = 1
    THEN 'VERIFY_OK'
    ELSE 'VERIFY_FAIL'
    END AS verification_status;
