-- AADS-DOC-SEARCH-TENANT-GUARD-20261003
-- 정본 청크에 tenant/head/revision 소유 칸을 추가한다. 순수 추가(nullable) — 기존 파일 청크는
-- 세 칸이 NULL 로 남고 검색 계약이 그대로다. 정본 청크는 이 칸이 채워져야만 검색에 나온다
-- (칸이 비어 있으면 fail-closed). 기존 정본 청크는 `index_docs.py index-canonical` 재색인으로 채운다.
-- FK 는 두지 않는다: head 삭제가 청크 쓰기를 막지 않게 하고, 검색은 head 를 실시간 조인해 거른다.
ALTER TABLE doc_chunks ADD COLUMN IF NOT EXISTS tenant_id uuid;
ALTER TABLE doc_chunks ADD COLUMN IF NOT EXISTS canonical_head_id uuid;
ALTER TABLE doc_chunks ADD COLUMN IF NOT EXISTS canonical_revision_id uuid;

CREATE INDEX IF NOT EXISTS idx_doc_chunks_canonical_tenant_project
    ON doc_chunks (tenant_id, project)
    WHERE tenant_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_doc_chunks_canonical_head
    ON doc_chunks (canonical_head_id)
    WHERE canonical_head_id IS NOT NULL;
