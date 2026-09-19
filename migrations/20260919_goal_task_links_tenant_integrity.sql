-- M6: make goal_task_links tenant ownership explicit and DB-enforced.
--
-- Legacy writers may omit tenant_id.  The trigger derives it from the linked
-- goal/milestone, but rejects a caller-supplied cross-tenant value.  This keeps
-- old scheduler paths compatible while preventing direct-SQL tenant mixing.

BEGIN;

ALTER TABLE goal_task_links
    ADD COLUMN IF NOT EXISTS tenant_id UUID;

UPDATE goal_task_links l
   SET tenant_id = COALESCE(
       (SELECT g.tenant_id FROM goals g WHERE g.id = l.goal_id),
       (SELECT m.tenant_id FROM milestones m WHERE m.id = l.milestone_id)
   )
 WHERE l.tenant_id IS NULL;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM goal_task_links WHERE tenant_id IS NULL) THEN
        RAISE EXCEPTION 'goal_task_links tenant backfill incomplete';
    END IF;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS ux_goals_id_tenant
    ON goals (id, tenant_id);
CREATE UNIQUE INDEX IF NOT EXISTS ux_milestones_id_tenant
    ON milestones (id, tenant_id);

CREATE OR REPLACE FUNCTION enforce_goal_task_link_tenant()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    v_goal_tenant UUID;
    v_milestone_tenant UUID;
    v_expected_tenant UUID;
BEGIN
    IF NEW.goal_id IS NOT NULL THEN
        SELECT tenant_id INTO v_goal_tenant FROM goals WHERE id = NEW.goal_id;
        IF v_goal_tenant IS NULL THEN
            RAISE EXCEPTION 'goal_task_links goal not found: %', NEW.goal_id
                USING ERRCODE = '23503';
        END IF;
    END IF;

    IF NEW.milestone_id IS NOT NULL THEN
        SELECT tenant_id INTO v_milestone_tenant FROM milestones WHERE id = NEW.milestone_id;
        IF v_milestone_tenant IS NULL THEN
            RAISE EXCEPTION 'goal_task_links milestone not found: %', NEW.milestone_id
                USING ERRCODE = '23503';
        END IF;
    END IF;

    IF v_goal_tenant IS NOT NULL AND v_milestone_tenant IS NOT NULL
       AND v_goal_tenant <> v_milestone_tenant THEN
        RAISE EXCEPTION 'goal_task_links goal/milestone tenant mismatch'
            USING ERRCODE = '23514';
    END IF;

    v_expected_tenant := COALESCE(v_goal_tenant, v_milestone_tenant);
    IF v_expected_tenant IS NULL THEN
        RAISE EXCEPTION 'goal_task_links requires a scoped goal or milestone'
            USING ERRCODE = '23514';
    END IF;

    IF NEW.tenant_id IS NULL THEN
        NEW.tenant_id := v_expected_tenant;
    ELSIF NEW.tenant_id <> v_expected_tenant THEN
        RAISE EXCEPTION 'goal_task_links tenant mismatch'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS trg_goal_task_links_tenant ON goal_task_links;
CREATE TRIGGER trg_goal_task_links_tenant
    BEFORE INSERT OR UPDATE OF goal_id, milestone_id, tenant_id
    ON goal_task_links
    FOR EACH ROW EXECUTE FUNCTION enforce_goal_task_link_tenant();

ALTER TABLE goal_task_links
    ALTER COLUMN tenant_id SET NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'fk_goal_task_links_tenant'
           AND conrelid = 'goal_task_links'::regclass
    ) THEN
        ALTER TABLE goal_task_links
            ADD CONSTRAINT fk_goal_task_links_tenant
            FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE RESTRICT;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'fk_goal_task_links_goal_tenant'
           AND conrelid = 'goal_task_links'::regclass
    ) THEN
        ALTER TABLE goal_task_links
            ADD CONSTRAINT fk_goal_task_links_goal_tenant
            FOREIGN KEY (goal_id, tenant_id) REFERENCES goals(id, tenant_id)
            ON DELETE CASCADE NOT VALID;
        ALTER TABLE goal_task_links VALIDATE CONSTRAINT fk_goal_task_links_goal_tenant;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'fk_goal_task_links_milestone_tenant'
           AND conrelid = 'goal_task_links'::regclass
    ) THEN
        ALTER TABLE goal_task_links
            ADD CONSTRAINT fk_goal_task_links_milestone_tenant
            FOREIGN KEY (milestone_id, tenant_id) REFERENCES milestones(id, tenant_id)
            ON DELETE CASCADE NOT VALID;
        ALTER TABLE goal_task_links VALIDATE CONSTRAINT fk_goal_task_links_milestone_tenant;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_goal_task_links_tenant_goal
    ON goal_task_links (tenant_id, goal_id, task_type, task_id);

COMMENT ON COLUMN goal_task_links.tenant_id IS
    'Owning tenant, derived from and constrained to the linked goal/milestone.';

COMMIT;
