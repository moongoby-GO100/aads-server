-- 20261003_l1_doc_storage_rule_draft.sql 롤백.
-- 활성화(enabled=true)된 뒤에는 지우지 않는다 — 활성 자산 삭제는 별도 결정이다.
BEGIN;

DELETE FROM prompt_assets
WHERE slug = 'l1-doc-storage-rule' AND enabled = false;

COMMIT;
