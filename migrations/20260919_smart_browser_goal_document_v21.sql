-- Link the implemented Smart Browser v2.1 specification to its canonical goal.
BEGIN;

WITH previous AS (
    UPDATE goal_documents
       SET status = 'superseded',
           is_latest = FALSE,
           updated_at = clock_timestamp()
     WHERE goal_id = '169e5328-244e-444d-95c8-d20377192671'::uuid
       AND document_key = 'architecture:smart-browser-execution-learning-security'
       AND doc_path <> 'docs/plans/AADS-SMART-BROWSER-IMPLEMENTATION-v2.1-20260919.md'
       AND is_latest IS TRUE
     RETURNING id
), upserted AS (
    INSERT INTO goal_documents (
        goal_id, kind, doc_path, title, note, created_by,
        document_key, version, status, is_latest, change_summary, supersedes_id
    )
    SELECT
        '169e5328-244e-444d-95c8-d20377192671'::uuid,
        'architecture',
        'docs/plans/AADS-SMART-BROWSER-IMPLEMENTATION-v2.1-20260919.md',
        'OVIS Smart Browser 구현 정본 v2.1',
        'M7~M11 구현·보안 경계·E2E·운영 완료 기준 정본',
        'smartbrowser-release-20260919',
        'architecture:smart-browser-execution-learning-security',
        '2.1.0',
        'active',
        TRUE,
        'M7~M11 코드 정본, Channel Router·페이지 비명령화·ARIA·실시간 재검증·골든 승격·복구 UI 반영',
        (SELECT id FROM previous ORDER BY id DESC LIMIT 1)
    ON CONFLICT (goal_id, doc_path) DO UPDATE
       SET title = EXCLUDED.title,
           note = EXCLUDED.note,
           document_key = EXCLUDED.document_key,
           version = EXCLUDED.version,
           status = 'active',
           is_latest = TRUE,
           change_summary = EXCLUDED.change_summary,
           supersedes_id = COALESCE(goal_documents.supersedes_id, EXCLUDED.supersedes_id),
           updated_at = clock_timestamp()
    RETURNING id
)
SELECT id FROM upserted;

COMMIT;
