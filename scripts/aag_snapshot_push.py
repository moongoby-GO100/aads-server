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

REPO_ROOT = Path(__file__).resolve().parents[1]
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


def _statement(project: str, graph: dict) -> str:
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


def _targets(argv: list) -> Iterator:
    if argv:
        for item in argv:
            project, _, path = item.partition("=")
            if not path:
                print("무시: %s — PROJECT=경로 형식이 아니다" % item, file=sys.stderr)
                continue
            yield project.upper(), Path(path)
        return
    for path in sorted(GRAPH_DIR.glob("*-graph.json")):
        yield path.name[: -len("-graph.json")].upper(), path


def main() -> int:
    statements = []
    for project, path in _targets(sys.argv[1:]):
        try:
            graph = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print("SKIP %s %s — %s" % (project, path, exc), file=sys.stderr)
            continue
        if not graph.get("generated_at"):
            print("SKIP %s — generated_at 이 비어 있다" % project, file=sys.stderr)
            continue
        try:
            statements.append(_statement(project, graph))
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
    print("OK %d건 적재" % len(statements))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
