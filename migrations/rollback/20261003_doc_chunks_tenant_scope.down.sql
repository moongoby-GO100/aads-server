-- 롤백: 코드(검색 가드)를 먼저 이전 버전으로 돌린 뒤에 실행한다. 칸을 지우면 정본 청크는
-- 격리 근거를 잃으므로 먼저 정본 청크를 비운다(다음 index-canonical 재색인이 다시 채운다).
DELETE FROM doc_chunks WHERE label = '정본' AND doc_path LIKE 'canonical://%';
DROP INDEX IF EXISTS idx_doc_chunks_canonical_head;
DROP INDEX IF EXISTS idx_doc_chunks_canonical_tenant_project;
ALTER TABLE doc_chunks DROP COLUMN IF EXISTS canonical_revision_id;
ALTER TABLE doc_chunks DROP COLUMN IF EXISTS canonical_head_id;
ALTER TABLE doc_chunks DROP COLUMN IF EXISTS tenant_id;
