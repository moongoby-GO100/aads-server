-- 재작업 실제 실행 장부 + 원자 차감 함수 (AADS-GOAL-RETRY-EXECUTION-ACCOUNTING-IMPLEMENT-20261014)
--
-- 제안(retry_candidates)과 실제 실행을 분리한다. 실행 한도는 (tenant, milestone, lineage) 당 2회이며
-- lineage 는 재작업 사슬의 뿌리 failed_task_id 다. 차감은 러너가 실제 모델 CLI 를 띄우기 직전에 한 번,
-- job_id 로 멱등하게 일어난다. 대기·claim·릴레이 슬롯·의존성 대기·취소·만료·승인만·제출 거절은 행을 만들지 않는다.
--
-- 한도는 UNIQUE 제약으로 강제한다(락이 없어도 초과 불가):
--   UNIQUE (tenant_id, job_id)                      같은 job 의 중복 차감 방지
--   UNIQUE (tenant_id, milestone_id, lineage, slot) slot 은 1..2 만 허용 → 계보당 최대 2행
-- 가산 전용·반복 적용 가능. 기존 테이블·행은 건드리지 않는다.
-- 롤백: migrations/rollback/20261014_goal_retry_execution_ledger.down.sql
CREATE TABLE IF NOT EXISTS goal_retry_executions (
    id           bigserial PRIMARY KEY,
    tenant_id    uuid        NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    milestone_id uuid        NOT NULL REFERENCES milestones(id) ON DELETE CASCADE,
    lineage      text        NOT NULL,
    job_id       text        NOT NULL,
    slot         smallint    NOT NULL CHECK (slot BETWEEN 1 AND 2),
    charged_at   timestamptz NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_goal_retry_executions_job UNIQUE (tenant_id, job_id),
    CONSTRAINT uq_goal_retry_executions_slot UNIQUE (tenant_id, milestone_id, lineage, slot)
);

CREATE INDEX IF NOT EXISTS idx_goal_retry_executions_milestone
    ON goal_retry_executions (tenant_id, milestone_id);

-- 반환 outcome:
--   not_retry        RETRY_OF_LINK 줄이 없다 → 재작업이 아니므로 차감 대상 아님
--   job_not_found    job 행이 없다
--   tenant_missing   재작업 표식은 있으나 job 에 tenant 가 없다 (fail closed 대상)
--   milestone_missing 재작업 표식은 있으나 마일스톤을 특정할 수 없다 (fail closed 대상)
--   scope_mismatch   표식의 마일스톤이 job 의 tenant 에 없다 (fail closed 대상)
--   charged          이번 호출이 새로 차감했다
--   already_charged  같은 job 이 이미 차감돼 있다 (멱등)
--   cap_exceeded     계보 한도를 이미 다 썼다 → 모델을 띄우면 안 된다
CREATE OR REPLACE FUNCTION charge_goal_retry_execution(p_job_id text, p_cap integer DEFAULT 2)
RETURNS jsonb AS $fn$
DECLARE
    v_cap       integer := LEAST(2, GREATEST(0, COALESCE(p_cap, 2)));
    v_inst      text;
    v_tenant    uuid;
    v_link      text;
    v_ms_text   text;
    v_ms        uuid;
    v_root      text;
    v_parent    text;
    v_hops      integer := 0;
    v_slot      integer;
    v_rows      integer;
    v_used      integer;
BEGIN
    SELECT p.instruction, p.tenant_id INTO v_inst, v_tenant
      FROM pipeline_jobs p WHERE p.job_id = p_job_id;
    IF NOT FOUND THEN
        RETURN jsonb_build_object('outcome', 'job_not_found');
    END IF;

    v_link := substring(COALESCE(v_inst, '') from '(?m)^RETRY_OF_LINK: (\S+)[ \t\r]*$');
    IF v_link IS NULL THEN
        RETURN jsonb_build_object('outcome', 'not_retry');
    END IF;
    IF v_tenant IS NULL THEN
        RETURN jsonb_build_object('outcome', 'tenant_missing', 'lineage', v_link);
    END IF;

    v_ms_text := substring(v_inst from '(?m)^MILESTONE_ID: ([0-9a-fA-F-]{36})[ \t\r]*$');
    IF v_ms_text IS NULL THEN
        SELECT l.milestone_id::text INTO v_ms_text
          FROM goal_task_links l
         WHERE l.task_id = p_job_id AND l.tenant_id = v_tenant AND l.milestone_id IS NOT NULL
         ORDER BY l.created_at DESC NULLS LAST LIMIT 1;
    END IF;
    IF v_ms_text IS NULL THEN
        RETURN jsonb_build_object('outcome', 'milestone_missing', 'lineage', v_link);
    END IF;
    v_ms := v_ms_text::uuid;
    PERFORM 1 FROM milestones m WHERE m.id = v_ms AND m.tenant_id = v_tenant;
    IF NOT FOUND THEN
        RETURN jsonb_build_object('outcome', 'scope_mismatch', 'lineage', v_link);
    END IF;

    -- 재작업이 다시 실패해 그 job 을 가리키는 새 재작업이 생겨도 같은 계보다.
    v_root := v_link;
    LOOP
        EXIT WHEN v_hops >= 5;
        SELECT substring(COALESCE(p.instruction, '') from '(?m)^RETRY_OF_LINK: (\S+)[ \t\r]*$')
          INTO v_parent
          FROM pipeline_jobs p WHERE p.job_id = v_root AND p.tenant_id = v_tenant;
        EXIT WHEN v_parent IS NULL OR v_parent = v_root OR v_parent = p_job_id;
        v_root := v_parent;
        v_hops := v_hops + 1;
    END LOOP;

    PERFORM pg_advisory_xact_lock(hashtextextended('goal_retry_exec:' || v_tenant::text || ':' || p_job_id, 0));

    FOR v_slot IN 1..v_cap LOOP
        IF EXISTS (SELECT 1 FROM goal_retry_executions e
                    WHERE e.tenant_id = v_tenant AND e.job_id = p_job_id) THEN
            EXIT;
        END IF;
        INSERT INTO goal_retry_executions (tenant_id, milestone_id, lineage, job_id, slot)
        VALUES (v_tenant, v_ms, v_root, p_job_id, v_slot)
        ON CONFLICT DO NOTHING;
        GET DIAGNOSTICS v_rows = ROW_COUNT;
        IF v_rows = 1 THEN
            RETURN jsonb_build_object('outcome', 'charged', 'lineage', v_root,
                                      'milestone_id', v_ms, 'slot', v_slot, 'cap', v_cap);
        END IF;
    END LOOP;

    IF EXISTS (SELECT 1 FROM goal_retry_executions e
                WHERE e.tenant_id = v_tenant AND e.job_id = p_job_id) THEN
        RETURN jsonb_build_object('outcome', 'already_charged', 'lineage', v_root,
                                  'milestone_id', v_ms, 'cap', v_cap);
    END IF;

    SELECT count(*) INTO v_used FROM goal_retry_executions e
     WHERE e.tenant_id = v_tenant AND e.milestone_id = v_ms AND e.lineage = v_root;
    RETURN jsonb_build_object('outcome', 'cap_exceeded', 'lineage', v_root,
                              'milestone_id', v_ms, 'used', v_used, 'cap', v_cap);
END
$fn$ LANGUAGE plpgsql;
