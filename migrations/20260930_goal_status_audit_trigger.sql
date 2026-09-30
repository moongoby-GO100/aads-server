-- AADS-GOAL-STATUS-AUDIT-DB-TRIGGER-20260930
-- goals.status 감사기록을 DB 트리거로 일원화한다.
-- 2026-09-30 goal_status_audit 는 2건뿐이었고 전부 source='update_goal' 이었다.
-- 자동경로(goal_manager 자동 승격·goal_failure_retry·temporal_controller 등)는 기록 없이
-- 지나갔다 — 15:53 KST 목표 e1689c52 의 draft→active 승격이 흔적 없이 남았다.
-- 함수마다 훅을 다는 대신 goals 테이블 레벨에서 모든 status 전이를 잡는다.
--
-- (A) goal_status_audit_capture: AFTER UPDATE OF status ON goals → 감사 1행.
--     actor/source 는 세션 GUC aads.audit_actor / aads.audit_source 로 넘길 수 있다
--     (SET LOCAL aads.audit_source = '...'). 없으면 actor NULL, source 'db_trigger'.
-- (B) goal_status_audit_merge: 애플리케이션이 UPDATE 직후 같은 전이를 또 INSERT 하면
--     (goal_manager._record_goal_status_audit) 트리거가 만든 행에 actor/source/note 를 합치고
--     중복 INSERT 는 버린다. 결과는 어느 경로든 전이 1회에 1행.
--     트리거 자신이 넣는 행(pg_trigger_depth() > 1)은 병합하지 않는다 — UPDATE 로 생긴
--     전이는 전부 실제 전이이므로 5초 안에 같은 전이가 반복돼도(active→blocked→active→blocked)
--     각각 남아야 한다.
--
-- 어느 트리거도 예외로 goals UPDATE / 애플리케이션 INSERT 를 죽이지 않는다(WARNING 만 남김).
-- 멱등: 여러 번 적용해도 안전하다. goal_status_audit 테이블·데이터는 건드리지 않는다.
-- 롤백: migrations/rollback/20260930_goal_status_audit_trigger.down.sql

CREATE OR REPLACE FUNCTION goal_status_audit_capture() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO goal_status_audit
        (goal_id, old_status, new_status, changed_at, actor, source, tenant_id, note)
    VALUES (
        NEW.id,
        OLD.status,
        NEW.status,
        now(),
        NULLIF(current_setting('aads.audit_actor', true), ''),
        COALESCE(NULLIF(current_setting('aads.audit_source', true), ''), 'db_trigger'),
        NEW.tenant_id,
        NULL
    );
    RETURN NULL;
EXCEPTION WHEN others THEN
    RAISE WARNING 'goal_status_audit_capture failed goal_id=% %->%: %',
        NEW.id, OLD.status, NEW.status, SQLERRM;
    RETURN NULL;
END;
$$;

CREATE OR REPLACE FUNCTION goal_status_audit_merge() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    existing_id BIGINT;
BEGIN
    -- 트리거가 넣는 행은 그대로 넣는다. 병합 대상은 애플리케이션이 직접 넣는 행뿐이다.
    IF pg_trigger_depth() > 1 THEN
        RETURN NEW;
    END IF;

    SELECT a.id INTO existing_id
      FROM goal_status_audit a
     WHERE a.goal_id = NEW.goal_id
       AND a.old_status IS NOT DISTINCT FROM NEW.old_status
       AND a.new_status IS NOT DISTINCT FROM NEW.new_status
       AND a.changed_at > now() - interval '5 seconds'
     ORDER BY a.changed_at DESC, a.id DESC
     LIMIT 1
     FOR UPDATE;

    IF existing_id IS NULL THEN
        RETURN NEW;
    END IF;

    UPDATE goal_status_audit a
       SET actor     = COALESCE(NEW.actor, a.actor),
           source    = CASE WHEN a.source IS NULL OR a.source = 'db_trigger'
                            THEN COALESCE(NEW.source, a.source)
                            ELSE a.source END,
           note      = COALESCE(NEW.note, a.note),
           tenant_id = COALESCE(a.tenant_id, NEW.tenant_id)
     WHERE a.id = existing_id;

    RETURN NULL;
EXCEPTION WHEN others THEN
    RAISE WARNING 'goal_status_audit_merge failed goal_id=%: %', NEW.goal_id, SQLERRM;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE TRIGGER trg_goal_status_audit_capture
    AFTER UPDATE OF status ON goals
    FOR EACH ROW
    WHEN (OLD.status IS DISTINCT FROM NEW.status)
    EXECUTE FUNCTION goal_status_audit_capture();

CREATE OR REPLACE TRIGGER trg_goal_status_audit_merge
    BEFORE INSERT ON goal_status_audit
    FOR EACH ROW
    EXECUTE FUNCTION goal_status_audit_merge();
