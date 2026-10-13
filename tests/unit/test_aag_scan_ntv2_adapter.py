import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
ADAPTER_PATH = ROOT / "tools/aag/scan_ntv2_adapter.py"
SHA = "a" * 40

FAKE_SCANNER = '''
from types import SimpleNamespace


def load_rules(path):
    return {"rules_path": path}


def scan(root, rules):
    return SimpleNamespace(live_root=root, scope_mode="full", unresolved=[])


def build_graph(context):
    return {"nodes": [{"id": "n1"}], "edges": [{"from": "n1", "to": "n1"}]}


def detect_findings(context):
    return {"R1": [{"file": "a.php", "line": 3}]}


def compute_metrics(context, graph, grouped):
    return {"nodes": len(graph["nodes"])}


def render_mermaid(context):
    return "graph TD\\n"


def render_findings(context, grouped, metrics):
    return "# findings\\n"
'''


def _load_adapter():
    spec = importlib.util.spec_from_file_location("aag_scan_ntv2_adapter_under_test", ADAPTER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _argv(tmp_path, commit_sha=SHA, expected=SHA):
    scanner = tmp_path / "scan.py"
    scanner.write_text(FAKE_SCANNER, encoding="utf-8")
    rules = tmp_path / "rules.yml"
    rules.write_text("rules: []\n", encoding="utf-8")
    return [
        "--root", str(tmp_path),
        "--scanner", str(scanner),
        "--rules", str(rules),
        "--out-dir", str(tmp_path / "out"),
        "--commit-sha", commit_sha,
        "--expected-head-sha", expected,
    ]


def test_adapter_module_imports_and_exposes_main():
    assert callable(_load_adapter().main)


def test_adapter_requires_all_cli_arguments():
    with pytest.raises(SystemExit) as exc:
        _load_adapter().main([])
    assert exc.value.code == 2


def test_adapter_rejects_moved_ref_head(tmp_path):
    with pytest.raises(SystemExit, match="ref head changed"):
        _load_adapter().main(_argv(tmp_path, commit_sha=SHA, expected="b" * 40))
    assert not (tmp_path / "out").exists()


def test_adapter_writes_v2_graph_with_source_identity(tmp_path, capsys):
    assert _load_adapter().main(_argv(tmp_path)) == 0

    out = tmp_path / "out"
    graph = json.loads((out / "ntv2-graph.json").read_text(encoding="utf-8"))
    assert graph["schema"] == "aag/graph@2"
    assert graph["commit_sha"] == SHA
    assert graph["findings"] == [{"file": "a.php", "line": 3, "rule": "R1"}]
    identity = graph["source_identity"]
    assert identity["resolved_commit_sha"] == SHA
    assert identity["expected_target_ref_head_sha"] == SHA
    assert identity["repository_id"] == "newtalk-v2"
    assert identity["scanner_version"].startswith("ntv2-scan-")
    assert (out / "ntv2-arch.mmd").read_text(encoding="utf-8") == "graph TD\n"
    assert (out / "ntv2-findings.md").exists()

    summary = json.loads(capsys.readouterr().out)
    assert summary["project"] == "NTV2"
    assert summary["commit_sha"] == SHA
