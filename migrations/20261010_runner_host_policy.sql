-- 서버(러너 호스트)별 실행 정책 테이블 + 변경 감사 (AADS-RUNNER-LR04-HEAVY-LANE-20261010, PRD R2 LR03/LR04)
--
-- 행이 없는 호스트는 지금까지의 env(MAX_CONCURRENT_SERVER 등) 동작 그대로다. 이 마이그레이션은
-- 운영 슬롯 값을 넣지 않는다 — 테이블과 감사 트리거만 만든다.
--   max_concurrent        러너가 사이클마다 읽어 MAX_CONCURRENT_SERVER 대신 쓰는 작업 동시 수(1~200)
--   heavy_slots           pytest/npm build/tsc 같은 CPU 큰 명령의 서버 전체 동시 슬롯 수(NULL/0 = 제한 없음)
--   urgent_reserved_slots heavy_slots 중 P0/P1 작업만 쓸 수 있는 슬롯 수
--   low_priority_nice     P0/P1 이 아닌 작업 워커의 nice 값(0~19, NULL = env 그대로)
-- revision 은 트리거가 올린다(클라이언트가 보낸 값은 무시). 변경마다 runner_host_policy_audit 에 이전값·새값이 남는다.
-- 가산 전용·반복 적용 가능. 롤백: migrations/rollback/20261010_runner_host_policy.down.sql
CREATE TABLE IF NOT EXISTS runner_host_policy (
    host                  text PRIMARY KEY,
    max_concurrent        int CHECK (max_concurrent IS NULL OR max_concurrent BETWEEN 1 AND 200),
    heavy_slots           int CHECK (heavy_slots IS NULL OR heavy_slots BETWEEN 0 AND 64),
    urgent_reserved_slots int CHECK (urgent_reserved_slots IS NULL OR urgent_reserved_slots BETWEEN 0 AND 64),
    low_priority_nice     int CHECK (low_priority_nice IS NULL OR low_priority_nice BETWEEN 0 AND 19),
    revision              int NOT NULL DEFAULT 1,
    updated_by            text,
    updated_at            timestamptz NOT NULL DEFAULT NOW(),
    note                  text
);

CREATE TABLE IF NOT EXISTS runner_host_policy_audit (
    id           bigserial PRIMARY KEY,
    host         text NOT NULL,
    action       text NOT NULL,
    old_values   jsonb,
    new_values   jsonb,
    old_revision int,
    new_revision int,
    updated_by   text,
    at           timestamptz NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_runner_host_policy_audit_host_at
    ON runner_host_policy_audit (host, at DESC);

CREATE OR REPLACE FUNCTION runner_host_policy_bump_revision() RETURNS trigger AS $fn$
BEGIN
    NEW.revision := OLD.revision + 1;
    NEW.updated_at := NOW();
    RETURN NEW;
END
$fn$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION runner_host_policy_write_audit() RETURNS trigger AS $fn$
BEGIN
    IF TG_OP = 'INSERT' THEN
        INSERT INTO runner_host_policy_audit (host, action, old_values, new_values, old_revision, new_revision, updated_by)
        VALUES (NEW.host, 'insert', NULL, to_jsonb(NEW), NULL, NEW.revision, NEW.updated_by);
        RETURN NEW;
    ELSIF TG_OP = 'UPDATE' THEN
        IF to_jsonb(OLD) = to_jsonb(NEW) THEN
            RETURN NEW;
        END IF;
        INSERT INTO runner_host_policy_audit (host, action, old_values, new_values, old_revision, new_revision, updated_by)
        VALUES (NEW.host, 'update', to_jsonb(OLD), to_jsonb(NEW), OLD.revision, NEW.revision, NEW.updated_by);
        RETURN NEW;
    END IF;
    INSERT INTO runner_host_policy_audit (host, action, old_values, new_values, old_revision, new_revision, updated_by)
    VALUES (OLD.host, 'delete', to_jsonb(OLD), NULL, OLD.revision, NULL, OLD.updated_by);
    RETURN OLD;
END
$fn$ LANGUAGE plpgsql;

CREATE OR REPLACE TRIGGER trg_runner_host_policy_bump_revision
    BEFORE UPDATE ON runner_host_policy
    FOR EACH ROW WHEN (OLD.* IS DISTINCT FROM NEW.*)
    EXECUTE FUNCTION runner_host_policy_bump_revision();

CREATE OR REPLACE TRIGGER trg_runner_host_policy_audit
    AFTER INSERT OR UPDATE OR DELETE ON runner_host_policy
    FOR EACH ROW EXECUTE FUNCTION runner_host_policy_write_audit();
