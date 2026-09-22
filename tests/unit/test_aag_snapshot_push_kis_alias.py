from __future__ import annotations

from pathlib import Path

import pytest

from scripts.aag_snapshot_push import _targets, _v2_statement
from tools.aag.v2_contract import stable_finding_key

ROOT = Path(__file__).resolve().parents[2]


def _go100_finding() -> dict:
    finding = {
        "rule": "ORPHAN_ROUTER",
        "module": "backend/app/routers/go100/scheduler_router.py",
        "detail": "not included by any entrypoint",
        "severity": "P2",
    }
    finding["stable_finding_key"] = stable_finding_key("GO100", finding)
    finding["stable_key_version"] = "aag-stable-key-v1"
    return finding


def _go100_graph(findings: list[dict]) -> dict:
    return {
        "generated_at": "2026-09-22T09:17:29+00:00",
        "source_identity": {
            "repository_id": "kis-autotrade-v4",
            "target_ref": "refs/heads/main",
            "resolved_commit_sha": "5" * 40,
            "expected_target_ref_head_sha": "5" * 40,
            "scanner_version": "aag-scanner-v1.1",
            "ruleset_digest": "a" * 64,
            "scan_scope_digest": "b" * 64,
            "normalization_version": "aag-c14n-v1",
            "stable_key_version": "aag-stable-key-v1",
            "parser_versions": {"python_ast": "3.12"},
        },
        "stats": {}, "nodes": [], "edges": [], "findings": findings, "unresolved": [],
    }


def test_targets_parses_shared_monorepo_alias():
    project, identity_project, path = next(
        _targets(["KIS@GO100=/tmp/go100-graph.json"])
    )

    assert (project, identity_project, path) == ("KIS", "GO100", Path("/tmp/go100-graph.json"))


def test_targets_without_alias_has_no_identity_project():
    project, identity_project, path = next(_targets(["GO100=/tmp/go100-graph.json"]))

    assert (project, identity_project) == ("GO100", None)


def test_kis_republish_with_go100_identity_accepts_go100_baked_findings():
    # go100-graph.json's findings carry stable_finding_key values baked at
    # scan time under AAG_PROJECT=GO100. Republishing the identical file as
    # KIS (shared_monorepo scope) must not re-derive finding identity under
    # the KIS label — that mismatch previously raised ValueError inside
    # graph_content()/normalize_findings() and was swallowed by main()'s
    # broad `except ValueError: continue`, silently dropping the v2 INSERT
    # that updates aag_latest_pointers. Real-world symptom (2026-09-22):
    # KIS pointer frozen at generated_at=2026-09-21T03:27 while GO100's
    # pointer for the identical repository kept advancing.
    graph = _go100_graph([_go100_finding()])

    sql = _v2_statement("KIS", graph, identity_project="GO100")

    assert sql is not None
    assert "aag_latest_pointers" in sql


def test_kis_republish_without_go100_identity_still_rejects_mismatched_keys():
    # Guard the failure mode this fix targets: publishing GO100-scanned
    # findings under a different project label *without* declaring the
    # alias must keep failing fast rather than silently accepting
    # mismatched finding identities.
    graph = _go100_graph([_go100_finding()])

    with pytest.raises(ValueError):
        _v2_statement("KIS", graph)


def test_all_projects_refresh_publishes_kis_via_go100_identity_alias():
    script = (ROOT / "scripts/aag_all_projects_refresh.sh").read_text(encoding="utf-8")

    assert '"KIS@GO100=$STATE_DIR/go100/go100-graph.json"' in script
