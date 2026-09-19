"""AAG v1.1 run/snapshot/observation ingestion.

The legacy ``aag_graph_snapshots`` path remains untouched.  This service records
every attempt, stores immutable graph content once, and adds a fresh observation
when identical content is verified again.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from app.core.db_pool import get_pool
from tools.aag.v2_contract import (
    CANONICALIZATION_VERSION,
    STABLE_KEY_VERSION,
    content_fingerprint,
    graph_content,
    input_fingerprint,
)


@dataclass(frozen=True)
class IngestResult:
    run_id: UUID
    snapshot_id: UUID | None
    observation_id: UUID | None
    result: str
    input_fingerprint: str
    content_fingerprint: str
    authoritative: bool
    verification_status: str


class IngestConflictError(ValueError):
    """The caller reused a run id with a different immutable input."""


async def ingest_graph(
    *,
    project: str,
    repository_id: str,
    target_ref: str,
    governance_scope: str,
    resolved_commit_sha: str,
    expected_target_ref_head_sha: str,
    host: str,
    generated_at: datetime,
    scanner_version: str,
    ruleset_digest: str,
    scan_scope_digest: str,
    parser_versions: dict[str, Any],
    graph: dict[str, Any],
    worktree_digest: str | None = None,
    canonicalization_version: str = CANONICALIZATION_VERSION,
    stable_key_version: str = STABLE_KEY_VERSION,
    run_id: UUID | None = None,
) -> IngestResult:
    """Persist one run and authoritative observation without mutating graph content."""
    project = project.strip().upper()
    run_id = run_id or uuid4()
    graph_body = graph_content(graph)
    content_hash = content_fingerprint(graph_body)
    input_hash = input_fingerprint(
        project=project,
        repository_id=repository_id,
        target_ref=target_ref,
        resolved_commit_sha=resolved_commit_sha,
        expected_target_ref_head_sha=expected_target_ref_head_sha,
        scanner_version=scanner_version,
        ruleset_digest=ruleset_digest,
        scan_scope_digest=scan_scope_digest,
        parser_versions=parser_versions,
        normalization_version=canonicalization_version,
        worktree_digest=worktree_digest,
    )
    stats = graph_body["stats"]
    findings = graph_body["findings"]
    unresolved = graph_body["unresolved"]
    nodes = graph_body["nodes"]
    edges = graph_body["edges"]
    authoritative = resolved_commit_sha == expected_target_ref_head_sha

    pool = get_pool()
    async with pool.acquire() as conn:
        inserted_run_id = await conn.fetchval(
            """INSERT INTO aag_scan_runs
                   (id, project, repository_id, target_ref, governance_scope,
                    resolved_commit_sha, expected_target_ref_head_sha,
                    input_fingerprint, scanner_version,
                    ruleset_digest, scan_scope_digest, normalization_version,
                    parser_versions, host, result, stage_status)
                 VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13::jsonb,$14,
                         'running', '{"scan":"succeeded","ingest":"running"}'::jsonb)
                 ON CONFLICT (id) DO NOTHING RETURNING id""",
            run_id, project, repository_id, target_ref, governance_scope,
            resolved_commit_sha, expected_target_ref_head_sha, input_hash,
            scanner_version, ruleset_digest,
            scan_scope_digest, canonicalization_version,
            json.dumps(parser_versions), host,
        )
        if inserted_run_id is None:
            existing = await conn.fetchrow(
                """SELECT r.input_fingerprint, r.result,
                          o.snapshot_id, o.id AS observation_id,
                          o.content_fingerprint, o.authoritative,
                          o.verification_status
                     FROM aag_scan_runs r
                LEFT JOIN aag_snapshot_observations o ON o.run_id=r.id
                    WHERE r.id=$1""",
                run_id,
            )
            if not existing or existing["input_fingerprint"] != input_hash:
                raise IngestConflictError("run_id already exists with different input")
            return IngestResult(
                run_id=run_id,
                snapshot_id=existing["snapshot_id"],
                observation_id=existing["observation_id"],
                result=existing["result"],
                input_fingerprint=input_hash,
                content_fingerprint=existing["content_fingerprint"] or content_hash,
                authoritative=bool(existing["authoritative"]),
                verification_status=existing["verification_status"] or "unknown",
            )
        try:
            async with conn.transaction():
                snapshot_id = await conn.fetchval(
                    """INSERT INTO aag_graph_snapshots_v2
                           (project, repository_id, content_fingerprint,
                            canonicalization_version, stable_key_version, stats,
                            nodes, edges, findings, unresolved, node_count,
                            edge_count, finding_count, generated_at, publish_status,
                            first_published_at)
                         VALUES ($1,$2,$3,$4,$5,$6::jsonb,$7::jsonb,$8::jsonb,
                                 $9::jsonb,$10::jsonb,$11,$12,$13,$14,'ready',NOW())
                         ON CONFLICT (project, repository_id, content_fingerprint)
                         DO NOTHING RETURNING id""",
                    project, repository_id, content_hash, canonicalization_version,
                    stable_key_version, json.dumps(stats), json.dumps(nodes),
                    json.dumps(edges), json.dumps(findings), json.dumps(unresolved),
                    len(nodes), len(edges), len(findings), generated_at,
                )
                created = snapshot_id is not None
                if snapshot_id is None:
                    snapshot_id = await conn.fetchval(
                        """SELECT id FROM aag_graph_snapshots_v2
                            WHERE project=$1 AND repository_id=$2
                              AND content_fingerprint=$3""",
                        project, repository_id, content_hash,
                    )
                observation_result = "succeeded" if created else "no_change_success"
                result = observation_result if authoritative else "source_behind"
                verification_status = "verified" if authoritative else "commit_mismatch"
                observation_id = await conn.fetchval(
                    """INSERT INTO aag_snapshot_observations
                           (run_id, snapshot_id, project, repository_id, target_ref,
                            governance_scope, resolved_commit_sha,
                            expected_target_ref_head_sha, input_fingerprint,
                            content_fingerprint, authoritative, result,
                            verification_status, verified_at)
                         VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,NOW())
                         RETURNING id""",
                    run_id, snapshot_id, project, repository_id, target_ref,
                    governance_scope, resolved_commit_sha,
                    expected_target_ref_head_sha, input_hash, content_hash,
                    authoritative, observation_result, verification_status,
                )
                await conn.execute(
                    """UPDATE aag_scan_runs
                          SET result=$2, finished_at=NOW(),
                              stage_status='{ "scan":"succeeded", "ingest":"succeeded" }'::jsonb
                        WHERE id=$1""",
                    run_id, result,
                )
        except Exception as exc:
            await conn.execute(
                """UPDATE aag_scan_runs
                      SET result='ingest_failed', finished_at=NOW(), error_code='ingest_failed',
                          error_detail=$2,
                          stage_status='{ "scan":"succeeded", "ingest":"failed" }'::jsonb
                    WHERE id=$1""",
                run_id, str(exc)[:2000],
            )
            raise

    return IngestResult(
        run_id=run_id,
        snapshot_id=snapshot_id,
        observation_id=observation_id,
        result=result,
        input_fingerprint=input_hash,
        content_fingerprint=content_hash,
        authoritative=authoritative,
        verification_status=verification_status,
    )
