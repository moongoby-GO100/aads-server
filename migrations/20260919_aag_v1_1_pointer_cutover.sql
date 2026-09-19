-- AAG v1.1 V11-5 phase 2: repair the direct-publisher pointer gap.
-- Only verified authoritative ready observations may become latest.

WITH ranked AS (
    SELECT o.*,
           row_number() OVER (
               PARTITION BY o.project,o.repository_id,o.target_ref,o.governance_scope
               ORDER BY o.verified_at DESC,o.id DESC
           ) AS rn
      FROM aag_snapshot_observations o
      JOIN aag_graph_snapshots_v2 s ON s.id=o.snapshot_id
     WHERE o.authoritative=TRUE AND o.verification_status='verified'
       AND s.publish_status='ready'
), chosen AS (
    SELECT * FROM ranked WHERE rn=1
),
upsert_pointer AS (
INSERT INTO aag_latest_pointers
    (project,repository_id,target_ref,governance_scope,snapshot_id,observation_id,
     run_id,resolved_commit_sha,expected_target_ref_head_sha,generated_at,verified_at,updated_at)
SELECT c.project,c.repository_id,c.target_ref,c.governance_scope,c.snapshot_id,c.id,
       c.run_id,c.resolved_commit_sha,c.expected_target_ref_head_sha,s.generated_at,
       c.verified_at,NOW()
  FROM chosen c JOIN aag_graph_snapshots_v2 s ON s.id=c.snapshot_id
ON CONFLICT (project,repository_id,target_ref,governance_scope) DO UPDATE SET
    snapshot_id=EXCLUDED.snapshot_id,
    observation_id=EXCLUDED.observation_id,
    run_id=EXCLUDED.run_id,
    resolved_commit_sha=EXCLUDED.resolved_commit_sha,
    expected_target_ref_head_sha=EXCLUDED.expected_target_ref_head_sha,
    generated_at=EXCLUDED.generated_at,
    verified_at=EXCLUDED.verified_at,
    updated_at=NOW()
WHERE EXCLUDED.verified_at >= aag_latest_pointers.verified_at
RETURNING project
)
INSERT INTO aag_ref_heads
    (project,repository_id,target_ref,governance_scope,head_commit_sha,
     generated_at,verified_at,observation_id,updated_at)
SELECT c.project,c.repository_id,c.target_ref,c.governance_scope,c.resolved_commit_sha,
       s.generated_at,c.verified_at,c.id,NOW()
  FROM chosen c JOIN aag_graph_snapshots_v2 s ON s.id=c.snapshot_id
ON CONFLICT (project,repository_id,target_ref,governance_scope) DO UPDATE SET
    head_commit_sha=EXCLUDED.head_commit_sha,
    generated_at=EXCLUDED.generated_at,
    verified_at=EXCLUDED.verified_at,
    observation_id=EXCLUDED.observation_id,
    updated_at=NOW()
WHERE EXCLUDED.verified_at >= aag_ref_heads.verified_at;
