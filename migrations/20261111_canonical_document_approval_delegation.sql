-- 정본 문서 승인 위임: 사람이 부여하고 담당 채팅 세션이 교차검수 통과 문서를 승인한다.
-- 사람 승인 경로·자동 승인 기준은 바꾸지 않는다. 추가만 하는 변경(새 표·부분 색인·기존 표에 기본값 없는 nullable 열)이라 릴리스 자동 적용 대상.
-- 멱등: 두 번 실행해도 오류가 없다(IF NOT EXISTS).
-- 되돌림: migrations/rollback/20261111_canonical_document_approval_delegation.down.sql 및
--         migrations/20261111_canonical_document_approval_delegation_rollback.sql
--         (수동 실행용: DROP TABLE IF EXISTS, ALTER TABLE ... DROP COLUMN IF EXISTS).
--
-- ■ 이 파일이 만드는 객체 (전부 추가형, 멱등):
--   1) 표: project_document_approval_delegations
--   2) 부분 색인: project_document_approval_delegations_lookup_idx (WHERE revoked_at IS NULL)
--   3) 열: project_document_events.metadata (jsonb, nullable, no default)
--
-- ■ 1차 롤백(API 이전 버전 복귀) 시 잔존 객체의 무해성과 외래 키(tenants) 영향:
--   1) 새 표 참조: 구 버전 코드는 project_document_approval_delegations 표를 전혀 참조하지 않는다.
--      새 표를 사용하는 곳은 신규 서비스(app/services/canonical_document_delegation.py) 뿐이므로,
--      API 를 이전 판으로 되돌려도 잔존 표는 어떤 경로에서도 조회되거나 실행되지 않고 데이터만 남는다.
--   2) 새 열 참조: project_document_events 에 추가되는 metadata 열은 기본값 없는 nullable(NULL 허용) 열이다.
--      구 버전 코드가 실행하는 INSERT INTO project_document_events(tenant_id, project_key, head_id, revision_id, action, actor_id)
--      문장은 metadata 열을 지정하지 않으므로 자동으로 NULL 이 채워져 정상 실행된다.
--      기존 SELECT 쿼리(action='approved' 등) 역시 특정 열만 조회하므로 미참조 열이 결과나 집계에 영향을 주지 않는다.
--   3) 외래 키 tenant_id REFERENCES tenants(id) 의 ON DELETE 동작과 잔존 영향:
--      외래키 제약조건은 PostgreSQL 기본 동작(ON DELETE RESTRICT / NO ACTION)을 따른다.
--      새 버전 운영 중 위임 데이터가 생성된 후 API 만 1차 롤백(이전 판 복귀)했을 때,
--      구 버전 코드가 tenants 의 해당 행을 DELETE 하거나 tenant_id 키를 UPDATE 하려고 하면 외래키 제약조건 오류가 발생할 수 있다.
--      그러나 AADS 시스템에서 tenants 는 최상위 테넌트 식별자로서 불변이며, 구 버전 API·배경작업·스크립트 전체에
--      tenants 행을 삭제(DELETE)하거나 ID 를 변경하는 경로는 일절 존재하지 않는다(영구 보존 엔티티).
--      따라서 실 운영상 외래키 충돌은 발생하지 않는다.
--      만약 롤백 상태에서 특정 테넌트 삭제가 필요한 예외 상황이 발생할 경우,
--      수동으로 잔존 위임 행을 먼저 정리(DELETE FROM project_document_approval_delegations WHERE tenant_id = :tenant_id)한다.
--      책임 주체: 운영 담당자 및 CEO 승인.
--
-- ■ 잠금 및 실행 시간 제한:
--   SET LOCAL lock_timeout = '5s';
--   SET LOCAL statement_timeout = '60s';
--   새 빈 표와 부분 색인 생성은 신규 객체에 대한 것이므로 잠금 경합이 없고 즉시 완료된다.
--   project_document_events 표에 metadata 열을 추가하는 ALTER TABLE 문은 기본값 없는 nullable 열 추가이므로
--   표 전체 재작성(rewrite) 없이 시스템 카탈로그 갱신만 수행하여 매우 짧은 ACCESS EXCLUSIVE 잠금만 소요된다.
--   5초 내 잠금을 얻지 못하면 문장이 취소되며 트랜잭션이 즉시 롤백되어 운영 트랜잭션을 지연시키지 않는다.
--
-- ■ 수동 되돌림(down SQL)의 범위와 데이터 백업 책임:
--   되돌림 스크립트는 이번 마이그레이션이 생성한 project_document_approval_delegations 표(및 색인)와
--   project_document_events.metadata 열만 정확히 제거하며, 기존 데이터(tenants 및 project_document_events 기존 열)는 보존한다.
--   수동 되돌림 실행 시 신규 기능 운영 중 생성된 위임 행과 이벤트 메타데이터가 영구 삭제되므로,
--   되돌림 실행 전 반드시 pg_dump -t project_document_approval_delegations 백업을 수행해야 한다(CEO 승인 사항).
--   의존 순서: 자식 표(project_document_approval_delegations) DROP -> 부모 표 열(metadata) DROP.
--
-- ■ 실패 시 복구 책임과 절차:
--   적용기(apply_migration.sh)와 자동 검증기(release_migration_autoverify.py)는 단일 트랜잭션 안에서 이 파일을 실행한다.
--   적용 중 오류 발생 시 즉시 ROLLBACK 되므로 부분 적용 상태가 남지 않는다.
--   내부 BEGIN/COMMIT 은 두지 않는다(적용기 트랜잭션과의 원자성 유지).
--
-- ■ postcheck 실패 시 복구 절차:
--   1) COMMIT 전 실패(적용 SQL 오류 또는 <이름>.verify.sql 의 RAISE EXCEPTION):
--      마이그레이션과 schema_migrations 적용 기록이 함께 자동 ROLLBACK 된다. 수동 조치 없음.
--      재시도는 원인(정방향 SQL 또는 verify.sql)을 수정한 뒤 다음 릴리스에서만 한다.
--   2) COMMIT 후 실패(적용기의 postcheck 단계, exit=5):
--      API 전환을 하지 않는다(릴리스 중단). 잔존 스키마는 nullable·미참조라 그대로 둔다.
--      적용 기록은 유지되며 지우지 않는다. down SQL 은 CEO 승인 후 운영 세션에서만 실행한다.
--      담당은 CTO 세션이다. 재시도 조건: 원인 수정 후 다음 릴리스(파일 sha256 이 바뀌면 DRIFT 로 재적용되지 않으므로
--      새 마이그레이션 파일로 보정한다).
--   3) down 은 적용 기록의 note 에 {"precheck":"absent"} 가 있을 때만 객체를 지운다(기존 동명 객체 보호).
--
-- ■ 객체 정의 검증:
--   적용 후 migrations/20261111_canonical_document_approval_delegation.verify.sql 을 실행하여
--   표의 열 구성(타입·NULL여부), PK, FK, CHECK 제약조건, 부분 색인 WHERE 조건, metadata 열 타입을 검증한다.
--
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';

CREATE TABLE IF NOT EXISTS project_document_approval_delegations (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    project_key text NOT NULL CHECK (project_key ~ '^[A-Z0-9][A-Z0-9_-]{0,63}$'),
    goal_id uuid,
    grantee text NOT NULL CHECK (length(grantee) BETWEEN 1 AND 128),
    granted_by text NOT NULL CHECK (length(granted_by) > 0),
    expires_at timestamptz NOT NULL,
    revoked_at timestamptz,
    revoked_by text,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS project_document_approval_delegations_lookup_idx
    ON project_document_approval_delegations(tenant_id, project_key, grantee)
    WHERE revoked_at IS NULL;

-- 감사 메타데이터. action='approved' 는 그대로 쓰므로 기존 승인 집계 쿼리는 영향이 없다.
ALTER TABLE project_document_events ADD COLUMN IF NOT EXISTS metadata jsonb;
