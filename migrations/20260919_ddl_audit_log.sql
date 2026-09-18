-- 2026-09-19 파괴적 DDL 추적 — 이벤트 트리거 기반 감사 로그
--
-- 배경: aads DB 의 yeoljeong_* 28테이블(31,248행)이 제거됐을 때 postgres 쪽에는
--       아무 흔적도 남지 않았다. log_statement=none 이었고, ddl 로 바꿔도 이
--       컨테이너는 stderr 가 docker 로그 드라이버로 흐르지 않아(docker logs 0줄)
--       관측되지 않는다. logging_collector 를 켜려면 postmaster 재시작이 필요하고
--       재시작은 채팅 전체를 끊으므로(R-DOCKER), 재시작 없이 DDL 을 DB 안에
--       남기는 이벤트 트리거로 대신한다.
--
-- 되돌리기: DROP EVENT TRIGGER trg_ddl_audit_command_end, trg_ddl_audit_drop;

CREATE TABLE IF NOT EXISTS ddl_audit_log (
    id              bigserial PRIMARY KEY,
    event_time      timestamptz NOT NULL DEFAULT now(),
    db_user         text        NOT NULL DEFAULT current_user,
    app_name        text,
    client_addr     inet,
    event           text        NOT NULL,
    command_tag     text,
    object_type     text,
    object_identity text,
    query           text
);

CREATE INDEX IF NOT EXISTS idx_ddl_audit_log_time ON ddl_audit_log (event_time DESC);
CREATE INDEX IF NOT EXISTS idx_ddl_audit_log_tag ON ddl_audit_log (command_tag);

-- 잡음 제외: 요청마다 만들어졌다 사라지는 wave_gate_* 스키마와 임시/토스트 객체.
-- 이것들을 남기면 감사 로그가 초당 수십 행으로 불어나 정작 볼 것을 못 본다.
CREATE OR REPLACE FUNCTION ddl_audit_is_noise(p_schema text, p_identity text)
RETURNS boolean
LANGUAGE sql
IMMUTABLE
AS $$
    SELECT coalesce(p_schema, '') LIKE 'wave_gate%'
        OR coalesce(p_schema, '') LIKE 'pg_temp%'
        OR coalesce(p_schema, '') LIKE 'pg_toast%'
        OR coalesce(p_identity, '') LIKE 'wave_gate%'
$$;

CREATE OR REPLACE FUNCTION ddl_audit_command_end()
RETURNS event_trigger
LANGUAGE plpgsql
AS $$
DECLARE
    r record;
BEGIN
    FOR r IN SELECT * FROM pg_event_trigger_ddl_commands() LOOP
        CONTINUE WHEN ddl_audit_is_noise(r.schema_name, r.object_identity);
        INSERT INTO ddl_audit_log (
            event, command_tag, object_type, object_identity,
            query, app_name, client_addr
        ) VALUES (
            'ddl_command_end', r.command_tag, r.object_type, r.object_identity,
            current_query(), current_setting('application_name', true), inet_client_addr()
        );
    END LOOP;
END;
$$;

CREATE OR REPLACE FUNCTION ddl_audit_drop()
RETURNS event_trigger
LANGUAGE plpgsql
AS $$
DECLARE
    r record;
BEGIN
    FOR r IN SELECT * FROM pg_event_trigger_dropped_objects() LOOP
        CONTINUE WHEN NOT r.original AND NOT r.normal;
        CONTINUE WHEN ddl_audit_is_noise(r.schema_name, r.object_identity);
        INSERT INTO ddl_audit_log (
            event, command_tag, object_type, object_identity,
            query, app_name, client_addr
        ) VALUES (
            'sql_drop', tg_tag, r.object_type, r.object_identity,
            current_query(), current_setting('application_name', true), inet_client_addr()
        );
    END LOOP;
END;
$$;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_event_trigger WHERE evtname = 'trg_ddl_audit_command_end') THEN
        CREATE EVENT TRIGGER trg_ddl_audit_command_end
            ON ddl_command_end
            EXECUTE FUNCTION ddl_audit_command_end();
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_event_trigger WHERE evtname = 'trg_ddl_audit_drop') THEN
        CREATE EVENT TRIGGER trg_ddl_audit_drop
            ON sql_drop
            EXECUTE FUNCTION ddl_audit_drop();
    END IF;
END;
$$;
