WITH scope AS (
    SELECT '2d701a8c-9596-4757-8588-faa4f7837112'::uuid AS tenant_id,
           'cf1ec2f6-0072-4f85-aa5e-b08760cd6613'::uuid AS goal_id,
           'c83dd908-9351-4a84-b9cf-96f4d221a29b'::uuid AS milestone_id
), canonical_tree AS (
    SELECT w.* FROM work_items w JOIN scope s
      ON w.tenant_id=s.tenant_id AND w.goal_id=s.goal_id
     AND w.milestone_id=s.milestone_id
    WHERE w.status <> 'cancelled'
), live_milestone_duplicates AS (
    SELECT m.goal_id,m.sequence_order,lower(btrim(m.title)),COALESCE(m.variant,'')
      FROM milestones m JOIN scope s ON m.goal_id=s.goal_id
     WHERE m.status NOT IN ('cancelled','superseded','archived')
     GROUP BY m.goal_id,m.sequence_order,lower(btrim(m.title)),COALESCE(m.variant,'')
    HAVING count(*) > 1
)
SELECT
    (SELECT count(*) FROM live_milestone_duplicates) AS live_duplicate_groups,
    (SELECT count(*) FROM milestones m JOIN scope s ON m.id=s.milestone_id
      WHERE m.status='in_progress' AND m.sequence_order=22) AS canonical_m16,
    (SELECT count(*) FROM milestones
      WHERE id='0f97ce3d-45c4-411c-ab08-35ba5aea21d2'::uuid
        AND status='superseded') AS m10_history,
    (SELECT count(*) FROM work_items w JOIN scope s
      ON w.tenant_id=s.tenant_id AND w.goal_id=s.goal_id
      WHERE w.idempotency_key LIKE 'goal-v12-backfill:%' AND w.status<>'cancelled')
      AS active_synthetic,
    (SELECT count(*) FROM canonical_tree WHERE type='epic') AS epics,
    (SELECT count(*) FROM canonical_tree WHERE type='story') AS stories,
    (SELECT count(*) FROM canonical_tree WHERE type='task') AS tasks,
    (SELECT count(*) FROM canonical_tree WHERE assignment_id IS NULL) AS unassigned,
    (SELECT count(*) FROM canonical_tree
      WHERE jsonb_typeof(acceptance_criteria)<>'array'
         OR jsonb_array_length(acceptance_criteria)=0) AS missing_criteria;
