"""Pure AAG v1.1 fingerprint helpers shared by API and host-side publishers."""

from __future__ import annotations

import hashlib
import json
from typing import Any

CANONICALIZATION_VERSION = "aag-c14n-v1"
STABLE_KEY_VERSION = "aag-stable-key-v1"


def _canonical(value: Any) -> Any:
    """Return a JSON-compatible value with deterministic object and list ordering."""
    if isinstance(value, dict):
        return {str(key): _canonical(value[key]) for key in sorted(value, key=str)}
    if isinstance(value, (list, tuple)):
        normalized = [_canonical(item) for item in value]
        return sorted(
            normalized,
            key=lambda item: json.dumps(
                item, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ),
        )
    if isinstance(value, str):
        return value.replace("\\", "/")
    return value


def canonical_json(value: Any) -> str:
    return json.dumps(
        _canonical(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def graph_content(graph: dict[str, Any]) -> dict[str, Any]:
    """Select immutable graph content; timestamps and producer host are excluded."""
    return {
        "stats": graph.get("stats") or {},
        "nodes": graph.get("nodes") or [],
        "edges": graph.get("edges") or [],
        "findings": graph.get("findings") or [],
        "unresolved": graph.get("unresolved") or [],
    }


def content_fingerprint(graph: dict[str, Any]) -> str:
    return sha256_json(graph_content(graph))


def input_fingerprint(
    *,
    project: str,
    repository_id: str,
    target_ref: str,
    resolved_commit_sha: str,
    expected_target_ref_head_sha: str,
    scanner_version: str,
    ruleset_digest: str,
    scan_scope_digest: str,
    governance_scope: str = "default",
    parser_versions: dict[str, Any] | None = None,
    normalization_version: str = CANONICALIZATION_VERSION,
    worktree_digest: str | None = None,
) -> str:
    """Hash every source identity that can change a scan result."""
    return sha256_json(
        {
            "project": project.upper(),
            "repository_id": repository_id,
            "target_ref": target_ref,
            "governance_scope": governance_scope,
            "resolved_commit_sha": resolved_commit_sha,
            "expected_target_ref_head_sha": expected_target_ref_head_sha,
            "scanner_version": scanner_version,
            "ruleset_digest": ruleset_digest,
            "scan_scope_digest": scan_scope_digest,
            "parser_versions": parser_versions or {},
            "normalization_version": normalization_version,
            "worktree_digest": worktree_digest,
        }
    )
