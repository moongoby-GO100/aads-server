"""Authoritative, snapshot-pinned AAG v2 read helpers.

The v1 reader returns whichever legacy project snapshot was newest at query time.
This module keeps every paginated response pinned to one immutable v2 snapshot and
records which consumer/version was used so v1 retirement can be evidence based.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
from typing import Any
from uuid import UUID

import asyncpg

from app.core.db_pool import get_pool

logger = logging.getLogger(__name__)

_SEVERITY_ORDER = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}


class AAGQueryError(ValueError):
    """A safe client-visible v2 query error."""


def _json_value(value: Any, fallback: Any) -> Any:
    if value is None:
        return fallback
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return fallback
    return value


def _filter_digest(filters: dict[str, Any]) -> str:
    encoded = json.dumps(filters, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _encode_cursor(snapshot_id: str, offset: int, digest: str) -> str:
    payload = json.dumps(
        {"snapshot_id": snapshot_id, "offset": offset, "filter_digest": digest},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_cursor(cursor: str) -> dict[str, Any]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
        snapshot_id = str(UUID(str(payload["snapshot_id"])))
        offset = int(payload["offset"])
        digest = str(payload["filter_digest"])
    except (KeyError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AAGQueryError("invalid pagination cursor") from exc
    if offset < 0 or len(digest) != 64:
        raise AAGQueryError("invalid pagination cursor")
    return {"snapshot_id": snapshot_id, "offset": offset, "filter_digest": digest}


async def load_authoritative_snapshot(
    *,
    project: str,
    repository_id: str | None = None,
    target_ref: str | None = None,
    governance_scope: str = "default",
    snapshot_id: str | None = None,
) -> dict[str, Any] | None:
    """Load one verified snapshot, optionally pinned by immutable snapshot id."""
    project = project.strip().upper()
    repository_id = repository_id.strip() if repository_id else None
    target_ref = target_ref.strip() if target_ref else None
    governance_scope = governance_scope.strip() or "default"
    pinned = str(UUID(snapshot_id)) if snapshot_id else None
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            """SELECT p.snapshot_id, p.observation_id, p.run_id,
                      p.project, p.repository_id, p.target_ref, p.governance_scope,
                      p.resolved_commit_sha, p.expected_target_ref_head_sha,
                      p.generated_at, p.verified_at, s.content_fingerprint,
                      s.scanner_version, s.ruleset_digest, s.scan_scope_digest,
                      s.canonicalization_version, s.stable_key_version,
                      s.stats, s.nodes, s.edges, s.findings, s.unresolved,
                      s.node_count, s.edge_count, s.finding_count,
                      s.first_published_at
                 FROM aag_latest_pointers p
                 JOIN aag_graph_snapshots_v2 s
                   ON s.id=p.snapshot_id AND s.project=p.project
                  AND s.repository_id=p.repository_id
                 JOIN aag_snapshot_observations o
                   ON o.id=p.observation_id AND o.snapshot_id=p.snapshot_id
                  AND o.run_id=p.run_id AND o.project=p.project
                  AND o.repository_id=p.repository_id AND o.target_ref=p.target_ref
                  AND o.governance_scope=p.governance_scope
                  AND o.resolved_commit_sha=p.resolved_commit_sha
                WHERE p.project=$1
                  AND ($2::text IS NULL OR p.repository_id=$2)
                  AND ($3::text IS NULL OR p.target_ref=$3)
                  AND p.governance_scope=$4
                  AND ($5::uuid IS NULL OR p.snapshot_id=$5::uuid)
                  AND s.publish_status='ready'
                  AND o.authoritative=TRUE AND o.verification_status='verified'
                ORDER BY p.verified_at DESC, p.snapshot_id DESC LIMIT 1""",
            project, repository_id, target_ref, governance_scope, pinned,
        )
    if not row:
        return None
    data = dict(row)
    for key, fallback in (
        ("stats", {}), ("nodes", []), ("edges", []),
        ("findings", []), ("unresolved", []),
    ):
        data[key] = _json_value(data.get(key), fallback)
    return data


def _finding_path(finding: dict[str, Any]) -> str:
    return str(
        finding.get("path") or finding.get("module") or finding.get("file")
        or finding.get("semantic_target") or ""
    ).replace("\\", "/")


def _matches(finding: dict[str, Any], filters: dict[str, Any]) -> bool:
    if filters["rule"] and str(finding.get("rule") or "").lower() != filters["rule"].lower():
        return False
    if filters["severity"] and str(finding.get("severity") or "").upper() != filters["severity"].upper():
        return False
    if filters["path_prefix"] and not _finding_path(finding).startswith(filters["path_prefix"]):
        return False
    if filters["stable_finding_key"] and str(finding.get("stable_finding_key") or finding.get("key") or "") != filters["stable_finding_key"]:
        return False
    if filters["status"] and str(finding.get("status") or "open").lower() != filters["status"].lower():
        return False
    if filters["owner"]:
        owners = finding.get("owners") or [finding.get("owner")]
        if filters["owner"] not in {str(owner) for owner in owners if owner}:
            return False
    return True


def _finding_sort_key(finding: dict[str, Any]) -> tuple[Any, ...]:
    severity = str(finding.get("severity") or "").upper()
    stable_key = str(finding.get("stable_finding_key") or finding.get("key") or "")
    return (
        _SEVERITY_ORDER.get(severity, 99),
        str(finding.get("rule") or "").lower(),
        _finding_path(finding),
        stable_key,
        json.dumps(finding, ensure_ascii=True, sort_keys=True, separators=(",", ":")),
    )


async def query_findings_page(
    *,
    project: str,
    repository_id: str | None = None,
    target_ref: str | None = None,
    governance_scope: str = "default",
    snapshot_id: str | None = None,
    cursor: str | None = None,
    page_size: int = 50,
    rule: str | None = None,
    severity: str | None = None,
    path_prefix: str | None = None,
    stable_finding_key: str | None = None,
    status: str | None = None,
    owner: str | None = None,
) -> dict[str, Any]:
    """Return one deterministic page pinned to an immutable snapshot."""
    page_size = max(1, min(int(page_size), 200))
    filters = {
        "rule": (rule or "").strip(),
        "severity": (severity or "").strip(),
        "path_prefix": (path_prefix or "").strip().replace("\\", "/"),
        "stable_finding_key": (stable_finding_key or "").strip(),
        "status": (status or "").strip(),
        "owner": (owner or "").strip(),
    }
    digest = _filter_digest(filters)
    offset = 0
    if cursor:
        decoded = _decode_cursor(cursor)
        if decoded["filter_digest"] != digest:
            raise AAGQueryError("pagination cursor does not match filters")
        if snapshot_id and str(UUID(snapshot_id)) != decoded["snapshot_id"]:
            raise AAGQueryError("pagination cursor does not match snapshot_id")
        snapshot_id = decoded["snapshot_id"]
        offset = decoded["offset"]

    snapshot = await load_authoritative_snapshot(
        project=project, repository_id=repository_id, target_ref=target_ref,
        governance_scope=governance_scope, snapshot_id=snapshot_id,
    )
    if not snapshot:
        raise AAGQueryError("no authoritative AAG v2 snapshot")
    findings = sorted(
        [item for item in snapshot["findings"] if isinstance(item, dict) and _matches(item, filters)],
        key=_finding_sort_key,
    )
    page = findings[offset:offset + page_size]
    next_offset = offset + len(page)
    next_cursor = None
    if next_offset < len(findings):
        next_cursor = _encode_cursor(str(snapshot["snapshot_id"]), next_offset, digest)
    return {
        "schema_version": "aag-v2",
        "snapshot_id": str(snapshot["snapshot_id"]),
        "observation_id": str(snapshot["observation_id"]),
        "run_id": str(snapshot["run_id"]),
        "ref": {
            "project": snapshot["project"],
            "repository_id": snapshot["repository_id"],
            "target_ref": snapshot["target_ref"],
            "governance_scope": snapshot["governance_scope"],
        },
        "commit_sha": snapshot["resolved_commit_sha"],
        "authoritative": True,
        "generated_at": snapshot["generated_at"],
        "verified_at": snapshot["verified_at"],
        "findings": page,
        "pagination": {
            "pinned": True, "offset": offset, "page_size": page_size,
            "returned": len(page), "total": len(findings), "next_cursor": next_cursor,
        },
        "limitation": [],
    }


async def record_consumer_event(
    *, consumer: str, api_version: str, endpoint: str, project: str,
    outcome: str, request_id: str | None = None, repository_id: str | None = None,
    target_ref: str | None = None, snapshot_id: str | None = None,
    latency_ms: int | None = None,
) -> None:
    """Best-effort telemetry; observability must never break the read path."""
    try:
        async with get_pool().acquire() as conn:
            await conn.execute(
                """INSERT INTO aag_api_consumer_events
                       (request_id, consumer, api_version, endpoint, project,
                        repository_id, target_ref, snapshot_id, outcome, latency_ms)
                     VALUES ($1,$2,$3,$4,$5,$6,$7,$8::uuid,$9,$10)""",
                (request_id or "")[:200], (consumer or "unknown")[:200],
                api_version[:16], endpoint[:200], project.strip().upper()[:100],
                repository_id, target_ref, snapshot_id, outcome[:40], latency_ms,
            )
    except (asyncpg.PostgresError, RuntimeError) as exc:  # migration may lag code
        logger.warning("aag_consumer_telemetry_failed", extra={"error": str(exc)[:160]})


async def consumer_telemetry(project: str, hours: int = 24) -> list[dict[str, Any]]:
    async with get_pool().acquire() as conn:
        rows = await conn.fetch(
            """SELECT api_version, consumer, endpoint, outcome, COUNT(*)::int AS requests,
                      MAX(created_at) AS last_seen_at
                 FROM aag_api_consumer_events
                WHERE (project=$1 OR project='ALL')
                  AND created_at >= NOW() - make_interval(hours => $2::int)
                GROUP BY api_version, consumer, endpoint, outcome
                ORDER BY api_version, consumer, endpoint, outcome""",
            project.strip().upper(), max(1, min(int(hours), 24 * 90)),
        )
    return [dict(row) for row in rows]
