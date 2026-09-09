-- 168: Every completed assistant message updates one canonical handover checkpoint per session.
-- Existing sessions are backfilled from their latest completed assistant message.
-- Additive and idempotent: no chat row or legacy Markdown file is modified.

BEGIN;

CREATE OR REPLACE FUNCTION public.aads_upsert_session_handover(p_message_id UUID)
RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
    v_message       chat_messages%ROWTYPE;
    v_tenant_id     UUID;
    v_session_title TEXT;
    v_workspace     TEXT;
    v_project_key   TEXT;
    v_user_content  TEXT;
    v_body          TEXT;
    v_entry          project_handover_entries%ROWTYPE;
BEGIN
    SELECT * INTO v_message
      FROM chat_messages
     WHERE id = p_message_id;

    IF NOT FOUND
       OR v_message.role <> 'assistant'
       OR BTRIM(COALESCE(v_message.content, '')) = ''
       OR COALESCE(v_message.intent, '') IN (
           'streaming_placeholder', 'interrupted_partial', '_archived_partial',
           'interruption_notice', 'stale_empty_placeholder'
       ) THEN
        RETURN FALSE;
    END IF;

    SELECT s.tenant_id, COALESCE(NULLIF(s.title, ''), '제목 없는 세션'),
           COALESCE(w.name, ''),
           COALESCE(
               NULLIF(UPPER(BTRIM(w.project_key)), ''),
               NULLIF(UPPER(SUBSTRING(COALESCE(w.name, '') FROM '\[([A-Za-z0-9_.-]+)\]')), ''),
               'AADS'
           )
      INTO v_tenant_id, v_session_title, v_workspace, v_project_key
      FROM chat_sessions s
      LEFT JOIN chat_workspaces w ON w.id = s.workspace_id
     WHERE s.id = v_message.session_id;

    IF v_tenant_id IS NULL THEN
        RETURN FALSE;
    END IF;

    v_project_key := REGEXP_REPLACE(v_project_key, '[^A-Z0-9_.-]+', '', 'g');
    IF v_project_key = '' THEN
        v_project_key := 'AADS';
    END IF;

    SELECT content INTO v_user_content
      FROM chat_messages
     WHERE session_id = v_message.session_id
       AND role = 'user'
       AND created_at <= v_message.created_at
     ORDER BY created_at DESC, id DESC
     LIMIT 1;

    v_body := CONCAT(
        '## 마지막 사용자 요청', E'\n\n', LEFT(COALESCE(v_user_content, '(없음)'), 4000),
        E'\n\n## 마지막 AI 응답\n\n', LEFT(v_message.content, 20000)
    );

    INSERT INTO project_handover_entries (
        tenant_id, project_key, entry_key, entry_type, title, summary, body,
        status, priority, source_kind, source_session_id, source_task_id,
        source_path, metadata, created_by
    ) VALUES (
        v_tenant_id, v_project_key, 'session:' || v_message.session_id::text,
        'status', '[세션] ' || LEFT(v_session_title, 290),
        LEFT(v_message.content, 2000), v_body,
        'active', 'P2', 'session_auto', v_message.session_id,
        v_message.execution_id::text, 'chat://' || v_message.session_id::text,
        jsonb_build_object(
            'auto_checkpoint', true,
            'assistant_message_id', v_message.id,
            'intent', v_message.intent,
            'model_used', v_message.model_used,
            'workspace', v_workspace
        ),
        'chat_message_trigger'
    )
    ON CONFLICT (tenant_id, project_key, entry_key) DO UPDATE SET
        entry_type = EXCLUDED.entry_type,
        title = EXCLUDED.title,
        summary = EXCLUDED.summary,
        body = EXCLUDED.body,
        status = 'active',
        priority = EXCLUDED.priority,
        source_kind = EXCLUDED.source_kind,
        source_session_id = EXCLUDED.source_session_id,
        source_task_id = EXCLUDED.source_task_id,
        source_path = EXCLUDED.source_path,
        metadata = EXCLUDED.metadata,
        revision = project_handover_entries.revision + 1,
        updated_at = NOW(),
        resolved_at = NULL
    WHERE project_handover_entries.body IS DISTINCT FROM EXCLUDED.body
       OR project_handover_entries.metadata IS DISTINCT FROM EXCLUDED.metadata
       OR project_handover_entries.title IS DISTINCT FROM EXCLUDED.title
    RETURNING * INTO v_entry;

    IF v_entry.id IS NULL THEN
        RETURN FALSE;
    END IF;

    INSERT INTO project_handover_events (
        entry_id, tenant_id, project_key, event_type, revision,
        snapshot, change_summary, changed_by
    ) VALUES (
        v_entry.id, v_entry.tenant_id, v_entry.project_key,
        CASE WHEN v_entry.revision = 1 THEN 'created' ELSE 'updated' END,
        v_entry.revision, TO_JSONB(v_entry) - 'search_vector',
        'Automatic checkpoint after completed assistant response',
        'chat_message_trigger'
    )
    ON CONFLICT (entry_id, revision) DO NOTHING;

    RETURN TRUE;
END;
$$;

CREATE OR REPLACE FUNCTION public.aads_chat_message_handover_trigger()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    PERFORM public.aads_upsert_session_handover(NEW.id);
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_chat_message_handover_checkpoint ON chat_messages;
CREATE TRIGGER trg_chat_message_handover_checkpoint
AFTER INSERT OR UPDATE OF role, content, intent ON chat_messages
FOR EACH ROW
WHEN (NEW.role = 'assistant')
EXECUTE FUNCTION public.aads_chat_message_handover_trigger();

-- Backfill one current checkpoint for every existing session that has a final response.
SELECT public.aads_upsert_session_handover(latest.id)
  FROM (
      SELECT DISTINCT ON (session_id) id
        FROM chat_messages
       WHERE role = 'assistant'
         AND BTRIM(COALESCE(content, '')) <> ''
         AND COALESCE(intent, '') NOT IN (
             'streaming_placeholder', 'interrupted_partial', '_archived_partial',
             'interruption_notice', 'stale_empty_placeholder'
         )
       ORDER BY session_id, created_at DESC, id DESC
  ) AS latest;

COMMENT ON FUNCTION public.aads_upsert_session_handover(UUID) IS
    'Idempotently mirrors a completed assistant message to the tenant/project session checkpoint';

COMMIT;
