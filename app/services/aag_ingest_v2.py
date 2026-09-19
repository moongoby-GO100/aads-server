"""Atomic AAG v1.1 run/snapshot/observation/latest-pointer ingestion."""
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


def _recorded_result(run_id: UUID, row: Any, input_hash: str, content_hash: str) -> IngestResult:
    return IngestResult(
        run_id=run_id, snapshot_id=row["snapshot_id"], observation_id=row["observation_id"],
        result=row["result"], input_fingerprint=input_hash,
        content_fingerprint=row["content_fingerprint"] or content_hash,
        authoritative=bool(row["authoritative"]),
        verification_status=row["verification_status"] or "unknown",
    )


async def ingest_graph(
    *, project: str, repository_id: str, target_ref: str, governance_scope: str,
    resolved_commit_sha: str, expected_target_ref_head_sha: str, host: str,
    generated_at: datetime, scanner_version: str, ruleset_digest: str,
    scan_scope_digest: str, parser_versions: dict[str, Any], graph: dict[str, Any],
    worktree_digest: str | None = None,
    canonicalization_version: str = CANONICALIZATION_VERSION,
    stable_key_version: str = STABLE_KEY_VERSION,
    run_id: UUID | None = None,
) -> IngestResult:
    """Publish candidate -> ready -> observation -> pointer in one transaction."""
    project = project.strip().upper()
    repository_id, target_ref = repository_id.strip(), target_ref.strip()
    governance_scope = governance_scope.strip()
    run_id = run_id or uuid4()
    graph_body = graph_content(graph)
    content_hash = content_fingerprint(graph_body)
    input_hash = input_fingerprint(
        project=project, repository_id=repository_id, target_ref=target_ref,
        governance_scope=governance_scope,
        resolved_commit_sha=resolved_commit_sha,
        expected_target_ref_head_sha=expected_target_ref_head_sha,
        scanner_version=scanner_version, ruleset_digest=ruleset_digest,
        scan_scope_digest=scan_scope_digest, parser_versions=parser_versions,
        normalization_version=canonicalization_version, worktree_digest=worktree_digest,
    )
    stats, findings = graph_body["stats"], graph_body["findings"]
    nodes, edges = graph_body["nodes"], graph_body["edges"]
    unresolved = graph_body["unresolved"]
    scope_key = f"{project}\x1f{repository_id}\x1f{target_ref}\x1f{governance_scope}"

    async with get_pool().acquire() as conn:
        try:
            async with conn.transaction():
                await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", scope_key)
                inserted = await conn.fetchval(
                    """INSERT INTO aag_scan_runs
                           (id, project, repository_id, target_ref, governance_scope,
                            resolved_commit_sha, expected_target_ref_head_sha,
                            input_fingerprint, scanner_version, ruleset_digest,
                            scan_scope_digest, normalization_version, parser_versions,
                            host, result, stage_status)
                         VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13::jsonb,$14,
                                 'running','{"scan":"succeeded","ingest":"running"}'::jsonb)
                         ON CONFLICT (id) DO NOTHING RETURNING id""",
                    run_id, project, repository_id, target_ref, governance_scope,
                    resolved_commit_sha, expected_target_ref_head_sha, input_hash,
                    scanner_version, ruleset_digest, scan_scope_digest,
                    canonicalization_version, json.dumps(parser_versions), host,
                )
                if inserted is None:
                    existing = await conn.fetchrow(
                        """SELECT r.input_fingerprint, r.result, o.snapshot_id,
                                  o.id AS observation_id, o.content_fingerprint,
                                  o.authoritative, o.verification_status
                             FROM aag_scan_runs r
                        LEFT JOIN aag_snapshot_observations o ON o.run_id=r.id
                            WHERE r.id=$1""", run_id,
                    )
                    if not existing or existing["input_fingerprint"] != input_hash:
                        raise IngestConflictError("run_id already exists with different input")
                    return _recorded_result(run_id, existing, input_hash, content_hash)

                previous = await conn.fetchrow(
                    """SELECT p.generated_at, p.resolved_commit_sha,
                              h.head_commit_sha, h.generated_at AS head_generated_at
                         FROM aag_latest_pointers p
                    LEFT JOIN aag_ref_heads h USING
                              (project, repository_id, target_ref, governance_scope)
                        WHERE p.project=$1 AND p.repository_id=$2 AND p.target_ref=$3
                          AND p.governance_scope=$4 FOR UPDATE OF p""",
                    project, repository_id, target_ref, governance_scope,
                )
                commit_matches = resolved_commit_sha == expected_target_ref_head_sha
                ordered = (
                    previous is None
                    or generated_at > previous["generated_at"]
                    or (
                        generated_at == previous["generated_at"]
                        and resolved_commit_sha == previous["resolved_commit_sha"]
                    )
                )
                authoritative = commit_matches and ordered
                if not commit_matches:
                    result, verification_status = "source_behind", "commit_mismatch"
                elif not ordered:
                    result, verification_status = "out_of_order", "out_of_order"
                else:
                    result, verification_status = "succeeded", "verified"

                snapshot_id = await conn.fetchval(
                    """INSERT INTO aag_graph_snapshots_v2
                           (project, repository_id, content_fingerprint,
                            canonicalization_version, stable_key_version, stats,
                            nodes, edges, findings, unresolved, node_count,
                            edge_count, finding_count, generated_at, publish_status)
                         VALUES ($1,$2,$3,$4,$5,$6::jsonb,$7::jsonb,$8::jsonb,
                                 $9::jsonb,$10::jsonb,$11,$12,$13,$14,'candidate')
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
                else:
                    await conn.execute(
                        """UPDATE aag_graph_snapshots_v2
                              SET publish_status='ready', first_published_at=NOW()
                            WHERE id=$1 AND publish_status='candidate'""", snapshot_id,
                    )

                observation_result = "succeeded" if created else "no_change_success"
                if authoritative:
                    result = observation_result
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
                if authoritative:
                    await conn.execute(
                        """INSERT INTO aag_ref_heads
                               (project, repository_id, target_ref, governance_scope,
                                head_commit_sha, generated_at, verified_at, observation_id)
                             VALUES ($1,$2,$3,$4,$5,$6,NOW(),$7)
                             ON CONFLICT (project, repository_id, target_ref, governance_scope)
                             DO UPDATE SET head_commit_sha=EXCLUDED.head_commit_sha,
                               generated_at=EXCLUDED.generated_at,
                               verified_at=EXCLUDED.verified_at,
                               observation_id=EXCLUDED.observation_id, updated_at=NOW()""",
                        project, repository_id, target_ref, governance_scope,
                        resolved_commit_sha, generated_at, observation_id,
                    )
                    await conn.execute(
                        """INSERT INTO aag_latest_pointers
                               (project, repository_id, target_ref, governance_scope,
                                snapshot_id, observation_id, run_id, resolved_commit_sha,
                                expected_target_ref_head_sha, generated_at, verified_at)
                             VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,NOW())
                             ON CONFLICT (project, repository_id, target_ref, governance_scope)
                             DO UPDATE SET snapshot_id=EXCLUDED.snapshot_id,
                               observation_id=EXCLUDED.observation_id, run_id=EXCLUDED.run_id,
                               resolved_commit_sha=EXCLUDED.resolved_commit_sha,
                               expected_target_ref_head_sha=EXCLUDED.expected_target_ref_head_sha,
                               generated_at=EXCLUDED.generated_at,
                               verified_at=EXCLUDED.verified_at, updated_at=NOW()""",
                        project, repository_id, target_ref, governance_scope,
                        snapshot_id, observation_id, run_id, resolved_commit_sha,
                        expected_target_ref_head_sha, generated_at,
                    )
                await conn.execute(
                    """UPDATE aag_scan_runs SET result=$2, finished_at=NOW(),
                              stage_status='{"scan":"succeeded","ingest":"succeeded"}'::jsonb
                            WHERE id=$1""", run_id, result,
                )
        except IngestConflictError:
            raise
        except Exception as exc:
            await conn.execute(
                """INSERT INTO aag_scan_runs
                       (id, project, repository_id, target_ref, governance_scope,
                        resolved_commit_sha, expected_target_ref_head_sha,
                        input_fingerprint, scanner_version, ruleset_digest,
                        scan_scope_digest, normalization_version, parser_versions,
                        host, result, stage_status, error_code, error_detail, finished_at)
                     VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13::jsonb,$14,
                             'ingest_failed','{"scan":"succeeded","ingest":"failed"}'::jsonb,
                             'ingest_failed',$15,NOW())
                     ON CONFLICT (id) DO UPDATE SET result='ingest_failed',
                       stage_status=EXCLUDED.stage_status, error_code=EXCLUDED.error_code,
                       error_detail=EXCLUDED.error_detail, finished_at=EXCLUDED.finished_at
                     WHERE aag_scan_runs.input_fingerprint=EXCLUDED.input_fingerprint""",
                run_id, project, repository_id, target_ref, governance_scope,
                resolved_commit_sha, expected_target_ref_head_sha, input_hash,
                scanner_version, ruleset_digest, scan_scope_digest,
                canonicalization_version, json.dumps(parser_versions), host, str(exc)[:2000],
            )
            raise

    return IngestResult(
        run_id=run_id, snapshot_id=snapshot_id, observation_id=observation_id,
        result=result, input_fingerprint=input_hash, content_fingerprint=content_hash,
        authoritative=authoritative, verification_status=verification_status,
    )
