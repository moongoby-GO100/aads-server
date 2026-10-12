\echo '--- now'
select now() at time zone 'Asia/Seoul' kst;
\echo '--- totals'
select count(*) chunks, count(*) filter (where canonical_head_id is not null) canonical_chunks, count(*) filter (where canonical_head_id is not null and embedding is not null) canonical_emb from doc_chunks;
select (select count(*) from project_document_heads) heads,(select count(*) from project_document_revisions) revisions,(select count(*) from project_document_grants) grants,(select count(*) from project_document_legacy_links) legacy_links,(select count(*) from project_document_events) events;
