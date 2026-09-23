-- Down migration for migrations/20260923_project_document_canonical.sql
-- AADS-PROJECT-DOCUMENTS-M1 / goal ac431290-66be-4d00-8bbb-9bf162917240
--
-- 이 스크립트는 up 이 만든 것만 되돌린다. 기존 정본(goal_documents,
-- project_artifacts, chat_artifacts)의 행은 건드리지 않는다.
-- pgcrypto 확장은 다른 스키마가 공유하므로 DROP 하지 않는다.
--
-- 적용: psql -v ON_ERROR_STOP=1 -f migrations/rollback/20260923_project_document_canonical.down.sql

-- 1) append-only 트리거 (테이블과 함께 사라지지만 부분 적용 상태를 위해 명시한다)
DROP TRIGGER IF EXISTS project_document_legacy_links_immutable ON project_document_legacy_links;
DROP TRIGGER IF EXISTS project_document_events_immutable ON project_document_events;
DROP TRIGGER IF EXISTS project_document_revisions_immutable ON project_document_revisions;

-- 2) 순환 FK 해제 — heads.latest/approved 가 revisions 를 참조하고
--    revisions.head_id 가 heads 를 참조하므로, 제약을 먼저 떼야 테이블을 지울 수 있다.
--    (2026-09-23 실측: 이 단계 없이는 DROP TABLE project_document_revisions 가
--     "constraint project_document_latest_fk ... depends on table" 로 실패한다)
ALTER TABLE IF EXISTS project_document_heads
    DROP CONSTRAINT IF EXISTS project_document_latest_fk,
    DROP CONSTRAINT IF EXISTS project_document_approved_fk;
ALTER TABLE IF EXISTS project_document_events
    DROP CONSTRAINT IF EXISTS project_document_events_scope_fk;
ALTER TABLE IF EXISTS project_document_legacy_links
    DROP CONSTRAINT IF EXISTS project_document_legacy_identity_fk;

-- 3) 테이블 — FK 의존 역순 (legacy_links/goal_links → events → revisions → heads → grants)
DROP TABLE IF EXISTS project_document_legacy_links;
DROP TABLE IF EXISTS project_document_goal_links;
DROP TABLE IF EXISTS project_document_events;
DROP TABLE IF EXISTS project_document_revisions;
DROP TABLE IF EXISTS project_document_heads;
DROP TABLE IF EXISTS project_document_grants;

-- 4) 트리거 함수
DROP FUNCTION IF EXISTS reject_project_document_history_mutation();

-- 5) 기존 테이블(goal_documents)에 up 이 추가한 유일 인덱스 —
--    신규 테이블을 지워도 이것은 남으므로 반드시 별도로 지운다.
DROP INDEX IF EXISTS project_document_goal_documents_identity_idx;

-- 6) up 이 스테이징한 핸드오버 1행만 회수한다.
--    revision=1 AND created_by='pipeline_runner' 조건으로, up 이 기존 행을
--    UPDATE 한 경우(revision>=2)는 남긴다 — 남의 감사기록을 지우지 않는다.
DELETE FROM project_handover_events
 WHERE entry_id IN (
     SELECT id FROM project_handover_entries
      WHERE project_key = 'AADS'
        AND entry_key = 'aads-project-documents-canonical-20260923'
        AND created_by = 'pipeline_runner'
        AND revision = 1
 );
DELETE FROM project_handover_entries
 WHERE project_key = 'AADS'
   AND entry_key = 'aads-project-documents-canonical-20260923'
   AND created_by = 'pipeline_runner'
   AND revision = 1;
