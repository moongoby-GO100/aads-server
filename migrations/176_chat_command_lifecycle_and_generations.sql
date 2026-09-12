-- WP05 durable command lifecycle, idempotency, and stable generation identity.
--
-- Additive and idempotent: every statement is IF NOT EXISTS / CREATE OR REPLACE,
-- so re-running during a rolling deploy is a no-op.  Transactional DDL only;
-- online indexes belong in a separate CONCURRENTLY migration, as in 174/175.
--
-- ROLLBACK PATH (no data loss for WP03/WP04 tables):
--   DROP TRIGGER IF EXISTS trg_chat_execution_a_generation ON chat_turn_executions;
--   DROP TRIGGER IF EXISTS trg_chat_generation_fence ON chat_execution_generations;
--   DROP TRIGGER IF EXISTS trg_chat_command_transition ON chat_commands;
--   DROP FUNCTION IF EXISTS public.aads_chat_ensure_generation();
--   DROP FUNCTION IF EXISTS public.aads_chat_fence_generation();
--   DROP FUNCTION IF EXISTS public.aads_chat_command_transition();
--   DROP TABLE IF EXISTS chat_commands;
--   DROP TABLE IF EXISTS chat_execution_generations;
--   ALTER TABLE chat_turn_executions DROP COLUMN IF EXISTS generation_id;
--   -- then re-apply migration 174's aads_chat_write_execution_checkpoint body to
--   -- drop the generation COALESCE added below.
-- Dropping these leaves chat_messages / chat_execution_checkpoints untouched; the
-- checkpoint generation_id simply reverts to the chat_messages-sourced value.

BEGIN;

-- ── Stable generation identity ──────────────────────────────────────────────
-- WP03 advertises snapshot.generation_identity = "unavailable" because nothing
-- assigns a generation.  A generation is one uninterrupted attempt at producing
-- an execution's answer.  Identity is keyed by (execution_id, owner_epoch) so it
-- is *stable*: reconnecting to the same fenced generation always resolves to the
-- same generation_id, while every new lease claim (epoch + 1) starts a new one.

ALTER TABLE chat_turn_executions
    ADD COLUMN IF NOT EXISTS generation_id UUID;

CREATE TABLE IF NOT EXISTS chat_execution_generations (
    generation_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    execution_id UUID NOT NULL
        REFERENCES chat_turn_executions(id) ON DELETE CASCADE,
    tenant_id UUID NOT NULL,
    session_id UUID NOT NULL,
    owner_instance TEXT,
    owner_epoch BIGINT NOT NULL CHECK (owner_epoch >= 0),
    attempt INTEGER NOT NULL DEFAULT 1 CHECK (attempt >= 1),
    command_id UUID,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'superseded', 'completed', 'failed')),
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ended_at TIMESTAMPTZ,
    CONSTRAINT uq_chat_execution_generation_epoch
        UNIQUE (execution_id, owner_epoch),
    CONSTRAINT fk_chat_execution_generations_session_tenant
        FOREIGN KEY (session_id, tenant_id)
        REFERENCES chat_sessions(id, tenant_id)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_chat_execution_generations_execution
    ON chat_execution_generations(execution_id, owner_epoch DESC);

CREATE INDEX IF NOT EXISTS idx_chat_execution_generations_active
    ON chat_execution_generations(session_id, started_at DESC)
    WHERE status = 'active';

-- ── Durable command lifecycle + idempotency ─────────────────────────────────
-- One row per accepted chat command (send/interrupt/resume/retry/stop).  The row
-- commits *before* any side effect runs, so a process restart or a blue/green
-- slot switch can always recover the command's final state.  Idempotency is the
-- unique key; the fingerprint makes key reuse with a different body fail closed
-- instead of silently returning someone else's result.

CREATE TABLE IF NOT EXISTS chat_commands (
    command_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL,
    session_id UUID NOT NULL,
    command_type TEXT NOT NULL
        CHECK (command_type IN ('send', 'interrupt', 'resume', 'retry', 'stop')),
    idempotency_key TEXT NOT NULL CHECK (length(idempotency_key) BETWEEN 1 AND 200),
    request_fingerprint TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'accepted'
        CHECK (status IN ('accepted', 'running', 'succeeded', 'failed', 'superseded')),
    execution_id UUID,
    generation_id UUID,
    owner_instance TEXT,
    owner_epoch BIGINT NOT NULL DEFAULT 0 CHECK (owner_epoch >= 0),
    attempt INTEGER NOT NULL DEFAULT 0 CHECK (attempt >= 0),
    result JSONB,
    error JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    CONSTRAINT uq_chat_commands_idempotency
        UNIQUE (tenant_id, session_id, command_type, idempotency_key),
    CONSTRAINT fk_chat_commands_session_tenant
        FOREIGN KEY (session_id, tenant_id)
        REFERENCES chat_sessions(id, tenant_id)
        ON DELETE CASCADE,
    CONSTRAINT ck_chat_commands_terminal_completed_at
        CHECK (
            (status IN ('succeeded', 'failed', 'superseded')) = (completed_at IS NOT NULL)
        )
);

CREATE INDEX IF NOT EXISTS idx_chat_commands_session_created
    ON chat_commands(tenant_id, session_id, created_at DESC);

-- Orphan sweep target: non-terminal commands only.
CREATE INDEX IF NOT EXISTS idx_chat_commands_inflight
    ON chat_commands(updated_at)
    WHERE status IN ('accepted', 'running');

-- ── Generation assignment (automatic, every writer) ─────────────────────────
-- Implemented as a trigger rather than a call inside _claim_execution_lease so
-- that *all* existing fenced writers (lease claim, chat_repair, resume worker)
-- get stable identity without changing any Python signature.
--
-- The trigger name sorts before trg_chat_execution_checkpoint so the checkpoint
-- writer observes the generation in the same statement; the COALESCE added to
-- aads_chat_write_execution_checkpoint below makes that ordering non-load-bearing.

CREATE OR REPLACE FUNCTION public.aads_chat_ensure_generation()
RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path = pg_catalog, public
AS $$
DECLARE
    v_tenant_id UUID;
    v_generation_id UUID;
    v_attempt INTEGER;
BEGIN
    SELECT tenant_id INTO v_tenant_id
    FROM chat_sessions
    WHERE id = NEW.session_id;
    IF v_tenant_id IS NULL THEN
        -- Fail closed on a dangling session rather than minting an unscoped
        -- generation that no tenant-scoped reader could ever fence against.
        RETURN NULL;
    END IF;

    SELECT generation_id INTO v_generation_id
    FROM chat_execution_generations
    WHERE execution_id = NEW.id
      AND owner_epoch = NEW.owner_epoch;

    IF v_generation_id IS NULL THEN
        SELECT COALESCE(MAX(attempt), 0) + 1 INTO v_attempt
        FROM chat_execution_generations
        WHERE execution_id = NEW.id;

        -- Any generation from an older epoch can no longer write.
        UPDATE chat_execution_generations
        SET status = 'superseded',
            ended_at = COALESCE(ended_at, NOW())
        WHERE execution_id = NEW.id
          AND owner_epoch < NEW.owner_epoch
          AND status = 'active';

        INSERT INTO chat_execution_generations (
            execution_id, tenant_id, session_id, owner_instance,
            owner_epoch, attempt, status
        ) VALUES (
            NEW.id, v_tenant_id, NEW.session_id, NEW.owner_instance,
            NEW.owner_epoch, v_attempt, 'active'
        )
        ON CONFLICT (execution_id, owner_epoch) DO NOTHING
        RETURNING generation_id INTO v_generation_id;

        IF v_generation_id IS NULL THEN
            SELECT generation_id INTO v_generation_id
            FROM chat_execution_generations
            WHERE execution_id = NEW.id
              AND owner_epoch = NEW.owner_epoch;
        END IF;
    END IF;

    -- generation_id is not in any revision/checkpoint trigger's UPDATE OF list,
    -- so this write cannot recurse or inflate the revision ledger.
    IF v_generation_id IS NOT NULL
       AND NEW.generation_id IS DISTINCT FROM v_generation_id THEN
        UPDATE chat_turn_executions
        SET generation_id = v_generation_id
        WHERE id = NEW.id;
    END IF;
    RETURN NULL;
END;
$$;

DROP TRIGGER IF EXISTS trg_chat_execution_a_generation ON chat_turn_executions;
CREATE TRIGGER trg_chat_execution_a_generation
AFTER INSERT OR UPDATE OF owner_epoch ON chat_turn_executions
FOR EACH ROW
EXECUTE FUNCTION public.aads_chat_ensure_generation();

-- ── Generation fencing (old writers cannot resurrect themselves) ────────────

CREATE OR REPLACE FUNCTION public.aads_chat_fence_generation()
RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path = pg_catalog, public
AS $$
DECLARE
    v_max_epoch BIGINT;
BEGIN
    IF TG_OP = 'INSERT' THEN
        SELECT MAX(owner_epoch) INTO v_max_epoch
        FROM chat_execution_generations
        WHERE execution_id = NEW.execution_id;
        IF v_max_epoch IS NOT NULL AND NEW.owner_epoch < v_max_epoch THEN
            RAISE EXCEPTION
                'chat_generation_epoch_reversal: execution % epoch % is behind %',
                NEW.execution_id, NEW.owner_epoch, v_max_epoch
                USING ERRCODE = '55006';
        END IF;
        RETURN NEW;
    END IF;

    IF OLD.status IN ('completed', 'failed') AND NEW.status IS DISTINCT FROM OLD.status THEN
        RAISE EXCEPTION
            'chat_generation_terminal_immutable: generation % is already %',
            OLD.generation_id, OLD.status
            USING ERRCODE = '55006';
    END IF;
    IF OLD.status = 'superseded' AND NEW.status = 'active' THEN
        RAISE EXCEPTION
            'chat_generation_superseded_immutable: generation % cannot reactivate',
            OLD.generation_id
            USING ERRCODE = '55006';
    END IF;
    IF NEW.owner_epoch IS DISTINCT FROM OLD.owner_epoch
       OR NEW.execution_id IS DISTINCT FROM OLD.execution_id THEN
        RAISE EXCEPTION
            'chat_generation_identity_immutable: generation % identity is fixed',
            OLD.generation_id
            USING ERRCODE = '55006';
    END IF;
    IF NEW.status IN ('completed', 'failed', 'superseded') THEN
        NEW.ended_at := COALESCE(NEW.ended_at, NOW());
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_chat_generation_fence ON chat_execution_generations;
CREATE TRIGGER trg_chat_generation_fence
BEFORE INSERT OR UPDATE ON chat_execution_generations
FOR EACH ROW
EXECUTE FUNCTION public.aads_chat_fence_generation();

-- ── Command state machine (terminal states are immutable) ───────────────────

CREATE OR REPLACE FUNCTION public.aads_chat_command_transition()
RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path = pg_catalog, public
AS $$
BEGIN
    IF OLD.status IN ('succeeded', 'failed', 'superseded') THEN
        IF NEW.status IS DISTINCT FROM OLD.status
           OR NEW.result IS DISTINCT FROM OLD.result
           OR NEW.error IS DISTINCT FROM OLD.error THEN
            RAISE EXCEPTION
                'chat_command_terminal_immutable: command % is already %',
                OLD.command_id, OLD.status
                USING ERRCODE = '55006';
        END IF;
        RETURN NEW;
    END IF;
    IF NEW.status = 'accepted' AND OLD.status = 'running' THEN
        RAISE EXCEPTION
            'chat_command_invalid_transition: command % cannot return to accepted',
            OLD.command_id
            USING ERRCODE = '55006';
    END IF;
    -- An older fence may never overwrite a newer generation's command state.
    IF NEW.owner_epoch < OLD.owner_epoch THEN
        RAISE EXCEPTION
            'chat_command_epoch_reversal: command % epoch % is behind %',
            OLD.command_id, NEW.owner_epoch, OLD.owner_epoch
            USING ERRCODE = '55006';
    END IF;
    IF NEW.command_type IS DISTINCT FROM OLD.command_type
       OR NEW.idempotency_key IS DISTINCT FROM OLD.idempotency_key
       OR NEW.request_fingerprint IS DISTINCT FROM OLD.request_fingerprint
       OR NEW.session_id IS DISTINCT FROM OLD.session_id
       OR NEW.tenant_id IS DISTINCT FROM OLD.tenant_id THEN
        RAISE EXCEPTION
            'chat_command_identity_immutable: command % identity is fixed',
            OLD.command_id
            USING ERRCODE = '55006';
    END IF;
    NEW.updated_at := NOW();
    IF NEW.status IN ('succeeded', 'failed', 'superseded') THEN
        NEW.completed_at := COALESCE(NEW.completed_at, NOW());
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_chat_command_transition ON chat_commands;
CREATE TRIGGER trg_chat_command_transition
BEFORE UPDATE ON chat_commands
FOR EACH ROW
EXECUTE FUNCTION public.aads_chat_command_transition();

-- ── Checkpoint writer now prefers the execution's stable generation ─────────
-- Extends migration 174's function; the only change is the generation COALESCE.
-- The owner_epoch fence in the ON CONFLICT clause is preserved verbatim.

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
        v_execution.id, v_tenant_id, v_execution.session_id,
        COALESCE(v_message.generation_id, v_execution.generation_id),
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

-- Backfill a generation for every execution that already exists, so recovery
-- after this migration never sees a NULL generation for live work.
INSERT INTO chat_execution_generations (
    execution_id, tenant_id, session_id, owner_instance, owner_epoch, attempt, status,
    started_at, ended_at
)
SELECT te.id,
       s.tenant_id,
       te.session_id,
       te.owner_instance,
       te.owner_epoch,
       1,
       CASE WHEN te.status IN ('running', 'retrying') THEN 'active' ELSE 'completed' END,
       COALESCE(te.created_at, NOW()),
       CASE WHEN te.status IN ('running', 'retrying') THEN NULL
            ELSE COALESCE(te.completed_at, te.updated_at, NOW()) END
FROM chat_turn_executions te
JOIN chat_sessions s ON s.id = te.session_id
ON CONFLICT (execution_id, owner_epoch) DO NOTHING;

UPDATE chat_turn_executions te
SET generation_id = g.generation_id
FROM chat_execution_generations g
WHERE g.execution_id = te.id
  AND g.owner_epoch = te.owner_epoch
  AND te.generation_id IS NULL;

COMMENT ON TABLE chat_execution_generations IS
    'WP05 stable generation identity keyed by (execution_id, owner_epoch)';
COMMENT ON TABLE chat_commands IS
    'WP05 durable chat command lifecycle; unique idempotency key per session/type';

COMMIT;
