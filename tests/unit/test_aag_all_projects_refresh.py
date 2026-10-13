from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_all_project_refresh_covers_the_six_registered_projects():
    source = (ROOT / "scripts/aag_all_projects_refresh.sh").read_text(encoding="utf-8")
    for project in ("AADS", "GO100", "KIS", "SF", "NTV2", "NAS"):
        assert f"project={project}" in source or f" {project} " in source


def test_refresh_is_locked_bounded_and_keeps_artifacts_outside_repositories():
    source = (ROOT / "scripts/aag_all_projects_refresh.sh").read_text(encoding="utf-8")
    assert "flock -n 9" in source
    assert "timeout 900" in source
    assert "/var/lib/aads/aag" in source
    assert "reports/aag" not in source


def test_timer_runs_every_fifteen_minutes():
    timer = (ROOT / "scripts/aads-aag-all-projects.timer").read_text(encoding="utf-8")
    assert "OnUnitActiveSec=15min" in timer
    assert "Persistent=true" in timer


def test_ntv2_uses_clean_ref_adapter_and_publishes_central_pointer():
    source = (ROOT / "scripts/aag_all_projects_refresh.sh").read_text(encoding="utf-8")

    assert "scan_ntv2_adapter.py" in source
    assert "git -C /srv/newtalk-v2 archive refs/remotes/origin/main" in source
    assert '"NTV2=$STATE_DIR/ntv2/ntv2-graph.json"' in source
    assert "publish=artifact_only" not in source
