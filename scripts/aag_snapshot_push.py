#!/usr/bin/env python3
"""AAG 그래프 산출물(reports/aag/*-graph.json) → DB `aag_graph_snapshots` 적재.

왜 있는가. 2026-09-19 실측 — 스캔은 6시간마다 돌아 `reports/aag/*-graph.json`
을 갱신하는데 `aag_graph_snapshots` 는 **0건**이었다. 적재하는 쪽이 아무도
없었다. 그래서 `aag_findings` 가 매번 local_graph 폴백으로만 답했고, 컨테이너
안 파일이 호스트와 어긋나면 이미 고친 결함을 그대로 보고했다(13.5시간 묵은
보고 1건, 같은 날 실측).

테이블이 없는 것이 원인이라고 읽기 쉽지만 아니다 — `app/api/aag.py` 의
`_ensure_table()` 이 런타임에 만든다. 정말 빈 곳은 **생산자**였다.

HTTP(`POST /api/v1/aag/snapshot`)를 쓰지 않는 이유: 그 경로는 테넌트 Bearer
토큰을 요구한다(401 실측). 이 스크립트는 DB 와 같은 호스트에서 도니 psql 로
직접 넣는다 — 토큰을 스크립트나 cron 에 두지 않는다(R-KEY).

사용:
    python3 scripts/aag_snapshot_push.py                      # reports/aag 전체
    python3 scripts/aag_snapshot_push.py GO100=/tmp/go100-graph.json

종료코드: 0=적재함 / 1=적재할 그래프 없음·psql 실패.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from pathlib import Path
from typing import Iterator
from uuid import uuid4

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from tools.aag.v2_contract import (  # noqa: E402
    canonical_json,
    content_fingerprint,
    graph_content,
    input_fingerprint,
)

GRAPH_DIR = REPO_ROOT / "reports" / "aag"
PG_CONTAINER = os.getenv("AAG_PG_CONTAINER", "aads-postgres")
PG_USER = os.getenv("AAG_PG_USER", "aads")
PG_DB = os.getenv("AAG_PG_DB", "aads")

# 달러 인용. JSON 본문에 따옴표·백슬래시가 그대로 들어 있어도 이스케이프가
# 필요 없다. 태그가 본문에 있으면 적재를 멈춘다 — 조용히 깨진 SQL 을 보내는
# 것보다 안 넣는 쪽이 낫다.
TAG = "$AAG$"


def _dollar(value: str) -> str:
    if TAG in value:
        raise ValueError("dollar-quote 태그가 본문에 있다 — 적재를 중단한다")
    return "%s%s%s" % (TAG, value, TAG)


def _legacy_statement(project: str, graph: dict) -> str:
    stats = graph.get("stats") or {}
    findings = graph.get("findings") or []
    unresolved = graph.get("unresolved") or []

    def dumps(value: object) -> str:
        return json.dumps(value, ensure_ascii=False)

    node_count = int(stats.get("graph_nodes") or len(graph.get("nodes") or []))
    edge_count = int(stats.get("graph_edges") or len(graph.get("edges") or []))
    return (
        "INSERT INTO aag_graph_snapshots (project, host, generated_at, commit_sha,"
        " stats, findings, unresolved, node_count, edge_count, finding_count)"
        " VALUES (%s, %s, %s::timestamptz, %s, %s::jsonb, %s::jsonb, %s::jsonb,"
        " %d, %d, %d)"
        " ON CONFLICT (project, generated_at) DO UPDATE SET host=EXCLUDED.host,"
        " commit_sha=EXCLUDED.commit_sha, stats=EXCLUDED.stats,"
        " findings=EXCLUDED.findings, unresolved=EXCLUDED.unresolved,"
        " node_count=EXCLUDED.node_count, edge_count=EXCLUDED.edge_count,"
        " finding_count=EXCLUDED.finding_count;"
        % (
            _dollar(project),
            _dollar(socket.gethostname()),
            _dollar(str(graph["generated_at"])),
            _dollar(str(graph.get("commit_sha") or "")),
            _dollar(dumps(stats)),
            _dollar(dumps(findings)),
            _dollar(dumps(unresolved)),
            node_count,
            edge_count,
            len(findings),
        )
    )


def _v2_statement(
    project: str, graph: dict, *, identity_project: str | None = None
) -> str | None:
    identity = graph.get("source_identity") or {}
    required = (
        "repository_id", "target_ref", "resolved_commit_sha", "scanner_version",
        "ruleset_digest", "scan_scope_digest", "normalization_version",
        "stable_key_version", "expected_target_ref_head_sha",
    )
    if any(not identity.get(key) for key in required):
        return None
    if "unknown" in {
        identity["resolved_commit_sha"], identity["expected_target_ref_head_sha"]
    }:
        return None

    body = graph_content(graph, project=identity_project or project)
    run_id = str(uuid4())
    content_hash = content_fingerprint(body)
    input_hash = input_fingerprint(
        project=project,
        repository_id=identity["repository_id"],
        target_ref=identity["target_ref"],
        resolved_commit_sha=identity["resolved_commit_sha"],
        expected_target_ref_head_sha=identity["expected_target_ref_head_sha"],
        scanner_version=identity["scanner_version"],
        ruleset_digest=identity["ruleset_digest"],
        scan_scope_digest=identity["scan_scope_digest"],
        parser_versions=identity.get("parser_versions") or {},
        normalization_version=identity["normalization_version"],
        worktree_digest=identity.get("worktree_digest"),
    )
    values = {
        "project": _dollar(project),
        "run_id": _dollar(run_id),
        "repository": _dollar(str(identity["repository_id"])),
        "ref": _dollar(str(identity["target_ref"])),
        "scope": _dollar(str(identity.get("governance_scope") or "default")),
        "commit": _dollar(str(identity["resolved_commit_sha"])),
        "expected_commit": _dollar(str(identity["expected_target_ref_head_sha"])),
        "input": _dollar(input_hash),
        "content": _dollar(content_hash),
        "scanner": _dollar(str(identity["scanner_version"])),
        "rules": _dollar(str(identity["ruleset_digest"])),
        "scan_scope": _dollar(str(identity["scan_scope_digest"])),
        "normalization": _dollar(str(identity["normalization_version"])),
        "stable_key": _dollar(str(identity["stable_key_version"])),
        "parsers": _dollar(canonical_json(identity.get("parser_versions") or {})),
        "host": _dollar(socket.gethostname()),
        "generated": _dollar(str(graph["generated_at"])),
        "stats": _dollar(canonical_json(body["stats"])),
        "nodes": _dollar(canonical_json(body["nodes"])),
        "edges": _dollar(canonical_json(body["edges"])),
        "findings": _dollar(canonical_json(body["findings"])),
        "unresolved": _dollar(canonical_json(body["unresolved"])),
    }
    return """
WITH scope_lock AS (
    SELECT pg_advisory_xact_lock(hashtextextended(
        {project} || '|' || {repository} || '|' || {ref} || '|' || {scope}, 0
    ))
), new_run AS (
    INSERT INTO aag_scan_runs
         (id, project, repository_id, target_ref, governance_scope, resolved_commit_sha,
         expected_target_ref_head_sha, input_fingerprint, scanner_version,
         ruleset_digest, scan_scope_digest,
         normalization_version, parser_versions, host, result, stage_status)
    SELECT {run_id}::uuid, {project}, {repository}, {ref}, {scope}, {commit}, {expected_commit},
            {input}, {scanner},
            {rules}, {scan_scope}, {normalization}, {parsers}::jsonb, {host},
            'running', '{{"scan":"succeeded","ingest":"running"}}'::jsonb
      FROM scope_lock
    RETURNING id
), inserted_snapshot AS (
    INSERT INTO aag_graph_snapshots_v2
        (project, repository_id, content_fingerprint, canonicalization_version,
         stable_key_version, stats, nodes, edges, findings, unresolved, node_count,
         edge_count, finding_count, generated_at, publish_status, first_published_at)
    VALUES ({project}, {repository}, {content}, {normalization}, {stable_key},
            {stats}::jsonb, {nodes}::jsonb, {edges}::jsonb, {findings}::jsonb,
            {unresolved}::jsonb, {node_count}, {edge_count}, {finding_count},
            {generated}::timestamptz, 'ready', NOW())
    ON CONFLICT (project, repository_id, content_fingerprint) DO NOTHING
    RETURNING id
), selected_snapshot AS (
    SELECT id, TRUE AS created FROM inserted_snapshot
    UNION ALL
    SELECT id, FALSE AS created FROM aag_graph_snapshots_v2
     WHERE project={project} AND repository_id={repository}
       AND content_fingerprint={content}
       AND NOT EXISTS (SELECT 1 FROM inserted_snapshot)
), new_observation AS (
    INSERT INTO aag_snapshot_observations
        (run_id, snapshot_id, project, repository_id, target_ref, governance_scope,
         resolved_commit_sha, input_fingerprint, content_fingerprint,
         expected_target_ref_head_sha, authoritative, result,
         verification_status, verified_at)
    SELECT r.id, s.id, {project}, {repository}, {ref}, {scope}, {commit}, {input},
           {content}, {expected_commit},
           ({commit} = {expected_commit} AND NOT EXISTS (
               SELECT 1 FROM aag_latest_pointers p
                WHERE p.project={project} AND p.repository_id={repository}
                  AND p.target_ref={ref} AND p.governance_scope={scope}
                  AND p.generated_at > {generated}::timestamptz
           )),
           CASE WHEN s.created THEN 'succeeded' ELSE 'no_change_success' END,
           CASE WHEN {commit} <> {expected_commit} THEN 'commit_mismatch'
                WHEN EXISTS (
                    SELECT 1 FROM aag_latest_pointers p
                     WHERE p.project={project} AND p.repository_id={repository}
                       AND p.target_ref={ref} AND p.governance_scope={scope}
                       AND p.generated_at > {generated}::timestamptz
                ) THEN 'out_of_order'
                ELSE 'verified' END, NOW()
      FROM new_run r CROSS JOIN selected_snapshot s
    RETURNING id, run_id, snapshot_id, result, verification_status, verified_at
), new_ref_head AS (
    INSERT INTO aag_ref_heads
        (project,repository_id,target_ref,governance_scope,head_commit_sha,
         generated_at,verified_at,observation_id,updated_at)
    SELECT {project},{repository},{ref},{scope},{commit},{generated}::timestamptz,
           o.verified_at,o.id,NOW()
      FROM new_observation o
     WHERE o.verification_status='verified'
    ON CONFLICT (project,repository_id,target_ref,governance_scope) DO UPDATE SET
        head_commit_sha=EXCLUDED.head_commit_sha,
        generated_at=EXCLUDED.generated_at,
        verified_at=EXCLUDED.verified_at,
        observation_id=EXCLUDED.observation_id,
        updated_at=NOW()
    WHERE EXCLUDED.generated_at >= aag_ref_heads.generated_at
    RETURNING observation_id
), new_pointer AS (
    INSERT INTO aag_latest_pointers
        (project,repository_id,target_ref,governance_scope,snapshot_id,observation_id,
         run_id,resolved_commit_sha,expected_target_ref_head_sha,generated_at,verified_at,updated_at)
    SELECT {project},{repository},{ref},{scope},o.snapshot_id,o.id,o.run_id,
           {commit},{expected_commit},{generated}::timestamptz,o.verified_at,NOW()
      FROM new_observation o
     WHERE o.verification_status='verified'
    ON CONFLICT (project,repository_id,target_ref,governance_scope) DO UPDATE SET
        snapshot_id=EXCLUDED.snapshot_id,
        observation_id=EXCLUDED.observation_id,
        run_id=EXCLUDED.run_id,
        resolved_commit_sha=EXCLUDED.resolved_commit_sha,
        expected_target_ref_head_sha=EXCLUDED.expected_target_ref_head_sha,
        generated_at=EXCLUDED.generated_at,
        verified_at=EXCLUDED.verified_at,
        updated_at=NOW()
    WHERE EXCLUDED.generated_at >= aag_latest_pointers.generated_at
    RETURNING run_id
)
SELECT COUNT(*) AS pointers_published FROM new_pointer;

-- A data-modifying CTE cannot UPDATE a row inserted by a sibling CTE in the
-- same statement snapshot. Finalize the explicit run id in a second statement.
UPDATE aag_scan_runs r
   SET result=CASE WHEN o.verification_status = 'verified' THEN o.result
                   WHEN o.verification_status = 'out_of_order' THEN 'out_of_order'
                   ELSE 'source_behind' END,
       finished_at=NOW(),
       stage_status='{{"scan":"succeeded","ingest":"succeeded"}}'::jsonb
  FROM aag_snapshot_observations o
 WHERE r.id={run_id}::uuid AND o.run_id=r.id;
""".format(
        **values,
        node_count=len(body["nodes"]),
        edge_count=len(body["edges"]),
        finding_count=len(body["findings"]),
    )


def _targets(argv: list) -> Iterator:
    """Yield (project, identity_project, path).

    "KIS@GO100=path" republishes path's already-scanned content under
    project=KIS while validating finding identity against GO100 — the
    project that actually produced the stable_finding_key values baked
    into the graph (shared_monorepo alias; see aag_all_projects_refresh.sh).
    """
    if argv:
        for item in argv:
            spec, _, path = item.partition("=")
            if not path:
                print("무시: %s — PROJECT=경로 형식이 아니다" % item, file=sys.stderr)
                continue
            project, _, identity_project = spec.partition("@")
            yield project.upper(), (identity_project.upper() or None), Path(path)
        return
    for path in sorted(GRAPH_DIR.glob("*-graph.json")):
        yield path.name[: -len("-graph.json")].upper(), None, path


def main() -> int:
    statements = []
    for project, identity_project, path in _targets(sys.argv[1:]):
        try:
            graph = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print("SKIP %s %s — %s" % (project, path, exc), file=sys.stderr)
            continue
        if not graph.get("generated_at"):
            print("SKIP %s — generated_at 이 비어 있다" % project, file=sys.stderr)
            continue
        try:
            statements.append(_legacy_statement(project, graph))
            v2_statement = _v2_statement(project, graph, identity_project=identity_project)
            if v2_statement:
                statements.append(v2_statement)
            else:
                print(
                    "V2 SKIP %s — source identity/commit is incomplete; legacy only" % project,
                    file=sys.stderr,
                )
        except ValueError as exc:
            print("SKIP %s — %s" % (project, exc), file=sys.stderr)
            continue
        print("PUSH %s generated=%s findings=%d nodes=%s edges=%s" % (
            project, graph["generated_at"], len(graph.get("findings") or []),
            (graph.get("stats") or {}).get("graph_nodes"),
            (graph.get("stats") or {}).get("graph_edges"),
        ))

    if not statements:
        print("적재할 그래프가 없다", file=sys.stderr)
        return 1

    sql = "BEGIN;\n%s\nCOMMIT;\n" % "\n".join(statements)
    try:
        proc = subprocess.run(
            ["docker", "exec", "-i", PG_CONTAINER,
             "psql", "-v", "ON_ERROR_STOP=1", "-q", "-U", PG_USER, "-d", PG_DB],
            input=sql, text=True, capture_output=True, timeout=180,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print("psql 실행 실패: %s" % exc, file=sys.stderr)
        return 1
    sys.stdout.write(proc.stdout)
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr)
        return 1
    print("OK %d SQL statements applied" % len(statements))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
