#!/usr/bin/env python3
"""구조화된 기록을 지식 그래프로 잇는다.

2026-09-14. 이미 갖고 있는 것부터 잇는다.

문서에서 개체와 관계를 뽑으려면 문서 823건마다 LLM 을 불러야 하고, 이
서버는 GPU 가 없다. 그런데 **이미 선으로 연결된 데이터**가 있다.

    ohvis_wiki_error_book          19건   증상→원인→예방→고친커밋→고친파일
    chat_workspace_change_ledger 4,725건  파일→커밋→푸시→배포
    deploy_runs                   430건   배포→릴리스SHA→성공여부
    doc_chunks                  7,847조각 문서→그 안에 언급된 파일 경로

추출 비용이 0 이고 정확도가 100% 다. 문서에서 뽑은 관계는 틀릴 수 있지만
이것들은 기계가 남긴 사실이다. 여기서 시작해 나중에 LLM 으로 넓힌다.

기존 `kg_entities` / `kg_relations` 를 그대로 쓴다 — 새 테이블을 만들면
`app/core/knowledge_graph.py` 가 쌓아 온 145개체·628관계와 갈라진다.

    build_kg.py build     구조화 소스에서 노드·엣지 생성 (재실행 안전)
    build_kg.py stats     현황
    build_kg.py trace <파일|커밋|오류키>   선을 따라가 본다

**서버별 사본을 만들지 마라** — error_book.py 와 같은 규약이다.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys

PG_CONTAINER = "aads-postgres"
PG_PASSWORD = "aads2026secure"

# 이 스크립트가 만든 것과 사람/LLM 이 만든 것을 구분한다. 재구축 때
# 내가 만든 것만 지우기 위해서다.
ORIGIN = "structured_ledger"


def _psql_argv() -> tuple[list[str], dict]:
    env = dict(os.environ)
    host = env.get("PGHOST", "")
    if host:
        return ([
            "psql", "-h", host, "-p", env.get("PGPORT", "5432"),
            "-U", env.get("PGUSER", "aads"), "-d", env.get("PGDATABASE", "aads"),
        ], env)
    env["PGPASSWORD"] = PG_PASSWORD
    return ([
        "docker", "exec", "-i", "-e", f"PGPASSWORD={PG_PASSWORD}", PG_CONTAINER,
        "psql", "-U", "aads", "-d", "aads",
    ], env)


def psql(sql: str) -> str:
    argv, env = _psql_argv()
    proc = subprocess.run(
        argv + ["-At", "-F", "\x1f", "-f", "-"],
        input=sql, text=True, capture_output=True, timeout=300, env=env,
    )
    if proc.returncode != 0:
        print(f"[build_kg] DB 오류: {proc.stderr.strip()[:400]}", file=sys.stderr)
        raise SystemExit(2)
    return proc.stdout


def lit(v) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, (int, float)):
        return str(v)
    return "'" + str(v).replace("\\", "\\\\").replace("'", "''") + "'"


def rows(sql: str) -> list[list[str]]:
    return [l.split("\x1f") for l in psql(sql).splitlines() if l.strip()]


# ── 노드·엣지 모으기 ────────────────────────────────────────────────────

class Graph:
    def __init__(self) -> None:
        self.nodes: dict[tuple[str, str], dict] = {}
        self.edges: dict[tuple[str, str, str, str, str], dict] = {}

    def node(self, etype: str, name: str, *, desc: str = "", project: str = "") -> None:
        name = (name or "").strip()
        if not name:
            return
        key = (etype, name[:255])
        prev = self.nodes.get(key)
        if prev is None:
            self.nodes[key] = {"desc": desc[:2000], "project": project}
        elif desc and not prev["desc"]:
            prev["desc"] = desc[:2000]

    def edge(self, st: str, sn: str, rel: str, tt: str, tn: str,
             *, evidence: str = "", weight: float = 1.0) -> None:
        sn, tn = (sn or "").strip()[:255], (tn or "").strip()[:255]
        if not sn or not tn or (st, sn) == (tt, tn):
            return
        key = (st, sn, rel, tt, tn)
        if key not in self.edges:
            self.edges[key] = {"evidence": evidence[:500], "weight": weight}


_SHA = re.compile(r"^[0-9a-f]{7,40}$")


def short(sha: str) -> str:
    sha = (sha or "").strip().lower()
    return sha[:12] if _SHA.match(sha) else ""


def basename(path: str) -> str:
    return (path or "").rstrip("/").split("/")[-1]


def collect_error_book(g: Graph) -> int:
    """오류 사전 — 이미 증상·원인·예방·고친커밋·고친파일이 이어져 있다."""
    n = 0
    for r in rows("""
        SELECT error_key, coalesce(symptom,''), coalesce(root_cause,''),
               coalesce(project,''), coalesce(metadata::text,'{}')
        FROM ohvis_wiki_error_book;
    """):
        if len(r) < 5:
            continue
        key, symptom, cause, project, meta_raw = r[0], r[1], r[2], r[3], r[4]
        g.node("error", key, desc=symptom or cause, project=project)
        n += 1
        try:
            meta = json.loads(meta_raw)
        except Exception:
            meta = {}
        fix = meta.get("fix") or {}
        for sha in fix.get("commits") or []:
            s = short(str(sha))
            if s:
                g.node("commit", s, desc=str(fix.get("note") or "")[:300])
                g.edge("error", key, "resolved_by", "commit", s,
                       evidence="오류 사전 fix.commits")
        for path in fix.get("files") or []:
            p = str(path).strip()
            if p:
                g.node("file", p)
                g.edge("error", key, "affects", "file", p,
                       evidence="오류 사전 fix.files")
    return n


def collect_change_ledger(g: Graph, days: int) -> int:
    """변경 원장 — 파일을 누가 언제 고쳤고 어느 커밋으로 올렸나."""
    n = 0
    for r in rows(f"""
        SELECT coalesce(file_path,''), coalesce(left(commit_sha,12),''),
               coalesce(project,''), coalesce(left(commit_message,200),''),
               coalesce(status,'')
        FROM chat_workspace_change_ledger
        WHERE created_at > now() - interval '{int(days)} days'
          AND file_path IS NOT NULL AND file_path <> '';
    """):
        if len(r) < 5:
            continue
        path, sha, project, msg, status = r[0], short(r[1]), r[2], r[3], r[4]
        g.node("file", path, project=project)
        n += 1
        if sha:
            g.node("commit", sha, desc=msg, project=project)
            g.edge("commit", sha, "modifies", "file", path,
                   evidence=f"변경 원장 ({status})")
    return n


def collect_deploys(g: Graph, days: int) -> int:
    """배포 원장 — 어느 커밋이 언제 운영에 올라갔나."""
    n = 0
    for r in rows(f"""
        SELECT id::text, coalesce(left(release_sha,12),''), coalesce(status,''),
               coalesce(project,''), to_char(created_at,'YYYY-MM-DD HH24:MI')
        FROM deploy_runs
        WHERE created_at > now() - interval '{int(days)} days';
    """):
        if len(r) < 5:
            continue
        did, sha, status, project, at = r[0], short(r[1]), r[2], r[3], r[4]
        name = f"deploy#{did}"
        g.node("deploy", name, desc=f"{at} {status}", project=project)
        n += 1
        if sha:
            g.node("commit", sha, project=project)
            g.edge("commit", sha, "deployed_in", "deploy", name,
                   evidence=f"배포 {status}",
                   weight=1.0 if status == "success" else 0.4)
    return n


def collect_doc_mentions(g: Graph) -> int:
    """문서 → 그 안에 언급된 파일.

    "이 파일에 대한 문서가 어디 있나" 가 지금 제일 답하기 어려운 질문이다.
    본문에서 경로처럼 생긴 문자열만 뽑는다 — LLM 없이 되는 범위다.
    """
    known = {name for (etype, name) in g.nodes if etype == "file"}
    by_base: dict[str, list[str]] = {}
    for p in known:
        by_base.setdefault(basename(p), []).append(p)

    n = 0
    seen: set[tuple[str, str]] = set()
    for r in rows("""
        SELECT doc_path, coalesce(title,''), content
        FROM doc_chunks
        WHERE content ~ '[A-Za-z0-9_/.-]+\\.(py|tsx|ts|sh|sql|md)'
        LIMIT 6000;
    """):
        if len(r) < 3:
            continue
        doc_path, title, content = r[0], r[1], r[2]
        g.node("doc", doc_path, desc=title)
        for m in re.finditer(r"[A-Za-z0-9_][A-Za-z0-9_/.-]{2,90}\.(?:py|tsx|ts|sh|sql)", content):
            cand = m.group(0)
            base = basename(cand)
            # 저장소에 실제로 있는 파일만 잇는다. 아무 문자열이나 이으면
            # 그래프가 쓰레기로 찬다 — 잘못된 관계는 계속 전파된다.
            targets = by_base.get(base)
            if not targets or len(targets) > 3:
                continue
            for t in targets:
                k = (doc_path, t)
                if k in seen:
                    continue
                seen.add(k)
                g.edge("doc", doc_path, "documents", "file", t,
                       evidence="문서 본문에 경로 언급")
                n += 1
    return n


# ── 저장 ────────────────────────────────────────────────────────────────

def persist(g: Graph) -> tuple[int, int]:
    # 이 스크립트가 만든 엣지만 지운다. 사람/LLM 이 만든 것은 남긴다.
    psql(f"DELETE FROM kg_relations WHERE evidence LIKE {lit('[' + ORIGIN + ']%')};")

    items = list(g.nodes.items())
    for i in range(0, len(items), 200):
        vals = ",".join(
            f"({lit(etype)},{lit(name)},{lit(v['desc'])},{lit(v['project'] or None)},now(),now())"
            for (etype, name), v in items[i:i + 200]
        )
        psql(
            "INSERT INTO kg_entities (entity_type,name,description,project,created_at,updated_at) "
            f"VALUES {vals} "
            "ON CONFLICT (entity_type,name) DO UPDATE SET "
            "description = CASE WHEN kg_entities.description IS NULL OR kg_entities.description='' "
            "THEN EXCLUDED.description ELSE kg_entities.description END, "
            "updated_at = now();"
        )

    edges = list(g.edges.items())
    for i in range(0, len(edges), 200):
        parts = []
        for (st, sn, rel, tt, tn), v in edges[i:i + 200]:
            parts.append(
                "((SELECT id FROM kg_entities WHERE entity_type=" + lit(st) + " AND name=" + lit(sn) + "),"
                "(SELECT id FROM kg_entities WHERE entity_type=" + lit(tt) + " AND name=" + lit(tn) + "),"
                + lit(rel) + "," + str(float(v["weight"])) + ","
                + lit(f"[{ORIGIN}] {v['evidence']}") + ",now())"
            )
        psql(
            "INSERT INTO kg_relations (source_entity_id,target_entity_id,relation_type,weight,evidence,created_at) "
            f"SELECT * FROM (VALUES {','.join(parts)}) AS v(s,t,r,w,e,c) WHERE s IS NOT NULL AND t IS NOT NULL "
            "ON CONFLICT (source_entity_id,target_entity_id,relation_type) DO UPDATE SET "
            "evidence = EXCLUDED.evidence, weight = EXCLUDED.weight;"
        )
    return len(items), len(edges)


# ── 명령 ────────────────────────────────────────────────────────────────

def cmd_build(args) -> None:
    g = Graph()
    print(f"[build_kg] 오류 사전       {collect_error_book(g):>6,}건")
    print(f"[build_kg] 변경 원장       {collect_change_ledger(g, args.days):>6,}건")
    print(f"[build_kg] 배포 원장       {collect_deploys(g, args.days):>6,}건")
    print(f"[build_kg] 문서→파일 언급  {collect_doc_mentions(g):>6,}건")
    nodes, edges = persist(g)
    print(f"[build_kg] 저장: 노드 {nodes:,} / 엣지 {edges:,}")


def cmd_stats(_args) -> None:
    print("── 노드 ──")
    for r in rows("SELECT entity_type, count(*) FROM kg_entities GROUP BY 1 ORDER BY 2 DESC;"):
        print(f"  {r[0]:<12} {int(r[1]):>6,}")
    print("── 관계 ──")
    for r in rows("SELECT relation_type, count(*) FROM kg_relations GROUP BY 1 ORDER BY 2 DESC;"):
        print(f"  {r[0]:<14} {int(r[1]):>6,}")


def cmd_trace(args) -> None:
    """선을 따라가 본다 — 그래프가 실제로 쓸모 있는지 보는 가장 빠른 방법."""
    target = args.target
    found = rows(
        "SELECT id::text, entity_type, name FROM kg_entities "
        f"WHERE name = {lit(target)} OR name LIKE {lit('%' + target + '%')} "
        "ORDER BY length(name) LIMIT 5;"
    )
    if not found:
        print(f"[build_kg] '{target}' 를 그래프에서 못 찾음")
        return
    for eid, etype, name in [(r[0], r[1], r[2]) for r in found[:1]]:
        print(f"◆ {etype} · {name}\n")
        out = rows(f"""
            SELECT r.relation_type, e2.entity_type, e2.name, coalesce(left(e2.description,70),''),
                   coalesce(left(r.evidence,60),'')
            FROM kg_relations r JOIN kg_entities e2 ON e2.id = r.target_entity_id
            WHERE r.source_entity_id = {eid} ORDER BY r.relation_type LIMIT 30;
        """)
        for rr in out:
            print(f"  ──{rr[0]}──▶ [{rr[1]}] {rr[2]}")
            if rr[3]:
                print(f"                {rr[3]}")
        inc = rows(f"""
            SELECT r.relation_type, e1.entity_type, e1.name, coalesce(left(e1.description,70),'')
            FROM kg_relations r JOIN kg_entities e1 ON e1.id = r.source_entity_id
            WHERE r.target_entity_id = {eid} ORDER BY r.relation_type LIMIT 30;
        """)
        for rr in inc:
            print(f"  ◀──{rr[0]}── [{rr[1]}] {rr[2]}")
            if rr[3]:
                print(f"                {rr[3]}")


def main() -> None:
    ap = argparse.ArgumentParser(description="구조화된 기록 → 지식 그래프")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--days", type=int, default=60, help="원장에서 몇 일치를 볼지")
    b.set_defaults(fn=cmd_build)
    sub.add_parser("stats").set_defaults(fn=cmd_stats)
    t = sub.add_parser("trace")
    t.add_argument("target")
    t.set_defaults(fn=cmd_trace)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
