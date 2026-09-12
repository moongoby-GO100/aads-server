-- WP04 expand-only chat read model, monotonic revisions, outbox, and checkpoints.
-- Transactional DDL only.  Online indexes are intentionally in migration 175.

BEGIN;

ALTER TABLE chat_messages
    ADD COLUMN IF NOT EXISTS content_version BIGINT NOT NULL DEFAULT 1,
    ADD COLUMN IF NOT EXISTS generation_id UUID,
    ADD COLUMN IF NOT EXISTS segment_id UUID,
    ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ;

UPDATE chat_messages
SET segment_id = id
WHERE segment_id IS NULL;

CREATE TABLE IF NOT EXISTS chat_session_revisions (
    tenant_id UUID NOT NULL,
    session_id UUID NOT NULL,
    revision BIGINT NOT NULL DEFAULT 0 CHECK (revision >= 0),
    message_revision BIGINT NOT NULL DEFAULT 0 CHECK (message_revision >= 0),
    artifact_revision BIGINT NOT NULL DEFAULT 0 CHECK (artifact_revision >= 0),
    execution_revision BIGINT NOT NULL DEFAULT 0 CHECK (execution_revision >= 0),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (tenant_id, session_id),
    CONSTRAINT fk_chat_session_revisions_session_tenant
        FOREIGN KEY (session_id, tenant_id)
        REFERENCES chat_sessions(id, tenant_id)
        ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS chat_outbox (
    event_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL,
    session_id UUID NOT NULL,
    session_revision BIGINT NOT NULL CHECK (session_revision > 0),
    event_type TEXT NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    dedupe_key TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    published_at TIMESTAMPTZ,
    claimed_by TEXT,
    claim_epoch BIGINT NOT NULL DEFAULT 0,
    lease_expires_at TIMESTAMPTZ,
    CONSTRAINT fk_chat_outbox_session_tenant
        FOREIGN KEY (session_id, tenant_id)
        REFERENCES chat_sessions(id, tenant_id)
        ON DELETE CASCADE,
    CONSTRAINT uq_chat_outbox_scope_dedupe
        UNIQUE (tenant_id, session_id, dedupe_key)
);

CREATE TABLE IF NOT EXISTS chat_execution_checkpoints (
    execution_id UUID PRIMARY KEY
        REFERENCES chat_turn_executions(id) ON DELETE CASCADE,
    tenant_id UUID NOT NULL,
    session_id UUID NOT NULL,
    generation_id UUID,
    segment_id UUID,
    message_id UUID,
    intent TEXT,
    content_version BIGINT CHECK (content_version IS NULL OR content_version > 0),
    covers_through_event_id TEXT,
    partial_content TEXT NOT NULL DEFAULT '',
    tool_checkpoint JSONB NOT NULL DEFAULT '[]'::jsonb,
    owner_instance TEXT,
    owner_epoch BIGINT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT fk_chat_execution_checkpoints_session_tenant
        FOREIGN KEY (session_id, tenant_id)
        REFERENCES chat_sessions(id, tenant_id)
        ON DELETE CASCADE
);

INSERT INTO chat_session_revisions (
    session_id, tenant_id, revision,
    message_revision, artifact_revision, execution_revision
)
SELECT s.id,
       s.tenant_id,
       counts.message_count + counts.artifact_count + counts.execution_count,
       counts.message_count,
       counts.artifact_count,
       counts.execution_count
FROM chat_sessions s
CROSS JOIN LATERAL (
    SELECT
        (SELECT COUNT(*) FROM chat_messages m WHERE m.session_id = s.id)::bigint
            AS message_count,
        (SELECT COUNT(*) FROM chat_artifacts a WHERE a.session_id = s.id)::bigint
            AS artifact_count,
        (SELECT COUNT(*) FROM chat_turn_executions te WHERE te.session_id = s.id)::bigint
            AS execution_count
) counts
ON CONFLICT (tenant_id, session_id) DO NOTHING;

INSERT INTO chat_execution_checkpoints (
    execution_id, tenant_id, session_id, generation_id, segment_id, message_id, intent,
    content_version, covers_through_event_id, partial_content, tool_checkpoint,
    owner_instance, owner_epoch, created_at, updated_at
)
SELECT te.id,
       s.tenant_id,
       te.session_id,
       m.generation_id,
       COALESCE(m.segment_id, m.id),
       m.id,
       m.intent,
       m.content_version,
       te.last_event_id,
       COALESCE(m.content, ''),
       COALESCE(m.tools_called, '[]'::jsonb),
       te.owner_instance,
       te.owner_epoch,
       COALESCE(te.created_at, NOW()),
       COALESCE(te.updated_at, NOW())
FROM chat_turn_executions te
JOIN chat_sessions s ON s.id = te.session_id
LEFT JOIN LATERAL (
    SELECT candidate.*
    FROM chat_messages candidate
    WHERE candidate.execution_id = te.id
      AND candidate.role = 'assistant'
    ORDER BY
        CASE WHEN candidate.id = te.assistant_message_id THEN 0 ELSE 1 END,
        candidate.created_at DESC,
        candidate.id DESC
    LIMIT 1
) m ON TRUE
ON CONFLICT (execution_id) DO NOTHING;

CREATE OR REPLACE FUNCTION public.aads_chat_message_content_version()
RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path = pg_catalog, public
AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        NEW.content_version := GREATEST(COALESCE(NEW.content_version, 1), 1);
        NEW.segment_id := COALESCE(NEW.segment_id, NEW.id);
        RETURN NEW;
    END IF;

    IF ROW(
        NEW.role, NEW.content, NEW.model_used, NEW.intent, NEW.bookmarked,
        NEW.attachments, NEW.sources, NEW.artifact_id, NEW.execution_id,
        NEW.is_hidden, NEW.deleted_at, NEW.tools_called, NEW.quality_details
    ) IS DISTINCT FROM ROW(
        OLD.role, OLD.content, OLD.model_used, OLD.intent, OLD.bookmarked,
        OLD.attachments, OLD.sources, OLD.artifact_id, OLD.execution_id,
        OLD.is_hidden, OLD.deleted_at, OLD.tools_called, OLD.quality_details
    ) THEN
        NEW.content_version := GREATEST(
            COALESCE(NEW.content_version, 0),
            COALESCE(OLD.content_version, 0) + 1
        );
    ELSE
        NEW.content_version := OLD.content_version;
    END IF;
    NEW.segment_id := COALESCE(NEW.segment_id, OLD.segment_id, NEW.id);
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_chat_message_content_version ON chat_messages;
CREATE TRIGGER trg_chat_message_content_version
BEFORE INSERT OR UPDATE ON chat_messages
FOR EACH ROW
EXECUTE FUNCTION public.aads_chat_message_content_version();

CREATE OR REPLACE FUNCTION public.aads_chat_record_revision()
RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path = pg_catalog, public
AS $$
DECLARE
    v_session_id UUID;
    v_tenant_id UUID;
    v_entity_id UUID;
    v_revision BIGINT;
    v_event_type TEXT;
    v_payload JSONB;
BEGIN
    IF TG_TABLE_NAME = 'chat_sessions' THEN
        v_session_id := NEW.id;
        v_entity_id := NEW.id;
    ELSIF TG_OP = 'DELETE' THEN
        v_session_id := OLD.session_id;
        v_entity_id := OLD.id;
    ELSE
        v_session_id := NEW.session_id;
        v_entity_id := NEW.id;
    END IF;

    -- Token streaming already has an ordered SSE ledger. Emitting a durable
    -- revision/outbox row for every partial-content flush would turn one answer
    -- into thousands of rows and force clients to refetch while the stream is
    -- healthy. Finalization (intent/status change) still records a revision.
    IF TG_TABLE_NAME = 'chat_messages' AND TG_OP = 'UPDATE' THEN
        IF OLD.intent = 'streaming_placeholder'
           AND NEW.intent = 'streaming_placeholder'
           AND ROW(
               NEW.role, NEW.model_used, NEW.bookmarked, NEW.attachments, NEW.sources,
               NEW.artifact_id, NEW.execution_id, NEW.is_hidden, NEW.deleted_at,
               NEW.quality_details
           ) IS NOT DISTINCT FROM ROW(
               OLD.role, OLD.model_used, OLD.bookmarked, OLD.attachments, OLD.sources,
               OLD.artifact_id, OLD.execution_id, OLD.is_hidden, OLD.deleted_at,
               OLD.quality_details
           ) THEN
            RETURN NULL;
        END IF;
    END IF;

    IF TG_TABLE_NAME = 'chat_turn_executions' THEN
        SELECT tenant_id INTO v_tenant_id
        FROM chat_sessions
        WHERE id = v_session_id;
    ELSIF TG_TABLE_NAME = 'chat_sessions' THEN
        v_tenant_id := NEW.tenant_id;
    ELSIF TG_OP = 'DELETE' THEN
        v_tenant_id := OLD.tenant_id;
    ELSE
        v_tenant_id := NEW.tenant_id;
    END IF;
    IF v_tenant_id IS NULL OR NOT EXISTS (
        SELECT 1
        FROM chat_sessions
        WHERE id = v_session_id AND tenant_id = v_tenant_id
    ) THEN
        RETURN NULL;
    END IF;

    IF TG_TABLE_NAME = 'chat_messages' THEN
        v_event_type := 'message.changed';

        INSERT INTO chat_session_revisions (
            session_id, tenant_id, revision, message_revision, updated_at
        ) VALUES (v_session_id, v_tenant_id, 1, 1, NOW())
        ON CONFLICT (tenant_id, session_id) DO UPDATE SET
            revision = chat_session_revisions.revision + 1,
            message_revision = chat_session_revisions.message_revision + 1,
            updated_at = NOW()
        RETURNING revision INTO v_revision;

        v_payload := jsonb_strip_nulls(jsonb_build_object(
            'message_id', v_entity_id,
            'operation', TG_OP,
            'content_version', CASE
                WHEN TG_OP = 'DELETE' THEN OLD.content_version
                ELSE NEW.content_version
            END,
            'tombstone', CASE
                WHEN TG_OP = 'DELETE' THEN TRUE
                ELSE NEW.deleted_at IS NOT NULL
            END
        ));
    ELSIF TG_TABLE_NAME = 'chat_sessions' THEN
        v_event_type := 'session.changed';

        INSERT INTO chat_session_revisions (
            session_id, tenant_id, revision, updated_at
        ) VALUES (v_session_id, v_tenant_id, 1, NOW())
        ON CONFLICT (tenant_id, session_id) DO UPDATE SET
            revision = chat_session_revisions.revision + 1,
            updated_at = NOW()
        RETURNING revision INTO v_revision;

        v_payload := jsonb_build_object(
            'session_id', v_entity_id,
            'operation', TG_OP
        );
    ELSIF TG_TABLE_NAME = 'chat_artifacts' THEN
        v_event_type := 'artifact.changed';

        INSERT INTO chat_session_revisions (
            session_id, tenant_id, revision, artifact_revision, updated_at
        ) VALUES (v_session_id, v_tenant_id, 1, 1, NOW())
        ON CONFLICT (tenant_id, session_id) DO UPDATE SET
            revision = chat_session_revisions.revision + 1,
            artifact_revision = chat_session_revisions.artifact_revision + 1,
            updated_at = NOW()
        RETURNING revision INTO v_revision;

        v_payload := jsonb_build_object(
            'artifact_id', v_entity_id,
            'operation', TG_OP
        );
    ELSE
        v_event_type := 'execution.changed';

        INSERT INTO chat_session_revisions (
            session_id, tenant_id, revision, execution_revision, updated_at
        ) VALUES (v_session_id, v_tenant_id, 1, 1, NOW())
        ON CONFLICT (tenant_id, session_id) DO UPDATE SET
            revision = chat_session_revisions.revision + 1,
            execution_revision = chat_session_revisions.execution_revision + 1,
            updated_at = NOW()
        RETURNING revision INTO v_revision;

        v_payload := jsonb_strip_nulls(jsonb_build_object(
            'execution_id', v_entity_id,
            'operation', TG_OP,
            'phase', CASE WHEN TG_OP = 'DELETE' THEN OLD.status ELSE NEW.status END,
            'owner_epoch', CASE
                WHEN TG_OP = 'DELETE' THEN OLD.owner_epoch
                ELSE NEW.owner_epoch
            END
        ));
    END IF;

    INSERT INTO chat_outbox (
        tenant_id, session_id, session_revision, event_type, payload, dedupe_key
    ) VALUES (
        v_tenant_id,
        v_session_id,
        v_revision,
        v_event_type,
        v_payload,
        TG_TABLE_NAME || ':' || TG_OP || ':' || v_entity_id::text || ':' || v_revision::text
    )
    ON CONFLICT (tenant_id, session_id, dedupe_key) DO NOTHING;

    RETURN NULL;
END;
$$;

DROP TRIGGER IF EXISTS trg_chat_message_revision ON chat_messages;
CREATE TRIGGER trg_chat_message_revision
AFTER INSERT OR DELETE OR UPDATE OF
    role, content, model_used, intent, bookmarked, attachments, sources,
    artifact_id, execution_id, is_hidden, deleted_at, tools_called, quality_details
ON chat_messages
FOR EACH ROW
EXECUTE FUNCTION public.aads_chat_record_revision();

DROP TRIGGER IF EXISTS trg_chat_session_revision ON chat_sessions;
CREATE TRIGGER trg_chat_session_revision
AFTER INSERT OR UPDATE OF
    title, summary, pinned, tags, current_model, role_key,
    current_execution_id, message_count
ON chat_sessions
FOR EACH ROW
EXECUTE FUNCTION public.aads_chat_record_revision();

DROP TRIGGER IF EXISTS trg_chat_artifact_revision ON chat_artifacts;
CREATE TRIGGER trg_chat_artifact_revision
AFTER INSERT OR UPDATE OR DELETE ON chat_artifacts
FOR EACH ROW
EXECUTE FUNCTION public.aads_chat_record_revision();

DROP TRIGGER IF EXISTS trg_chat_execution_revision ON chat_turn_executions;
CREATE TRIGGER trg_chat_execution_revision
AFTER INSERT OR DELETE OR UPDATE OF
    status, assistant_message_id, requested_model, actual_model, last_event_id,
    error_message, owner_instance, owner_epoch, completed_at
ON chat_turn_executions
FOR EACH ROW
EXECUTE FUNCTION public.aads_chat_record_revision();

CREATE OR REPLACE FUNCTION public.aads_chat_write_execution_checkpoint(
    p_execution_id UUID
)
RETURNS VOID
LANGUAGE plpgsql
SET search_path = pg_catalog, public
AS $$
DECLARE
    v_tenant_id UUID;
    v_execution chat_turn_executions%ROWTYPE;
    v_message chat_messages%ROWTYPE;
BEGIN
    SELECT candidate.* INTO v_execution
    FROM chat_turn_executions candidate
    WHERE candidate.id = p_execution_id;
    IF NOT FOUND THEN
        RETURN;
    END IF;

    SELECT tenant_id INTO v_tenant_id
    FROM chat_sessions
    WHERE id = v_execution.session_id;

    SELECT candidate.* INTO v_message
    FROM chat_messages candidate
    WHERE candidate.execution_id = v_execution.id
      AND candidate.role = 'assistant'
    ORDER BY
        CASE WHEN candidate.id = v_execution.assistant_message_id THEN 0 ELSE 1 END,
        candidate.created_at DESC,
        candidate.id DESC
    LIMIT 1;

    INSERT INTO chat_execution_checkpoints (
        execution_id, tenant_id, session_id, generation_id, segment_id, message_id, intent,
        content_version, covers_through_event_id, partial_content, tool_checkpoint,
        owner_instance, owner_epoch, updated_at
    ) VALUES (
        v_execution.id, v_tenant_id, v_execution.session_id, v_message.generation_id,
        COALESCE(v_message.segment_id, v_message.id), v_message.id, v_message.intent,
        v_message.content_version,
        v_execution.last_event_id, COALESCE(v_message.content, ''),
        COALESCE(v_message.tools_called, '[]'::jsonb),
        v_execution.owner_instance, v_execution.owner_epoch, NOW()
    )
    ON CONFLICT (execution_id) DO UPDATE SET
        tenant_id = EXCLUDED.tenant_id,
        session_id = EXCLUDED.session_id,
        generation_id = EXCLUDED.generation_id,
        segment_id = EXCLUDED.segment_id,
        message_id = EXCLUDED.message_id,
        intent = EXCLUDED.intent,
        content_version = EXCLUDED.content_version,
        covers_through_event_id = EXCLUDED.covers_through_event_id,
        partial_content = EXCLUDED.partial_content,
        tool_checkpoint = EXCLUDED.tool_checkpoint,
        owner_instance = EXCLUDED.owner_instance,
        owner_epoch = EXCLUDED.owner_epoch,
        updated_at = NOW()
    WHERE chat_execution_checkpoints.owner_epoch <= EXCLUDED.owner_epoch;
    RETURN;
END;
$$;

CREATE OR REPLACE FUNCTION public.aads_chat_checkpoint_execution()
RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path = pg_catalog, public
AS $$
BEGIN
    PERFORM public.aads_chat_write_execution_checkpoint(NEW.id);
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION public.aads_chat_checkpoint_message()
RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path = pg_catalog, public
AS $$
DECLARE
    v_execution_id UUID;
BEGIN
    v_execution_id := CASE
        WHEN TG_OP = 'DELETE' THEN OLD.execution_id
        ELSE NEW.execution_id
    END;
    -- Keep recovery content durable at a bounded cadence while SSE is active.
    -- The terminal execution trigger performs an unconditional final flush.
    IF TG_OP = 'UPDATE'
       AND OLD.intent = 'streaming_placeholder'
       AND NEW.intent = 'streaming_placeholder'
       AND v_execution_id IS NOT NULL THEN
        PERFORM 1
        FROM chat_execution_checkpoints
        WHERE execution_id = v_execution_id
          AND updated_at > clock_timestamp() - INTERVAL '1 second';
        IF FOUND THEN
            RETURN NULL;
        END IF;
    END IF;
    IF v_execution_id IS NOT NULL THEN
        PERFORM public.aads_chat_write_execution_checkpoint(v_execution_id);
    END IF;
    IF TG_OP = 'UPDATE'
       AND OLD.execution_id IS NOT NULL
       AND OLD.execution_id IS DISTINCT FROM NEW.execution_id THEN
        PERFORM public.aads_chat_write_execution_checkpoint(OLD.execution_id);
    END IF;
    RETURN NULL;
END;
$$;

DROP TRIGGER IF EXISTS trg_chat_execution_checkpoint ON chat_turn_executions;
CREATE TRIGGER trg_chat_execution_checkpoint
AFTER INSERT OR UPDATE OF
    status, assistant_message_id, last_event_id, owner_instance, owner_epoch
ON chat_turn_executions
FOR EACH ROW
EXECUTE FUNCTION public.aads_chat_checkpoint_execution();

DROP TRIGGER IF EXISTS trg_chat_message_checkpoint ON chat_messages;
CREATE TRIGGER trg_chat_message_checkpoint
AFTER INSERT OR DELETE OR UPDATE OF
    content, intent, model_used, tools_called, is_hidden, execution_id
ON chat_messages
FOR EACH ROW
EXECUTE FUNCTION public.aads_chat_checkpoint_message();

COMMENT ON TABLE chat_session_revisions IS
    'WP04 monotonic, tenant-scoped visible chat revision ledger';
COMMENT ON TABLE chat_outbox IS
    'WP04 transactional changed-ID outbox; delivery is at-least-once';
COMMENT ON TABLE chat_execution_checkpoints IS
    'WP04 content and replay coverage captured by the same execution transaction';

COMMIT;
