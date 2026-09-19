"""Pure AAG v1.1 fingerprint helpers shared by API and host-side publishers."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

CANONICALIZATION_VERSION = "aag-c14n-v1"
STABLE_KEY_VERSION = "aag-stable-key-v1"

_LINE_SUFFIX_RE = re.compile(r":\d+(?::\d+)?$")
_MULTISLASH_RE = re.compile(r"/{2,}")


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


def _normalized_path(value: Any) -> str:
    path = str(value or "").strip().replace("\\", "/")
    path = _MULTISLASH_RE.sub("/", path)
    while path.startswith("./"):
        path = path[2:]
    return _LINE_SUFFIX_RE.sub("", path)


def finding_identity(project: str, finding: dict[str, Any]) -> dict[str, str]:
    """Build a line-number-independent semantic finding identity."""
    rule = str(finding.get("rule") or "UNKNOWN").strip().upper()
    method = str(finding.get("method") or "").strip().upper()
    route = _normalized_path(finding.get("path")) if method else ""
    normalized_path = _normalized_path(
        finding.get("file") or finding.get("module") or finding.get("path") or ""
    )
    semantic_target = str(finding.get("semantic_target") or "").strip()
    if not semantic_target:
        if method and route:
            semantic_target = f"{method} {route}"
        elif finding.get("table"):
            semantic_target = f"table:{str(finding['table']).strip().lower()}"
        elif finding.get("symbol"):
            semantic_target = f"symbol:{finding['symbol']}"
        elif finding.get("namespace"):
            semantic_target = f"namespace:{_normalized_path(finding['namespace'])}"
        else:
            semantic_target = _normalized_path(finding.get("key") or normalized_path)
    contract_signature = str(finding.get("contract_signature") or "").strip()
    if not contract_signature:
        contract_signature = "|".join(
            part for part in (
                str(finding.get("entrypoint") or "").strip(),
                method,
                route,
                str(finding.get("table") or "").strip().lower(),
            ) if part
        )
    return {
        "project": str(project or "").strip().upper(),
        "rule": rule,
        "semantic_target": semantic_target,
        "normalized_path": normalized_path,
        "contract_signature": contract_signature,
    }


def stable_finding_key(project: str, finding: dict[str, Any]) -> str:
    return f"{STABLE_KEY_VERSION}:{sha256_json(finding_identity(project, finding))}"


def normalize_findings(project: str, findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized = []
    for raw in findings:
        item = dict(raw)
        expected = stable_finding_key(project, item)
        supplied = str(item.get("stable_finding_key") or "")
        if supplied and supplied != expected:
            raise ValueError("stable finding key does not match canonical identity")
        item["stable_finding_key"] = expected
        item["stable_key_version"] = STABLE_KEY_VERSION
        normalized.append(item)
    return sorted(normalized, key=lambda item: item["stable_finding_key"])


def finding_key_set_digest(findings: list[dict[str, Any]]) -> str:
    keys = sorted({str(item.get("stable_finding_key") or "") for item in findings})
    if any(not key for key in keys):
        raise ValueError("every baseline finding must have a stable key")
    return sha256_json(keys)


def graph_content(graph: dict[str, Any], *, project: str | None = None) -> dict[str, Any]:
    """Select immutable graph content; timestamps and producer host are excluded."""
    return {
        "stats": graph.get("stats") or {},
        "nodes": graph.get("nodes") or [],
        "edges": graph.get("edges") or [],
        "findings": (
            normalize_findings(project, graph.get("findings") or [])
            if project else graph.get("findings") or []
        ),
        "unresolved": graph.get("unresolved") or [],
    }


def content_fingerprint(graph: dict[str, Any], *, project: str | None = None) -> str:
    return sha256_json(graph_content(graph, project=project))


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
