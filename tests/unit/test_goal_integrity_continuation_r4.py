from pathlib import Path

from app.services.goal_binding import remediation_purpose

ROOT = Path(__file__).resolve().parents[2]
GOALS = (ROOT / "app/routers/goals.py").read_text(encoding="utf-8")
MANAGER = (ROOT / "app/services/goal_manager.py").read_text(encoding="utf-8")
HIERARCHY = (ROOT / "app/services/goal_work_hierarchy.py").read_text(encoding="utf-8")
UP = (ROOT / "migrations/20260920_goal_integrity_continuation_r4.sql").read_text(
    encoding="utf-8"
)
VERIFY = (
    ROOT / "migrations/20260920_goal_integrity_continuation_r4.verify.sql"
).read_text(encoding="utf-8")
DOWN = (
    ROOT / "migrations/rollback/20260920_goal_integrity_continuation_r4.down.sql"
).read_text(encoding="utf-8")


def test_remediation_purpose_is_explicit_and_nonempty() -> None:
    assert remediation_purpose("REMEDIATION_PURPOSE: restore-loop") == "restore-loop"
    assert remediation_purpose("REMEDIATION_PURPOSE:") is None
    assert remediation_purpose("please restore the loop") is None


def test_default_goal_views_hide_terminal_history_but_allow_audit_view() -> None:
    terminal_filter = "status NOT IN ('cancelled', 'superseded', 'archived')"
    assert GOALS.count("include_history: bool = Query(False)") >= 3
    assert terminal_filter in GOALS
    assert terminal_filter in MANAGER
    assert "include_history: bool = False" in HIERARCHY
    assert "w.status <> 'cancelled'" in HIERARCHY


def test_progress_and_completion_ignore_terminal_history() -> None:
    marker = "AND status NOT IN ('cancelled', 'superseded', 'archived')"
    assert marker in MANAGER
    assert MANAGER.count("_milestone_completion_stats(conn, goal_id)") >= 2
    assert "earlier.status NOT IN ('completed', 'cancelled', 'superseded', 'archived')" in MANAGER


def test_data_repair_is_exact_scoped_repeatable_and_non_destructive() -> None:
    upper = UP.upper()
    assert "DROP " not in upper
    assert "TRUNCATE " not in upper
    assert "\nDELETE " not in upper
    assert "CF1EC2F6-0072-4F85-AA5E-B08760CD6613" in upper
    assert "C83DD908-9351-4A84-B9CF-96F4D221A29B" in upper
    assert "0F97CE3D-45C4-411C-AB08-35BA5AEA21D2" in upper
    assert "ON CONFLICT" in upper
    assert "(3, 6, 12)" in UP
    assert "goal-v12-backfill:%" in UP
    assert "goal-integrity-r4:m16:epic:%" in UP


def test_verify_and_compensating_rollback_cover_required_invariants() -> None:
    for expected in (
        "live_duplicate_groups",
        "canonical_m16",
        "m10_history",
        "active_synthetic",
        "epics",
        "stories",
        "tasks",
        "unassigned",
        "missing_criteria",
    ):
        assert expected in VERIFY
    assert "goal_hierarchy_repair_audit" in DOWN
    assert "jsonb_to_recordset" in DOWN
    assert "WITH RECURSIVE canonical_tree" in DOWN
