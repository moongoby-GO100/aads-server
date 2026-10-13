from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_hourly_go100_refresh_keeps_git_worktree_clean():
    script = (ROOT / "scripts/aag_go100_refresh.sh").read_text(encoding="utf-8")

    assert 'AAG_GO100_OUT_DIR:-/var/lib/aads/aag/go100' in script
    assert 'OUT_DIR="${REPO_DIR}/reports/aag"' not in script
    assert '"GO100=${OUT_DIR}/go100-graph.json"' in script


def test_go100_refresh_syncs_scanner_contract_and_rejects_stale_artifacts():
    script = (ROOT / "scripts/aag_go100_refresh.sh").read_text(encoding="utf-8")

    assert '"${REPO_DIR}/tools/aag/v2_contract.py"' in script
    assert "AAG_PROJECT=GO100" in script
    assert "fresh graph artifact was not produced" in script
    assert "graph source SHA does not match" in script
