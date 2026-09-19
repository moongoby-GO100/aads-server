-- W-14F decision authentication and executor revocation fences. Additive/idempotent.
BEGIN;

ALTER TABLE goal_policy_decisions
    ADD COLUMN IF NOT EXISTS signature_key_id TEXT,
    ADD COLUMN IF NOT EXISTS signature_key_version INTEGER,
    ADD COLUMN IF NOT EXISTS ancestor_revocation_epoch BIGINT NOT NULL DEFAULT 0;

ALTER TABLE project_role_assignments
    ADD COLUMN IF NOT EXISTS assignment_epoch BIGINT NOT NULL DEFAULT 1;
ALTER TABLE goal_auto_approval_grants
    ADD COLUMN IF NOT EXISTS revocation_epoch BIGINT NOT NULL DEFAULT 0;

CREATE OR REPLACE FUNCTION aads_bump_assignment_epoch() RETURNS TRIGGER
LANGUAGE plpgsql AS $$ BEGIN
    IF ROW(OLD.active,OLD.session_id,OLD.role_key) IS DISTINCT FROM
       ROW(NEW.active,NEW.session_id,NEW.role_key) THEN
        NEW.assignment_epoch := OLD.assignment_epoch + 1;
    END IF;
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS trg_bump_assignment_epoch ON project_role_assignments;
CREATE TRIGGER trg_bump_assignment_epoch BEFORE UPDATE ON project_role_assignments
FOR EACH ROW EXECUTE FUNCTION aads_bump_assignment_epoch();

CREATE OR REPLACE FUNCTION aads_bump_grant_revocation_epoch() RETURNS TRIGGER
LANGUAGE plpgsql AS $$ BEGIN
    IF OLD.status IS DISTINCT FROM NEW.status THEN
        NEW.revocation_epoch := OLD.revocation_epoch + 1;
    END IF;
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS trg_bump_grant_revocation_epoch ON goal_auto_approval_grants;
CREATE TRIGGER trg_bump_grant_revocation_epoch BEFORE UPDATE ON goal_auto_approval_grants
FOR EACH ROW EXECUTE FUNCTION aads_bump_grant_revocation_epoch();

DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname='ck_goal_policy_signature_key'
          AND conrelid='goal_policy_decisions'::regclass
    ) THEN
        ALTER TABLE goal_policy_decisions ADD CONSTRAINT ck_goal_policy_signature_key CHECK
            (btrim(signature_key_id) <> '' AND signature_key_version > 0) NOT VALID;
    END IF;
END $$;

CREATE OR REPLACE FUNCTION goal_policy_execution_fences(
    p_decision_id UUID, p_tenant_id UUID, p_target_id UUID, p_assignment_id UUID
) RETURNS TABLE(
    target_version BIGINT, policy_version UUID, precondition_snapshot_hash TEXT,
    kill_switch_epoch BIGINT, deny_policy_epoch BIGINT, assignment_epoch BIGINT,
    grant_revocation_epoch BIGINT, ancestor_revocation_epoch BIGINT,
    kill_switch_active BOOLEAN, policy_active BOOLEAN, assignment_active BOOLEAN,
    grant_chain_active BOOLEAN
) LANGUAGE SQL STABLE SECURITY INVOKER AS $$
    SELECT w.version, COALESCE(current_policy.id,d.policy_version), s.snapshot_hash,
           COALESCE((SELECT max(k.epoch) FROM goal_kill_switches k
                     WHERE k.tenant_id=d.tenant_id AND k.project=d.project AND k.active
                       AND (k.goal_id IS NULL OR k.goal_id=w.goal_id)),0),
           COALESCE((extract(epoch FROM current_policy.created_at) * 1000000)::bigint,0),
           COALESCE(a.assignment_epoch,0),
           COALESCE(g.revocation_epoch,0),
           COALESCE((WITH RECURSIVE ancestors AS (
               SELECT id,parent_grant_id,status,revocation_epoch,ARRAY[id] AS path
                 FROM goal_auto_approval_grants
                WHERE tenant_id=d.tenant_id AND id=d.matched_grant_id
               UNION ALL
               SELECT parent.id,parent.parent_grant_id,parent.status,parent.revocation_epoch,
                      child.path || parent.id
                 FROM goal_auto_approval_grants parent JOIN ancestors child ON parent.id=child.parent_grant_id
                WHERE parent.tenant_id=d.tenant_id AND NOT parent.id=ANY(child.path)
           ) SELECT max(revocation_epoch) FROM ancestors),0),
           EXISTS(SELECT 1 FROM goal_kill_switches k
                   WHERE k.tenant_id=d.tenant_id AND k.project=d.project AND k.active
                     AND (k.goal_id IS NULL OR k.goal_id=w.goal_id)),
           current_policy.id IS NOT NULL,
           a.active AND a.session_id=d.principal_session_id,
           CASE WHEN d.matched_grant_id IS NULL THEN TRUE ELSE
             g.id IS NOT NULL AND g.status='active' AND g.grant_version=d.grant_version
             AND COALESCE((WITH RECURSIVE ancestors AS (
                 SELECT id,parent_grant_id,status,ARRAY[id] AS path
                   FROM goal_auto_approval_grants
                  WHERE tenant_id=d.tenant_id AND id=d.matched_grant_id
                 UNION ALL
                 SELECT parent.id,parent.parent_grant_id,parent.status,child.path || parent.id
                   FROM goal_auto_approval_grants parent
                   JOIN ancestors child ON parent.id=child.parent_grant_id
                  WHERE parent.tenant_id=d.tenant_id AND NOT parent.id=ANY(child.path)
             ) SELECT bool_and(status='active') FROM ancestors),FALSE)
           END
      FROM goal_policy_decisions d
      JOIN work_items w ON w.id=p_target_id AND w.tenant_id=d.tenant_id AND w.project=d.project
      JOIN project_role_assignments a ON a.id=p_assignment_id AND a.tenant_id=d.tenant_id
      LEFT JOIN goal_auto_approval_grants g ON g.id=d.matched_grant_id AND g.tenant_id=d.tenant_id
      LEFT JOIN LATERAL (
          SELECT p.id,p.created_at FROM goal_approval_policy_versions p
           WHERE p.tenant_id=d.tenant_id AND (p.project IS NULL OR p.project=d.project)
             AND p.mode IN ('canary','enabled')
             AND (p.effective_at IS NULL OR p.effective_at <= transaction_timestamp())
           ORDER BY p.effective_at DESC NULLS LAST,p.created_at DESC LIMIT 1
      ) current_policy ON TRUE
      JOIN LATERAL (
          SELECT snapshot_hash FROM goal_precondition_snapshots
           WHERE tenant_id=d.tenant_id AND work_item_id=p_target_id
           ORDER BY snapshot_version DESC LIMIT 1
      ) s ON TRUE
     WHERE d.id=p_decision_id AND d.tenant_id=p_tenant_id;
$$;

COMMIT;
