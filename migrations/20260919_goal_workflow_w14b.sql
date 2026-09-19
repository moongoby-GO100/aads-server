-- W-14b bounded grants, delegation lineage, and append-only usage evidence.
-- Additive/idempotent; apply after W-14F stores, W-14a, and M12/M14.
BEGIN;

ALTER TABLE goal_auto_approval_grants
    ADD COLUMN IF NOT EXISTS logical_grant_id UUID,
    ADD COLUMN IF NOT EXISTS root_grant_id UUID,
    ADD COLUMN IF NOT EXISTS delegation_path UUID[] NOT NULL DEFAULT '{}',
    ADD COLUMN IF NOT EXISTS parent_grant_version INTEGER,
    ADD COLUMN IF NOT EXISTS parent_scope_hash TEXT,
    ADD COLUMN IF NOT EXISTS ancestor_revocation_epoch BIGINT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS revocation_epoch BIGINT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS supersedes_grant_id UUID;

UPDATE goal_auto_approval_grants
   SET logical_grant_id=COALESCE(logical_grant_id,id),
       root_grant_id=COALESCE(root_grant_id,id),
       delegation_path=CASE WHEN cardinality(delegation_path)=0 THEN ARRAY[id] ELSE delegation_path END
 WHERE logical_grant_id IS NULL OR root_grant_id IS NULL OR cardinality(delegation_path)=0;

WITH RECURSIVE lineage AS (
    SELECT id,tenant_id,id AS root_id,ARRAY[id] AS path,0 AS depth
      FROM goal_auto_approval_grants WHERE parent_grant_id IS NULL
    UNION ALL
    SELECT child.id,child.tenant_id,parent.root_id,parent.path || child.id,parent.depth+1
      FROM goal_auto_approval_grants child JOIN lineage parent
        ON parent.id=child.parent_grant_id AND parent.tenant_id=child.tenant_id
)
UPDATE goal_auto_approval_grants grant_row
   SET root_grant_id=lineage.root_id,delegation_path=lineage.path
  FROM lineage WHERE grant_row.id=lineage.id AND grant_row.tenant_id=lineage.tenant_id;

ALTER TABLE goal_auto_approval_grants
    ALTER COLUMN logical_grant_id SET NOT NULL,
    ALTER COLUMN root_grant_id SET NOT NULL;

CREATE TABLE IF NOT EXISTS goal_auto_approval_usage_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    use_id UUID NOT NULL REFERENCES goal_auto_approval_uses(id) ON DELETE RESTRICT,
    execution_key TEXT NOT NULL,
    grant_id UUID NOT NULL,
    grant_version INTEGER NOT NULL,
    event_type TEXT NOT NULL CHECK (event_type IN
        ('reserved','executing','completed','failed','budget_overrun','reconciliation_required','reconciled')),
    sequence_no BIGINT NOT NULL CHECK (sequence_no > 0),
    estimated_budget JSONB NOT NULL DEFAULT '{}',
    actual_budget JSONB NOT NULL DEFAULT '{}',
    diagnostics JSONB NOT NULL DEFAULT '{}',
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT transaction_timestamp(),
    UNIQUE (tenant_id, execution_key, sequence_no),
    FOREIGN KEY (tenant_id, grant_id, grant_version)
        REFERENCES goal_auto_approval_grants(tenant_id,id,grant_version) ON DELETE RESTRICT
);

CREATE OR REPLACE FUNCTION aads_usage_event_append_only() RETURNS TRIGGER
LANGUAGE plpgsql AS $$ BEGIN
    RAISE EXCEPTION 'grant usage events are append-only' USING ERRCODE='55000';
END $$;
DROP TRIGGER IF EXISTS trg_goal_usage_event_append_only ON goal_auto_approval_usage_events;
CREATE TRIGGER trg_goal_usage_event_append_only BEFORE UPDATE OR DELETE ON goal_auto_approval_usage_events
FOR EACH ROW EXECUTE FUNCTION aads_usage_event_append_only();

CREATE OR REPLACE FUNCTION aads_prepare_grant_lineage() RETURNS TRIGGER
LANGUAGE plpgsql AS $$
DECLARE parent_row goal_auto_approval_grants%ROWTYPE;
BEGIN
    NEW.logical_grant_id := COALESCE(NEW.logical_grant_id,NEW.id);
    IF NEW.parent_grant_id IS NULL THEN
        NEW.root_grant_id := COALESCE(NEW.root_grant_id,NEW.id);
        NEW.delegation_path := ARRAY[NEW.id];
        RETURN NEW;
    END IF;
    SELECT * INTO parent_row FROM goal_auto_approval_grants
     WHERE tenant_id=NEW.tenant_id AND id=NEW.parent_grant_id FOR KEY SHARE;
    IF NOT FOUND OR parent_row.parent_grant_id IS NOT NULL OR parent_row.delegation_depth<>1 THEN
        RAISE EXCEPTION 'delegation depth exceeded' USING ERRCODE='23514';
    END IF;
    IF NEW.id=ANY(parent_row.delegation_path) THEN
        RAISE EXCEPTION 'delegation cycle' USING ERRCODE='23514';
    END IF;
    IF parent_row.project<>NEW.project OR parent_row.goal_id<>NEW.goal_id
       OR NOT NEW.actions <@ parent_row.actions OR NOT NEW.environments <@ parent_row.environments
       OR NOT NEW.tool_groups <@ parent_row.tool_groups OR NEW.max_risk_tier>parent_row.max_risk_tier
       OR NEW.max_executions>parent_row.max_executions-parent_row.used_executions
       OR NEW.max_files>parent_row.max_files OR NEW.max_rows>parent_row.max_rows
       OR NEW.max_cost_usd>parent_row.max_cost_usd OR NEW.max_parallel>parent_row.max_parallel
       OR NEW.max_duration_seconds>parent_row.max_duration_seconds THEN
        RAISE EXCEPTION 'delegation scope exceeded' USING ERRCODE='23514';
    END IF;
    IF NEW.issued_by<>parent_row.principal_session_id OR NEW.principal_session_id=parent_row.issued_by THEN
        RAISE EXCEPTION 'delegation separation of duties' USING ERRCODE='23514';
    END IF;
    NEW.root_grant_id := parent_row.root_grant_id;
    NEW.delegation_path := parent_row.delegation_path || NEW.id;
    NEW.parent_grant_version := parent_row.grant_version;
    NEW.parent_scope_hash := parent_row.scope_hash;
    NEW.ancestor_revocation_epoch := parent_row.revocation_epoch;
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS trg_prepare_grant_lineage ON goal_auto_approval_grants;
CREATE TRIGGER trg_prepare_grant_lineage BEFORE INSERT ON goal_auto_approval_grants
FOR EACH ROW EXECUTE FUNCTION aads_prepare_grant_lineage();

COMMIT;
