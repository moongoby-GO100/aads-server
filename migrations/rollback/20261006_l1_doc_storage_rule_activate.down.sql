-- 20261006_l1_doc_storage_rule_activate.sql 롤백.
-- 현재 본문 md5 가 적용 직후 값(applied_md5)과 같을 때만 원본 행(content/title/enabled/scopes/priority 등)을 복원한다.
-- 그 사이 누군가 고쳤다면 덮어쓰지 않고 중단한다. 백업이 없으면 아무것도 하지 않는다(멱등).
BEGIN;

DO $rollback$
DECLARE
    b prompt_rdoc_activation_backup%ROWTYPE;
    cur prompt_assets%ROWTYPE;
    o jsonb;
BEGIN
    SELECT * INTO b FROM prompt_rdoc_activation_backup
     WHERE slug = 'l1-doc-storage-rule' AND rolled_back_at IS NULL
     ORDER BY id DESC LIMIT 1;
    IF NOT FOUND THEN
        RAISE NOTICE '되돌릴 활성화 기록이 없다 — 변경 없음';
        RETURN;
    END IF;

    SELECT * INTO cur FROM prompt_assets WHERE slug = 'l1-doc-storage-rule' FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'l1-doc-storage-rule 행이 없다 — 중단';
    END IF;
    IF md5(cur.content) <> b.applied_md5 THEN
        RAISE EXCEPTION '적용 이후 본문이 바뀌었다(현재 md5=%) — 덮어쓰지 않고 중단', md5(cur.content);
    END IF;

    o := b.original_row;
    UPDATE prompt_assets SET
        content        = o->>'content',
        title          = o->>'title',
        enabled        = (o->>'enabled')::boolean,
        priority       = (o->>'priority')::int,
        model_variants = o->'model_variants',
        workspace_scope = CASE WHEN jsonb_typeof(o->'workspace_scope') = 'array' THEN ARRAY(SELECT jsonb_array_elements_text(o->'workspace_scope')) END,
        intent_scope = CASE WHEN jsonb_typeof(o->'intent_scope') = 'array' THEN ARRAY(SELECT jsonb_array_elements_text(o->'intent_scope')) END,
        target_models = CASE WHEN jsonb_typeof(o->'target_models') = 'array' THEN ARRAY(SELECT jsonb_array_elements_text(o->'target_models')) END,
        role_scope = CASE WHEN jsonb_typeof(o->'role_scope') = 'array' THEN ARRAY(SELECT jsonb_array_elements_text(o->'role_scope')) END,
        updated_at      = now()
     WHERE id = cur.id;

    UPDATE prompt_rdoc_activation_backup SET rolled_back_at = now() WHERE id = b.id;
END
$rollback$;

COMMIT;
