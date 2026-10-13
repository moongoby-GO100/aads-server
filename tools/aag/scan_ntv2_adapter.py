#!/usr/bin/env python3
"""Run the NTV2 Laravel scanner and emit the central AAG v2 graph contract.

The NTV2 scanner predates the central publisher and writes ``metrics`` plus a
finding summary only.  This adapter uses the scanner's public functions so the
full findings and immutable Git source identity reach ``aag_latest_pointers``.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_scanner(path: Path):
    spec = importlib.util.spec_from_file_location("aag_ntv2_scanner", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load scanner: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _flatten_findings(grouped: dict) -> list[dict]:
    flattened: list[dict] = []
    for rule, items in grouped.items():
        for raw in items:
            item = dict(raw)
            item["rule"] = rule
            flattened.append(item)
    return flattened


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--scanner", required=True)
    parser.add_argument("--rules", required=True)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--commit-sha", required=True)
    parser.add_argument("--expected-head-sha", required=True)
    args = parser.parse_args(argv)

    if args.commit_sha != args.expected_head_sha:
        raise SystemExit("ref head changed while preparing the NTV2 scan")

    root = Path(args.root).resolve()
    scanner_path = Path(args.scanner).resolve()
    rules_path = Path(args.rules).resolve()
    out_dir = Path(args.out_dir).resolve()
    scanner = _load_scanner(scanner_path)
    rules = scanner.load_rules(str(rules_path))
    context = scanner.scan(str(root), rules)
    graph = scanner.build_graph(context)
    grouped_findings = scanner.detect_findings(context)
    metrics = scanner.compute_metrics(context, graph, grouped_findings)

    scanner_digest = _sha256(scanner_path)
    rules_digest = _sha256(rules_path)
    scope_digest = hashlib.sha256(
        json.dumps(
            {
                "live_root": context.live_root,
                "rules_digest": rules_digest,
                "scope_mode": context.scope_mode,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    generated_at = datetime.now(timezone.utc).isoformat()
    graph_doc = {
        "schema": "aag/graph@2",
        "generated_at": generated_at,
        "commit_sha": args.commit_sha,
        "stats": metrics,
        "findings": _flatten_findings(grouped_findings),
        "unresolved": context.unresolved,
        "nodes": graph["nodes"],
        "edges": graph["edges"],
        "source_identity": {
            "repository_id": "newtalk-v2",
            "target_ref": "refs/heads/main",
            "governance_scope": "default",
            "resolved_commit_sha": args.commit_sha,
            "expected_target_ref_head_sha": args.expected_head_sha,
            "scanner_version": f"ntv2-scan-{scanner_digest[:16]}",
            "ruleset_digest": rules_digest,
            "scan_scope_digest": scope_digest,
            "normalization_version": "aag-c14n-v1",
            "stable_key_version": "aag-stable-key-v1",
            "parser_versions": {"laravel": scanner_digest[:16]},
        },
    }

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "ntv2-graph.json").write_text(
        json.dumps(graph_doc, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (out_dir / "ntv2-arch.mmd").write_text(
        scanner.render_mermaid(context), encoding="utf-8"
    )
    (out_dir / "ntv2-findings.md").write_text(
        scanner.render_findings(context, grouped_findings, metrics), encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "project": "NTV2",
                "generated_at": generated_at,
                "commit_sha": args.commit_sha,
                "nodes": len(graph_doc["nodes"]),
                "edges": len(graph_doc["edges"]),
                "findings": len(graph_doc["findings"]),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
