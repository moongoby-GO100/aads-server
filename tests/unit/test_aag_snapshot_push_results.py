"""aag_snapshot_push.py 가 DB 가 확정한 run 결과를 그대로 내보내고 거부를 exit 9 로 알리는가."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts import aag_snapshot_push as push  # noqa: E402

SHA = "a" * 40


def _graph(path: Path, commit: str = SHA, expected: str = SHA) -> Path:
    graph = {
        "generated_at": "2026-10-22T17:00:00+09:00",
        "commit_sha": commit,
        "source_identity": {
            "repository_id": "aads-server", "target_ref": "refs/heads/main",
            "resolved_commit_sha": commit, "expected_target_ref_head_sha": expected,
            "scanner_version": "v", "ruleset_digest": "r", "scan_scope_digest": "s",
            "normalization_version": "n", "stable_key_version": "k", "parser_versions": {},
        },
        "stats": {"graph_nodes": 1, "graph_edges": 0}, "nodes": [], "edges": [],
        "findings": [], "unresolved": [],
    }
    path.write_text(json.dumps(graph), encoding="utf-8")
    return path


class _Proc:
    def __init__(self, stdout="", returncode=0, stderr=""):
        self.stdout, self.returncode, self.stderr = stdout, returncode, stderr


def _run(monkeypatch, capsys, graph: Path, rows_for):
    """Run main() against a fake psql whose AAGRESULT rows come from rows_for(sql_run_ids)."""
    seen = {}

    def fake_run(cmd, input=None, **_):
        seen["sql"] = input
        ids = [line for line in input.split("'") if len(line) == 36 and line.count("-") == 4]
        return _Proc("INSERT 0 1\n" + "\n".join(rows_for(ids)) + "\n")

    monkeypatch.setattr(push.subprocess, "run", fake_run)
    rc = push.main(["AADS=%s" % graph])
    out = capsys.readouterr()
    return rc, out.out, out.err, seen["sql"]


def test_accepted_run_exits_zero_and_reports_db_result(tmp_path, monkeypatch, capsys):
    rc, out, _, sql = _run(
        monkeypatch, capsys, _graph(tmp_path / "g.json"),
        lambda ids: ["AAGRESULT|AADS|%s|no_change_success|verified|true" % i for i in ids],
    )
    assert rc == 0
    assert "RESULT project=AADS" in out and "result=no_change_success verification=verified authoritative=true" in out
    assert "AAGRESULT|" not in out
    assert "\\pset tuples_only on" in sql and sql.rstrip().endswith("COMMIT;")
    assert sql.index("AAGRESULT") < sql.rindex("COMMIT;")


@pytest.mark.parametrize("db_result,verification", [
    ("source_behind", "commit_mismatch"), ("out_of_order", "out_of_order"),
])
def test_rejected_run_exits_9_and_keeps_the_db_result(tmp_path, monkeypatch, capsys, db_result, verification):
    rc, out, err, _ = _run(
        monkeypatch, capsys, _graph(tmp_path / "g.json", commit="b" * 40),
        lambda ids: ["AAGRESULT|AADS|%s|%s|%s|false" % (i, db_result, verification) for i in ids],
    )
    assert rc == push.EXIT_REJECTED == 9
    assert "RESULT project=AADS" in out and "result=%s verification=%s authoritative=false" % (db_result, verification) in out
    assert "REJECTED AADS=%s" % db_result in err


def test_run_missing_from_readback_is_rejected_not_assumed_ok(tmp_path, monkeypatch, capsys):
    rc, out, _, _ = _run(monkeypatch, capsys, _graph(tmp_path / "g.json"), lambda ids: [])
    assert rc == 9 and "result=missing" in out


def test_psql_failure_is_exit_1(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(push.subprocess, "run", lambda *a, **k: _Proc("", 3, "boom"))
    assert push.main(["AADS=%s" % _graph(tmp_path / "g.json")]) == 1


def test_legacy_only_project_is_reported_and_not_rejected(tmp_path, monkeypatch, capsys):
    graph = _graph(tmp_path / "g.json")
    data = json.loads(graph.read_text())
    data["source_identity"] = {}
    graph.write_text(json.dumps(data))
    monkeypatch.setattr(push.subprocess, "run", lambda *a, **k: _Proc("INSERT 0 1\n"))
    rc = push.main(["AADS=%s" % graph])
    out = capsys.readouterr().out
    assert rc == 0 and "RESULT project=AADS run_id= result=legacy_only" in out


def test_parse_results_ignores_malformed_rows():
    run_id = str(uuid4())
    kept, rows = push.parse_results("x\nAAGRESULT|broken\n", [("AADS", run_id)])
    assert kept == "x\n" and rows[0]["result"] == "missing"


def test_v2_statement_uses_the_supplied_run_id(tmp_path):
    graph = json.loads(_graph(tmp_path / "g.json").read_text())
    run_id = str(uuid4())
    assert run_id in push._v2_statement("AADS", graph, run_id=run_id)


def test_subprocess_module_is_still_the_real_one():
    assert push.subprocess is subprocess
