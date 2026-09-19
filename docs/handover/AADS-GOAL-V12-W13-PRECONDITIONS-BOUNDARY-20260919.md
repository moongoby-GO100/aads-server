# AADS W-13 preconditions and project-boundary handover

- Task: `AADS-GOAL-V12-W13-PRECONDITIONS-BOUNDARY-20260919`
- Base SHA: `0f98c5eefc56305944c90cce2d748a41623e0dee`
- Required ancestor: `e1061c657a59fadf612c30bb294c432221aed6e6` (confirmed)
- Change SHA: none; commit/push prohibited by runner instruction

Implemented additive W-13 services and APIs for server-computed precondition
previews and policy-input creation. Snapshots are immutable, tenant-fenced,
versioned, and hash-deterministic. Policy-input creation does not evaluate AUTO
or touch grant reservation/use stores. `require_current_preconditions()` is the
downstream execution boundary for rejecting a changed snapshot before grant use.

Changed paths:

- `app/routers/work_items.py`
- `app/services/goal_policy_preconditions.py`
- `migrations/20260919_goal_policy_preconditions_w13.sql`
- `tests/unit/test_goal_policy_preconditions.py`
- `tests/unit/test_goal_policy_preconditions_migration.py`
- `tests/integration/test_goal_policy_foundation_migration.py`
- `RESULT.md`

Verification:

- py_compile: PASS
- git diff --check: PASS
- AAG baseline: PASS, no baseline increase
- target pytest: HOLD at collection because FastAPI is absent from the host Python
- disposable PostgreSQL: not run; `M12_TEST_DATABASE_URL` is not configured
- npm/next/docker build: 승인 후 Runner 빌드 검증 대상

Gaps: W-14F must call `require_current_preconditions()` immediately before
evaluation/execution and before any grant reservation. This task intentionally
does not alter the signed decision ledger, grant consumer, deploy state, or
production schema.
