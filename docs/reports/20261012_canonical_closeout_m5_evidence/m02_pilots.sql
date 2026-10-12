\echo '--- tenant distribution of heads / canonical chunks (tenant aliased by rank)'
select dense_rank() over (order by h.tenant_id::text) tenant_rank, count(*) heads from project_document_heads h group by h.tenant_id order by 1;
select count(distinct tenant_id) tenants_with_heads from project_document_heads;
\echo '--- 5 pilots: goal_document -> legacy link -> head/revision/approved pointer'
select g.id gdoc, g.kind, g.version gdoc_ver, g.status gdoc_status, g.is_latest, left(g.doc_path,60) doc_path,
  l.project_key link_proj, h.document_key head_key, left(h.id::text,8) head, h.generation gen,
  (h.approved_revision_id is not null) has_approved,
  (h.latest_revision_id=h.approved_revision_id) latest_is_approved,
  (h.approved_revision_id=l.revision_id) link_rev_is_approved,
  ar.version approved_ver, ar.revision approved_rev, ar.content_hash=encode(digest(convert_to(ar.content,'UTF8'),'sha256'),'hex') approved_hash_ok,
  lr.version latest_ver, lr.revision latest_rev, lr.title latest_title,
  (select count(*) from doc_chunks c where c.canonical_revision_id=h.approved_revision_id) approved_chunks,
  (select count(*) from doc_chunks c where c.canonical_revision_id=h.latest_revision_id) latest_chunks
from goal_documents g left join project_document_legacy_links l on l.goal_document_id=g.id
left join project_document_revisions r on r.id=l.revision_id left join project_document_heads h on h.id=r.head_id
left join project_document_revisions ar on ar.id=h.approved_revision_id
left join project_document_revisions lr on lr.id=h.latest_revision_id
where g.id in (74,92,109,110,119) order by g.id;
