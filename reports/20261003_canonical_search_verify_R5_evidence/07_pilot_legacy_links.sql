\echo '--- goal_documents 74/92/109/110/119 + legacy link -> head/canonical chunks'
select g.id gdoc, g.kind, g.document_key gdoc_key, g.version, g.status, g.is_latest, left(g.doc_path,70) doc_path,
  l.project_key link_proj, h.document_key head_key, left(h.id::text,8) head,
  (h.approved_revision_id=l.revision_id) link_rev_is_approved,
  (select count(*) from doc_chunks c where c.canonical_revision_id=l.revision_id) chunks_for_link_rev
from goal_documents g left join project_document_legacy_links l on l.goal_document_id=g.id
left join project_document_revisions r on r.id=l.revision_id left join project_document_heads h on h.id=r.head_id
where g.id in (74,92,109,110,119) order by g.id;
\echo '--- all legacy links'
select l.goal_document_id gdoc, l.project_key, h.document_key, (h.approved_revision_id=l.revision_id) is_approved from project_document_legacy_links l join project_document_revisions r on r.id=l.revision_id join project_document_heads h on h.id=r.head_id order by 1;
