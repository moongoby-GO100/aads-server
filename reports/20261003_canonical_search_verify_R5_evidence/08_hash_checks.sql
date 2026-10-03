\echo '--- revision content_hash == sha256(content) for indexed revisions; legacy validated_hash; chunk concat vs content length'
with idx as (select distinct canonical_revision_id rid from doc_chunks where doc_path like 'canonical://%')
select count(*) revs,
  count(*) filter (where encode(sha256(convert_to(r.content,'UTF8')),'hex') = r.content_hash) sha_matches_content,
  count(*) filter (where encode(sha256(convert_to(r.content,'UTF8')),'hex') <> r.content_hash) sha_mismatch
from idx join project_document_revisions r on r.id=idx.rid;
select count(*) legacy_links, count(*) filter (where l.validated_hash = r.content_hash) legacy_hash_eq_rev_hash from project_document_legacy_links l join project_document_revisions r on r.id=l.revision_id;
\echo '--- indexed chunk text covers revision content? (per rev: chunk content length sum vs content length; chunk_index contiguous)'
with c as (select canonical_revision_id rid, count(*) n, min(chunk_index) mn, max(chunk_index) mx, sum(length(content)) clen from doc_chunks where doc_path like 'canonical://%' group by 1)
select count(*) revs, count(*) filter (where c.mn=0 and c.mx=c.n-1) contiguous_idx, count(*) filter (where c.clen < 0.5*length(r.content)) chunk_text_under_50pct
from c join project_document_revisions r on r.id=c.rid;
\echo '--- secret-pattern spot check on indexed canonical chunks (count only)'
select count(*) chunks_matching_secretish from doc_chunks where doc_path like 'canonical://%' and content ~* '(sk-[a-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|BEGIN (RSA |EC )?PRIVATE KEY)';
