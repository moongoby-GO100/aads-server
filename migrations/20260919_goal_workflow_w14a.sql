-- W-14a immutable change sets, independent approval routes and transactional outbox.
-- Additive and idempotent; application transactions own all mutation boundaries.
BEGIN;

ALTER TABLE work_item_change_sets
    ADD COLUMN IF NOT EXISTS target_version BIGINT,
    ADD COLUMN IF NOT EXISTS body_hash TEXT,
    ADD COLUMN IF NOT EXISTS risk_factors TEXT[] NOT NULL DEFAULT '{}';

UPDATE work_item_change_sets
   SET target_version=base_version+1,
       body_hash=patch_hash
 WHERE target_version IS NULL OR body_hash IS NULL;

CREATE OR REPLACE FUNCTION aads_fill_change_set_w14a_fields() RETURNS TRIGGER
LANGUAGE plpgsql AS $$ BEGIN
    NEW.target_version := COALESCE(NEW.target_version,NEW.base_version+1);
    -- Compatibility for old trusted writers. The W-14a service always sends
    -- the full semantic body hash; legacy rows retain patch-hash identity.
    NEW.body_hash := COALESCE(NEW.body_hash,NEW.patch_hash);
    RETURN NEW;
END $$;
CREATE OR REPLACE TRIGGER trg_fill_change_set_w14a_fields
BEFORE INSERT ON work_item_change_sets
FOR EACH ROW EXECUTE FUNCTION aads_fill_change_set_w14a_fields();

ALTER TABLE work_item_change_sets
    ALTER COLUMN target_version SET NOT NULL,
    ALTER COLUMN body_hash SET NOT NULL;

DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_change_set_target_version') THEN
        ALTER TABLE work_item_change_sets ADD CONSTRAINT ck_change_set_target_version
            CHECK (target_version > base_version);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_change_set_body_hash') THEN
        ALTER TABLE work_item_change_sets ADD CONSTRAINT ck_change_set_body_hash
            CHECK (body_hash ~ '^sha256:[0-9a-f]{64}$');
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS work_item_change_set_approval_routes (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    project TEXT NOT NULL,
    change_set_id UUID NOT NULL,
    route_key TEXT NOT NULL,
    required_role TEXT NOT NULL,
    approval_request_id UUID NOT NULL REFERENCES agent_permission_requests(id) ON DELETE RESTRICT,
    state TEXT NOT NULL DEFAULT 'pending'
        CHECK (state IN ('pending','approved','rejected')),
    decided_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT fk_change_set_route_scope
        FOREIGN KEY (change_set_id,tenant_id,project)
        REFERENCES work_item_change_sets(id,tenant_id,project) ON DELETE RESTRICT,
    UNIQUE (tenant_id,change_set_id,route_key),
    UNIQUE (tenant_id,approval_request_id),
    UNIQUE (id,tenant_id,project,change_set_id)
);

CREATE TABLE IF NOT EXISTS work_item_change_set_approval_decisions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    project TEXT NOT NULL,
    change_set_id UUID NOT NULL,
    route_id UUID NOT NULL,
    actor_session_id UUID NOT NULL,
    decision TEXT NOT NULL CHECK (decision IN ('approved','rejected')),
    reason TEXT NOT NULL DEFAULT '',
    decided_at TIMESTAMPTZ NOT NULL DEFAULT transaction_timestamp(),
    CONSTRAINT fk_change_set_decision_actor FOREIGN KEY (actor_session_id,tenant_id)
        REFERENCES chat_sessions(id,tenant_id) ON DELETE RESTRICT,
    CONSTRAINT fk_change_set_decision_route FOREIGN KEY
        (route_id,tenant_id,project,change_set_id)
        REFERENCES work_item_change_set_approval_routes(id,tenant_id,project,change_set_id)
        ON DELETE RESTRICT,
    UNIQUE (tenant_id,route_id,actor_session_id)
);

CREATE OR REPLACE FUNCTION aads_change_set_decisions_append_only() RETURNS TRIGGER
LANGUAGE plpgsql AS $$ BEGIN
    RAISE EXCEPTION 'change-set approval decisions are append-only' USING ERRCODE='55000';
END $$;
DROP TRIGGER IF EXISTS trg_change_set_decisions_append_only ON work_item_change_set_approval_decisions;
CREATE TRIGGER trg_change_set_decisions_append_only
BEFORE UPDATE OR DELETE ON work_item_change_set_approval_decisions
FOR EACH ROW EXECUTE FUNCTION aads_change_set_decisions_append_only();

ALTER TABLE goal_workflow_outbox
    DROP CONSTRAINT IF EXISTS goal_workflow_outbox_status_check;
ALTER TABLE goal_workflow_outbox
    ADD CONSTRAINT goal_workflow_outbox_status_check CHECK
        (status IN ('pending','delivering','delivered','failed','reconciliation_required'));

CREATE TABLE IF NOT EXISTS goal_workflow_effects (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    project TEXT NOT NULL,
    execution_key TEXT NOT NULL,
    change_set_id UUID NOT NULL,
    owner_instance TEXT NOT NULL,
    owner_epoch BIGINT NOT NULL CHECK (owner_epoch > 0),
    effect_kind TEXT NOT NULL CHECK (effect_kind IN ('internal','external')),
    state TEXT NOT NULL CHECK
        (state IN ('applying','applied','failed','reconciliation_required')),
    result JSONB NOT NULL DEFAULT '{}'::jsonb,
    applied_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT fk_goal_effect_change_set_scope
        FOREIGN KEY (change_set_id,tenant_id,project)
        REFERENCES work_item_change_sets(id,tenant_id,project) ON DELETE RESTRICT,
    UNIQUE (tenant_id,execution_key)
);

CREATE OR REPLACE FUNCTION aads_guard_change_set_immutable_fields() RETURNS TRIGGER
LANGUAGE plpgsql AS $$ BEGIN
    IF ROW(OLD.tenant_id,OLD.project,OLD.target_type,OLD.target_id,OLD.action,
           OLD.base_version,OLD.target_version,OLD.patch,OLD.patch_hash,OLD.body_hash,
           OLD.rationale,OLD.expected_effect,OLD.rollback_plan,OLD.risk_tier,
           OLD.risk_factors,OLD.idempotency_key,OLD.requested_by,OLD.environment)
       IS DISTINCT FROM
       ROW(NEW.tenant_id,NEW.project,NEW.target_type,NEW.target_id,NEW.action,
           NEW.base_version,NEW.target_version,NEW.patch,NEW.patch_hash,NEW.body_hash,
           NEW.rationale,NEW.expected_effect,NEW.rollback_plan,NEW.risk_tier,
           NEW.risk_factors,NEW.idempotency_key,NEW.requested_by,NEW.environment) THEN
        RAISE EXCEPTION 'change-set request fields are immutable' USING ERRCODE='55000';
    END IF;
    RETURN NEW;
END $$;
CREATE OR REPLACE TRIGGER trg_change_set_immutable_fields
BEFORE UPDATE ON work_item_change_sets
FOR EACH ROW EXECUTE FUNCTION aads_guard_change_set_immutable_fields();

DO $$
DECLARE table_name TEXT;
BEGIN
    FOREACH table_name IN ARRAY ARRAY[
        'work_item_change_set_approval_routes',
        'work_item_change_set_approval_decisions',
        'goal_workflow_effects'
    ] LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', table_name);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', table_name);
        IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE schemaname=current_schema()
                       AND tablename=table_name AND policyname='tenant_isolation') THEN
            EXECUTE format(
                'CREATE POLICY tenant_isolation ON %I USING (tenant_id=aads_current_tenant_id()) WITH CHECK (tenant_id=aads_current_tenant_id())',
                table_name
            );
        END IF;
    END LOOP;
END $$;

CREATE INDEX IF NOT EXISTS ix_goal_outbox_claim
    ON goal_workflow_outbox(status,available_at,created_at)
    WHERE status IN ('pending','failed');

COMMIT;
