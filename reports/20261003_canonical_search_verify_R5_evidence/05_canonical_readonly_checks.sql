-- READ-ONLY (run with default_transaction_read_only=on)
\echo '--- A. by project/tenant/server/label'
select server, project, label, tenant_id, count(*) chunks, count(distinct doc_path) docs, count(embedding) emb,
       count(*) filter (where canonical_head_id is null) no_head, count(*) filter (where canonical_revision_id is null) no_rev
from doc_chunks where doc_path like 'canonical://%' group by 1,2,3,4 order by 2;
\echo '--- B. integrity vs heads/revisions (per distinct doc_path)'
with d as (
  select distinct doc_path, doc_sha256, project, tenant_id, canonical_head_id hid, canonical_revision_id rid, title, label from doc_chunks where doc_path like 'canonical://%')
select
 count(*) docs,
 count(*) filter (where h.id is null) head_missing,
 count(*) filter (where h.id is not null and (h.tenant_id<>d.tenant_id or h.project_key<>d.project)) head_tenant_proj_mismatch,
 count(*) filter (where r.id is null) rev_missing,
 count(*) filter (where r.id is not null and r.head_id<>d.hid) rev_head_mismatch,
 count(*) filter (where r.id is not null and r.content_hash<>d.doc_sha256) hash_mismatch,
 count(*) filter (where d.doc_path <> 'canonical://'||h.project_key||'/'||h.document_key||'@'||d.rid) path_mismatch,
 count(*) filter (where d.rid = h.approved_revision_id) is_approved_ptr,
 count(*) filter (where d.rid = h.latest_revision_id and d.rid is distinct from h.approved_revision_id) is_latest_not_approved,
 count(*) filter (where d.rid is distinct from h.approved_revision_id and d.rid is distinct from h.latest_revision_id) stale_pointer,
 count(*) filter (where d.title like '[승인 %') t_approved,
 count(*) filter (where d.title like '[초안 %') t_draft,
 count(*) filter (where d.title like '[승인 %' and d.rid is distinct from h.approved_revision_id) t_approved_but_not_approved_ptr,
 count(*) filter (where d.title like '[초안 %' and d.rid = h.approved_revision_id) t_draft_but_is_approved_ptr
from d left join project_document_heads h on h.id=d.hid left join project_document_revisions r on r.id=d.rid;
\echo '--- C. internal-tenant heads vs indexed (missing/extra)'
with hs as (select h.*, public.aads_internal_tenant_id() it from project_document_heads h),
 idx as (select distinct canonical_head_id hid, canonical_revision_id rid from doc_chunks where doc_path like 'canonical://%')
select
 (select count(*) from project_document_heads) heads_all,
 (select count(*) from project_document_heads where tenant_id=public.aads_internal_tenant_id()) heads_internal,
 (select count(*) from project_document_heads where tenant_id<>public.aads_internal_tenant_id()) heads_other_tenant,
 (select count(distinct hid) from idx) heads_indexed,
 (select count(*) from project_document_heads h where tenant_id=public.aads_internal_tenant_id() and not exists (select 1 from idx where idx.hid=h.id)) heads_internal_not_indexed,
 (select count(*) from project_document_heads h where tenant_id=public.aads_internal_tenant_id() and h.approved_revision_id is not null and not exists (select 1 from idx where idx.rid=h.approved_revision_id)) approved_rev_not_indexed,
 (select count(*) from project_document_heads h where tenant_id=public.aads_internal_tenant_id() and h.latest_revision_id is not null and h.latest_revision_id is distinct from h.approved_revision_id and not exists (select 1 from idx where idx.rid=h.latest_revision_id)) draft_rev_not_indexed,
 (select count(*) from idx where not exists (select 1 from project_document_heads h where h.id=idx.hid)) idx_orphans;
\echo '--- D. duplicates'
select count(*) dup_path_chunkidx from (select doc_path, chunk_index from doc_chunks where doc_path like 'canonical://%' group by 1,2 having count(*)>1) x;
select count(*) heads_with_multi_rev_indexed from (select canonical_head_id from (select distinct canonical_head_id, canonical_revision_id from doc_chunks where doc_path like 'canonical://%') y group by 1 having count(*)>1) z;
select count(*) empty_content_chunks from doc_chunks where doc_path like 'canonical://%' and coalesce(btrim(content),'')='';
\echo '--- E. non-canonical rows leaking canonical markers (should be 0)'
select count(*) label_jeongbon_non_canonical_path from doc_chunks where label='정본' and doc_path not like 'canonical://%';
select count(*) canonical_path_without_label from doc_chunks where doc_path like 'canonical://%' and label is distinct from '정본';
select count(*) tenant_set_on_non_canonical from doc_chunks where doc_path not like 'canonical://%' and (tenant_id is not null or canonical_head_id is not null);
