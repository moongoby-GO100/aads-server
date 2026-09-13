-- 178_chat_outbox_noop_suppression_and_retention.sql
-- AADS Chat Modernization WP04 후속 — chat_outbox 폭주 차단
--
-- 배경(2026-09-13 실측):
--   migration 174가 만든 AFTER UPDATE OF ... 트리거는 SET 목록에 컬럼이 들어 있으면
--   값이 동일해도 발화한다. 유지보수 스윕이 동일 값을 반복 기록하는 동안
--   chat_outbox가 13.5시간 만에 942,611행 / 698MB까지 증가했고(소비자 0건),
--   단일 메시지 하나가 1,489개의 이벤트를 만들어냈다.
--   BEGIN; UPDATE chat_messages SET is_hidden = is_hidden, content = content ...; 로
--   검증한 결과 content_version은 그대로인데 outbox만 +1 되는 것을 확인했다.
--
-- 이 마이그레이션:
--   1) aads_chat_record_revision()에 일반 no-op 가드를 추가한다.
--      추적 컬럼 값이 실제로 바뀌지 않은 UPDATE는 revision/outbox를 만들지 않는다.
--   2) chat_outbox 보존 함수(aads_chat_outbox_prune)를 추가한다.
--      read model은 oldest_outbox_revision보다 오래된 커서를 snapshot 요구로
--      자동 폴백하므로(chat_read_model.py:847), 오래된 이벤트 삭제는 안전하다.
--
-- 영향 범위: 트리거 함수 교체 — 테이블 ACCESS EXCLUSIVE 잠금 없음, 앱 재배포 불필요.
-- 롤백: migration 174의 aads_chat_record_revision() 본문을 다시 적용하고
--       DROP FUNCTION public.aads_chat_outbox_prune(INTERVAL, INTEGER, INTEGER);

BEGIN;

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

    -- (178) no-op 억제: 추적 컬럼이 실제로 바뀌지 않은 UPDATE는 이벤트를 만들지 않는다.
    -- AFTER UPDATE OF 는 값이 같아도 SET 목록에 있으면 발화하므로, 유지보수 스윕의
    -- 동일값 재기록이 outbox를 무한 증식시키는 경로를 여기서 끊는다.
    IF TG_OP = 'UPDATE' THEN
        IF TG_TABLE_NAME = 'chat_messages' THEN
            IF ROW(
                NEW.role, NEW.content, NEW.model_used, NEW.intent, NEW.bookmarked,
                NEW.attachments, NEW.sources, NEW.artifact_id, NEW.execution_id,
                NEW.is_hidden, NEW.deleted_at, NEW.tools_called, NEW.quality_details
            ) IS NOT DISTINCT FROM ROW(
                OLD.role, OLD.content, OLD.model_used, OLD.intent, OLD.bookmarked,
                OLD.attachments, OLD.sources, OLD.artifact_id, OLD.execution_id,
                OLD.is_hidden, OLD.deleted_at, OLD.tools_called, OLD.quality_details
            ) THEN
                RETURN NULL;
            END IF;
        ELSIF TG_TABLE_NAME = 'chat_sessions' THEN
            IF ROW(
                NEW.title, NEW.summary, NEW.pinned, NEW.tags, NEW.current_model,
                NEW.role_key, NEW.current_execution_id, NEW.message_count
            ) IS NOT DISTINCT FROM ROW(
                OLD.title, OLD.summary, OLD.pinned, OLD.tags, OLD.current_model,
                OLD.role_key, OLD.current_execution_id, OLD.message_count
            ) THEN
                RETURN NULL;
            END IF;
        ELSIF TG_TABLE_NAME = 'chat_turn_executions' THEN
            IF ROW(
                NEW.status, NEW.assistant_message_id, NEW.requested_model,
                NEW.actual_model, NEW.last_event_id, NEW.error_message,
                NEW.owner_instance, NEW.owner_epoch, NEW.completed_at
            ) IS NOT DISTINCT FROM ROW(
                OLD.status, OLD.assistant_message_id, OLD.requested_model,
                OLD.actual_model, OLD.last_event_id, OLD.error_message,
                OLD.owner_instance, OLD.owner_epoch, OLD.completed_at
            ) THEN
                RETURN NULL;
            END IF;
        ELSIF TG_TABLE_NAME = 'chat_artifacts' THEN
            IF (to_jsonb(NEW) - 'updated_at')
               IS NOT DISTINCT FROM (to_jsonb(OLD) - 'updated_at') THEN
                RETURN NULL;
            END IF;
        END IF;
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

-- chat_outbox 보존정책.
-- read model은 커서가 보존 구간보다 오래되면 snapshot_required로 폴백하므로
-- 오래된 이벤트 삭제는 클라이언트 정합성을 깨지 않는다.
CREATE OR REPLACE FUNCTION public.aads_chat_outbox_prune(
    p_retention INTERVAL DEFAULT INTERVAL '2 hours',
    p_batch INTEGER DEFAULT 20000,
    p_max_batches INTEGER DEFAULT 50
)
RETURNS BIGINT
LANGUAGE plpgsql
SET search_path = pg_catalog, public
AS $$
DECLARE
    v_deleted BIGINT := 0;
    v_round INTEGER := 0;
    v_rows INTEGER;
BEGIN
    LOOP
        v_round := v_round + 1;
        EXIT WHEN v_round > p_max_batches;

        DELETE FROM chat_outbox
        WHERE ctid IN (
            SELECT ctid
            FROM chat_outbox
            WHERE created_at < NOW() - p_retention
            LIMIT p_batch
        );
        GET DIAGNOSTICS v_rows = ROW_COUNT;
        v_deleted := v_deleted + v_rows;
        EXIT WHEN v_rows = 0;
    END LOOP;

    RETURN v_deleted;
END;
$$;

COMMENT ON FUNCTION public.aads_chat_outbox_prune(INTERVAL, INTEGER, INTEGER) IS
    'chat_outbox 보존 스윕. 보존 구간보다 오래된 이벤트를 배치 삭제하고 삭제 행 수를 반환한다. (178)';

COMMIT;
