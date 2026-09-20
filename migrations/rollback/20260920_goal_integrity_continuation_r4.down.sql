-- Compensating rollback: restore only rows captured by this migration.
BEGIN;

DO $$
DECLARE
    v_tenant CONSTANT UUID := '2d701a8c-9596-4757-8588-faa4f7837112';
    v_goal CONSTANT UUID := 'cf1ec2f6-0072-4f85-aa5e-b08760cd6613';
    v_payload JSONB;
BEGIN
    SELECT payload INTO v_payload
      FROM goal_hierarchy_repair_audit
     WHERE tenant_id=v_tenant AND goal_id=v_goal
       AND entry_key='aads-goal-integrity-continuation-r4-20260920'
       AND phase='before';
    IF v_payload IS NULL THEN
        RAISE EXCEPTION 'goal integrity R4 rollback snapshot missing';
    END IF;

    UPDATE work_items w
       SET status=x.status,
           progress=x.progress,
           completed_at=x.completed_at,
           description=x.description,
           version=x.version + 1,
           updated_at=clock_timestamp()
      FROM jsonb_to_recordset(v_payload->'synthetic_items') AS x(
          id UUID, status TEXT, progress NUMERIC, completed_at TIMESTAMPTZ,
          description TEXT, version BIGINT
      )
     WHERE w.id=x.id AND w.tenant_id=v_tenant AND w.goal_id=v_goal;

    UPDATE milestones m
       SET status=x.status,
           review_note=x.review_note,
           criteria_history=x.criteria_history,
           updated_at=clock_timestamp()
      FROM jsonb_to_record(v_payload->'m10') AS x(
          id UUID, status TEXT, review_note TEXT, criteria_history JSONB
      )
     WHERE m.id=x.id AND m.goal_id=v_goal;

    UPDATE goals g
       SET status=x.status,
           progress=x.progress,
           completed_at=x.completed_at,
           updated_at=clock_timestamp()
      FROM jsonb_to_record(v_payload->'goal') AS x(
          id UUID, status TEXT, progress REAL, completed_at TIMESTAMPTZ
      )
     WHERE g.id=x.id AND g.tenant_id=v_tenant;

    WITH RECURSIVE canonical_tree AS (
        SELECT id
          FROM work_items
         WHERE tenant_id=v_tenant AND goal_id=v_goal
           AND idempotency_key LIKE 'goal-integrity-r4:m16:epic:%'
        UNION ALL
        SELECT child.id
          FROM work_items child
          JOIN canonical_tree parent ON child.parent_id=parent.id
         WHERE child.tenant_id=v_tenant AND child.goal_id=v_goal
    )
    UPDATE work_items
       SET status='cancelled', progress=0, completed_at=NULL,
           updated_at=clock_timestamp(), version=version+1
     WHERE id IN (SELECT id FROM canonical_tree);

    INSERT INTO goal_hierarchy_repair_audit
        (tenant_id,project,goal_id,entry_key,phase,payload)
    VALUES (v_tenant,'AADS',v_goal,
            'aads-goal-integrity-continuation-r4-20260920','rollback',
            jsonb_build_object('at',clock_timestamp()))
    ON CONFLICT (tenant_id,goal_id,entry_key,phase) DO NOTHING;
END $$;

COMMIT;
