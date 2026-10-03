\echo '--- heads (no content)'
select h.project_key, h.document_key, left(h.id::text,8) head, h.generation g,
  (h.approved_revision_id is not null) has_approved, (h.latest_revision_id=h.approved_revision_id) latest_is_approved,
  r.revision latest_rev, r.version, r.source_kind, left(r.source_path,60) src_path,
  (select count(*) from doc_chunks c where c.canonical_head_id=h.id) chunks,
  (select string_agg(distinct left(c.title,28), ' | ') from doc_chunks c where c.canonical_head_id=h.id) idx_titles,
  (select e.action from project_document_events e where e.head_id=h.id order by e.id desc limit 1) last_event
from project_document_heads h left join project_document_revisions r on r.id=h.latest_revision_id order by 1,2;
\echo '--- events with goal_document_id (pilot candidates)'
select e.goal_document_id, h.project_key, h.document_key, e.action, e.created_at at time zone 'Asia/Seoul' kst
from project_document_events e join project_document_heads h on h.id=e.head_id where e.goal_document_id is not null order by e.goal_document_id, e.id;
\echo '--- grants (user ids hashed)'
select tenant_id, project_key, access, left(md5(user_id),8) user_hash, granted_at at time zone 'Asia/Seoul' kst from project_document_grants order by 2,3,4;
