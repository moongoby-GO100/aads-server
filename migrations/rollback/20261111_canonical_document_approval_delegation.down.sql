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
-- ■ 소유권·의존 가드 (2026-11-16 교차검토 보강)
--   - schema_migrations.note 에 {"precheck":"absent"} 가 없으면 삭제하지 않는다(적용 전부터 있던 동명 객체일 수 있음).
--   - pg_depend 는 deptype n·a·i 전체를 보고, 정방향이 만들지 않은 색인·트리거·제약·뷰가 하나라도 있으면 중단한다.
--

BEGIN;

SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';

-- 스크립트 위치 기준 공통 함수(rollback 디렉터리의 파일은 같은 정본의 심볼릭 링크).
\ir release_object_fingerprint.verify.sql
-- 삭제 전 객체 존재·정의 확인 가드 (기존 객체 보호)
DO $$
DECLARE
    v_table_exists boolean;
    v_col_exists boolean;
    v_recorded jsonb;
    v_current jsonb;
    v_n integer;
    v_tbl regclass;
    v_evt regclass;
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

    -- 대상이 없으면 지문을 입증할 수 없으므로 DROP 문장까지 진행하지 않는다.
    IF NOT v_table_exists AND NOT v_col_exists THEN
        RAISE EXCEPTION 'fingerprint 대상 객체 없음: 삭제 중단(이미 되돌려진 상태 포함)';
    END IF;

    -- 일부만 남은 불일치 상태: EXCEPTION 발생 및 트랜잭션 롤백
    IF v_table_exists <> v_col_exists THEN
        RAISE EXCEPTION '20261111: 되돌림 대상 객체 중 일부만 존재합니다 (표 존재=%, 열 존재=%). 상태가 불일치하므로 되돌림을 중단합니다.', v_table_exists, v_col_exists;
    END IF;

    LOCK TABLE public.project_document_approval_delegations, public.project_document_events IN ACCESS EXCLUSIVE MODE;

    -- 소유권 미입증 또는 지문 불일치는 어떤 DROP 보다 먼저 중단한다.
    SELECT note::jsonb INTO STRICT v_recorded FROM public.schema_migrations
    WHERE filename IN ('migrations/20261111_canonical_document_approval_delegation.sql', '20261111_canonical_document_approval_delegation.sql');
    v_current := pg_temp.release_object_fingerprint('20261111_canonical_document_approval_delegation.sql');
    IF v_recorded->>'precheck' IS DISTINCT FROM 'absent'
       OR v_recorded->>'fingerprint' IS NULL
       OR v_recorded->>'fingerprint' IS DISTINCT FROM v_current->>'fingerprint'
       OR v_recorded->'objects' IS DISTINCT FROM v_current->'objects' THEN
        RAISE EXCEPTION '소유권 미입증: fingerprint missing/mismatch — 삭제 중단';
    END IF;

    -- c) 삭제 대상 표/열에 정방향이 만들지 않은 의존 객체가 없을 것 (CASCADE 금지).
    --    pg_depend 는 deptype 'n'(일반)·'a'(자동: 색인·제약·트리거·기본값)·'i'(내부) 전부, classid·refclassid 를 지정해 본다.
    v_tbl := to_regclass('public.project_document_approval_delegations');
    v_evt := to_regclass('public.project_document_events');

    -- 1) 표를 참조하는 외부 FK
    IF EXISTS (SELECT 1 FROM pg_constraint WHERE confrelid = v_tbl) THEN
        RAISE EXCEPTION '20261111: project_document_approval_delegations 표를 참조하는 외부 외래 키(FK)가 존재합니다. 중단합니다.';
    END IF;

    -- 3) 표를 refobjid 로 하는 pg_depend 중 허용 목록(자기 행 타입·기본값·제약·색인·TOAST) 밖의 객체
    SELECT count(*) INTO v_n FROM pg_depend d
    WHERE d.refclassid = 'pg_class'::regclass AND d.refobjid = v_tbl
      AND d.deptype IN ('n', 'a', 'i')
      AND NOT (
            (d.classid = 'pg_type'::regclass AND d.deptype = 'i'
               AND d.objid = (SELECT reltype FROM pg_class WHERE oid = v_tbl))
         OR d.classid = 'pg_attrdef'::regclass
         OR d.classid = 'pg_constraint'::regclass
         OR (d.classid = 'pg_class'::regclass
               AND EXISTS (SELECT 1 FROM pg_class x WHERE x.oid = d.objid AND x.relkind IN ('i', 't')))
      );
    IF v_n <> 0 THEN
        RAISE EXCEPTION '20261111: project_document_approval_delegations 표에 정방향이 만들지 않은 의존 객체(뷰·정책·확장통계·트리거 등)가 %개 있습니다. 중단합니다.', v_n;
    END IF;

    -- 4) project_document_events.metadata 열에 걸린 모든 의존(색인·트리거·제약·뷰·기본값 등): 정방향은 아무것도 걸지 않는다
    SELECT count(*) INTO v_n FROM pg_depend d
    JOIN pg_attribute att ON att.attrelid = d.refobjid AND att.attnum = d.refobjsubid
    WHERE d.refclassid = 'pg_class'::regclass AND d.refobjid = v_evt
      AND att.attname = 'metadata' AND d.deptype IN ('n', 'a', 'i');
    IF v_n <> 0 THEN
        RAISE EXCEPTION '20261111: project_document_events.metadata 열에 정방향이 만들지 않은 의존 객체(색인·트리거·제약·뷰 등)가 %개 있습니다. 중단합니다.', v_n;
    END IF;
END $$;

-- 1. 위임 표 삭제 (자식 표이므로 의존성 없이 단독 삭제 가능, 연관 부분 색인 lookup_idx 도 함께 자동 삭제됨)
DROP TABLE IF EXISTS public.project_document_approval_delegations;

-- 2. 이벤트 표의 메타데이터 열 삭제 (기존 열과 행은 보존됨)
ALTER TABLE IF EXISTS public.project_document_events DROP COLUMN IF EXISTS metadata;

COMMIT;
