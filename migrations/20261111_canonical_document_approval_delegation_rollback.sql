\set ON_ERROR_STOP on
-- 주의: 이 스크립트는 내부 트랜잭션(BEGIN ... COMMIT)을 포함하므로 psql --single-transaction 옵션과 함께 실행하지 마십시오.
-- 실행 예시: psql -v ON_ERROR_STOP=1 -X -f migrations/rollback/20261111_canonical_document_approval_delegation.down.sql
-- (또는: psql -v ON_ERROR_STOP=1 -X -f migrations/20261111_canonical_document_approval_delegation_rollback.sql)
--
-- 되돌림 스크립트 (수동 실행용)
-- 대응 마이그레이션: migrations/20261111_canonical_document_approval_delegation.sql
-- 정방향 파일의 서술 중 백업 및 수동 되돌림 관련 설명(40~46행 등)은 이 파일의 설명으로 대체한다.
--
-- ■ 되돌림 범위 및 삭제 객체
--   - 표: public.project_document_approval_delegations (연관 부분 색인 lookup_idx 포함 자동 삭제)
--   - 열: public.project_document_events.metadata
--   - 기존 객체(public.tenants, public.project_document_events 의 기존 열 및 기존 행 데이터)는 전혀 삭제하거나 변경하지 않습니다.
--
-- ■ 선행 조건 및 1차 롤백 구분
--   - 1차 롤백: API 컨테이너를 구 버전으로 되돌릴 때 잔존 표는 미참조 상태이며 metadata 열은 기본값 없는 nullable 이라
--     운영에 무해하므로 down 실행이 불필요합니다 (API 롤백만으로 서비스 정상 복구).
--   - 2차 롤백(스키마 완전 제거): 신규 기능을 영구 철회하고 스키마를 이전 상태로 완전히 되돌려야 할 때만
--     CEO 승인 후 운영 세션에서 아래 백업 확인을 거쳐 이 스크립트를 수동 실행합니다.
--
-- ■ 데이터 백업 권장 절차 (실행 전 필수)
--   배포 후 운영 중에 생성된 위임 레코드나 이벤트 메타데이터가 삭제되므로 실행 전 백업을 권장합니다:
--     1) pg_dump -U aads -d aads -t project_document_approval_delegations -f /root/aads/backups/delegations_before_rollback.sql
--     2) psql 클라이언트 측 \copy 사용 (호스트 경로 /root/aads/backups/... 에 저장):
--        \copy (SELECT id, head_id, revision_id, action, metadata FROM public.project_document_events WHERE metadata IS NOT NULL) TO '/root/aads/backups/events_metadata_before_rollback.csv' WITH CSV HEADER
--   백업 파일 존재 및 행 수 확인 후 down 을 실행하십시오 (예: wc -l /root/aads/backups/*_before_rollback.*).
--
-- ■ 의존 순서: 자식 표(project_document_approval_delegations) DROP -> 부모 표 열(metadata) DROP. IF EXISTS 사용.
--

BEGIN;

SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';

-- 삭제 전 객체 존재·정의 확인 가드 (기존 객체 보호)
DO $$
DECLARE
    v_table_exists boolean;
    v_col_exists boolean;
    v_col_count integer;
    v_meta_match integer;
BEGIN
    -- 0. 대상 객체 존재 여부 확인 (public 스키마 한정)
    SELECT EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = 'public' AND table_name = 'project_document_approval_delegations'
    ) INTO v_table_exists;

    SELECT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'project_document_events' AND column_name = 'metadata'
    ) INTO v_col_exists;

    -- 이미 되돌려진 상태(둘 다 없음): NOTICE 후 정상 종료 (멱등성 보장)
    IF NOT v_table_exists AND NOT v_col_exists THEN
        RAISE NOTICE '20261111: 되돌릴 대상 객체(project_document_approval_delegations 표 및 project_document_events.metadata 열)가 존재하지 않습니다. 이미 되돌려진 상태입니다.';
        RETURN;
    END IF;

    -- 일부만 남은 불일치 상태: EXCEPTION 발생 및 트랜잭션 롤백
    IF v_table_exists <> v_col_exists THEN
        RAISE EXCEPTION '20261111: 되돌림 대상 객체 중 일부만 존재합니다 (표 존재=%, 열 존재=%). 상태가 불일치하므로 되돌림을 중단합니다.', v_table_exists, v_col_exists;
    END IF;

    -- a) schema_migrations 에 해당 마이그레이션 파일 기록이 있는가
    IF NOT EXISTS (
        SELECT 1 FROM public.schema_migrations
        WHERE filename IN (
            'migrations/20261111_canonical_document_approval_delegation.sql',
            '20261111_canonical_document_approval_delegation.sql'
        )
    ) THEN
        RAISE EXCEPTION '20261111: schema_migrations 에 해당 마이그레이션 적용 기록이 없습니다. 이 마이그레이션이 적용된 적 없음 — 같은 이름의 기존 객체일 수 있어 중단합니다.';
    END IF;

    -- b) 대상 객체가 정방향 정의와 같은가
    -- project_document_approval_delegations 표 열 수(10개) 확인
    SELECT count(*) INTO v_col_count
    FROM information_schema.columns
    WHERE table_schema = 'public' AND table_name = 'project_document_approval_delegations';

    IF v_col_count <> 10 THEN
        RAISE EXCEPTION '20261111: project_document_approval_delegations 표의 열 구성이 정방향 정의(10개)와 다릅니다 (실제: %개).', v_col_count;
    END IF;

    -- project_document_events.metadata 타입(jsonb), nullable, 기본값 없음 확인
    SELECT count(*) INTO v_meta_match
    FROM information_schema.columns
    WHERE table_schema = 'public'
      AND table_name = 'project_document_events'
      AND column_name = 'metadata'
      AND data_type = 'jsonb'
      AND is_nullable = 'YES'
      AND column_default IS NULL;

    IF v_meta_match <> 1 THEN
        RAISE EXCEPTION '20261111: project_document_events.metadata 열의 정의가 정방향(jsonb, nullable, no default)과 일치하지 않습니다.';
    END IF;

    -- c) 삭제 대상 표/열에 정방향이 만들지 않은 의존 객체(뷰·FK·트리거 등, pg_depend 기준)가 없을 것 (CASCADE 금지)
    -- 1) 외부 테이블에서 project_document_approval_delegations 를 참조하는 FK 검사
    IF EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE confrelid = to_regclass('public.project_document_approval_delegations')
    ) THEN
        RAISE EXCEPTION '20261111: project_document_approval_delegations 표를 참조하는 외부 외래 키(FK)가 존재합니다. 연쇄 삭제 금지 원칙에 따라 중단합니다.';
    END IF;

    -- 2) 외부 뷰 또는 다른 객체가 project_document_approval_delegations 표에 의존하는지 검사
    IF EXISTS (
        SELECT 1 FROM pg_depend dep
        WHERE dep.refobjid = to_regclass('public.project_document_approval_delegations')
          AND dep.deptype = 'n'
          AND (to_regclass('public.project_document_approval_delegations_lookup_idx') IS NULL
               OR dep.objid <> to_regclass('public.project_document_approval_delegations_lookup_idx'))
    ) THEN
        RAISE EXCEPTION '20261111: project_document_approval_delegations 표에 정방향이 만들지 않은 의존 객체(뷰 등)가 존재합니다. 중단합니다.';
    END IF;

    -- 3) project_document_events.metadata 열을 참조하는 의존 객체(뷰, 색인 등) 검사
    IF EXISTS (
        SELECT 1 FROM pg_depend dep
        JOIN pg_attribute att ON att.attrelid = dep.refobjid AND att.attnum = dep.refobjsubid
        WHERE dep.refobjid = to_regclass('public.project_document_events')
          AND att.attname = 'metadata'
          AND dep.deptype = 'n'
    ) THEN
        RAISE EXCEPTION '20261111: project_document_events.metadata 열에 정방향이 만들지 않은 의존 객체(뷰, 색인 등)가 존재합니다. 중단합니다.';
    END IF;
END $$;

-- 1. 위임 표 삭제 (자식 표이므로 의존성 없이 단독 삭제 가능, 연관 부분 색인 lookup_idx 도 함께 자동 삭제됨)
DROP TABLE IF EXISTS public.project_document_approval_delegations;

-- 2. 이벤트 표의 메타데이터 열 삭제 (기존 열과 행은 보존됨)
ALTER TABLE IF EXISTS public.project_document_events DROP COLUMN IF EXISTS metadata;

COMMIT;
