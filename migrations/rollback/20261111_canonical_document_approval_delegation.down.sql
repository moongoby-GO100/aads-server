-- 되돌림 스크립트 (수동 실행용)
-- 대응 마이그레이션: migrations/20261111_canonical_document_approval_delegation.sql
--
-- ■ 되돌림 범위 및 삭제 객체
--   - 표: project_document_approval_delegations (연관 부분 색인 lookup_idx 포함 자동 삭제)
--   - 열: project_document_events.metadata
--   - 기존 객체(tenants, project_document_events 의 기존 열 및 기존 행 데이터)는 전혀 삭제하거나 변경하지 않습니다.
--
-- ■ 데이터 백업 권장 절차 (실행 전 필수)
--   배포 후 운영 중에 생성된 위임 레코드나 이벤트 메타데이터가 삭제되므로 실행 전 백업을 권장합니다:
--     1) pg_dump -U aads -d aads -t project_document_approval_delegations -f /root/aads/backups/delegations_before_rollback.sql
--     2) COPY (SELECT id, head_id, revision_id, action, metadata FROM project_document_events WHERE metadata IS NOT NULL)
--        TO '/root/aads/backups/events_metadata_before_rollback.csv' WITH CSV HEADER;
--
-- ■ 선행 조건: API 컨테이너를 구 버전으로 먼저 롤백한 후 실행합니다 (CEO 승인 사항).
-- ■ 의존 순서: 자식 표(project_document_approval_delegations) DROP -> 부모 표 열(metadata) DROP. IF EXISTS 사용.
--
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';

-- 1. 위임 표 삭제 (자식 표이므로 의존성 없이 단독 삭제 가능, 연관 색인도 함께 삭제됨)
DROP TABLE IF EXISTS project_document_approval_delegations;

-- 2. 이벤트 표의 메타데이터 열 삭제 (기존 열과 행은 보존됨)
ALTER TABLE IF EXISTS project_document_events DROP COLUMN IF EXISTS metadata;
