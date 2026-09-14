#!/usr/bin/env python3
"""채팅 메시지 임베딩 백필 — 담당들이 서로의 결과를 볼 수 있게 만든다.

## 왜 필요한가

`ask_session` 으로 담당끼리 묻고 답하는 길은 열렸다. 그런데 **묻지 않아도
보이는** 길(Auto-RAG 크로스 세션 검색)은 벡터가 가짜라서 동작하지 않았다.

2026-09-14 실측. `chat_messages` 에 벡터가 33,140건 있었고 그중 진짜는
**65건**이었다. 나머지는 임베딩 경로가 컨테이너에서 안 닿는 상태로
`_dummy_embedding()` 이 돌려준 해시 더미가 진짜인 것처럼 저장된 것이다.
차원도 값 분포도 같아서 유사도 숫자는 그럴듯하게 나온다 — 의미만 없다.

    담당      메시지   벡터있음   더미
    파동엔진    912      741      610
    전략카드    974      606      529
    데이터      1117     634      517
    ...

그러니 주도가 "파동엔진이 뭘 하고 있지" 를 물었을 때 붙는 근거는 난수였다.

## 쓰는 법

    backfill_chat_embeddings.py status              현황만 본다
    backfill_chat_embeddings.py run --roster        #310 담당 8명 먼저
    backfill_chat_embeddings.py run --workspace GO100
    backfill_chat_embeddings.py run --all --max-seconds 7200

중단해도 안전하다 — 채운 것만 `embedding_ver=2` 로 남고 다음 실행이 이어간다.

## 두 가지 규약

**더미를 저장하지 않는다.** 임베딩이 실패하면 실패로 둔다. 이 사고의 원인이
바로 "실패를 조용히 그럴듯한 값으로 덮은 것" 이었다.

**접두어를 붙인다.** nomic-embed-text 는 문서에 `search_document: `,
질문에 `search_query: ` 를 요구한다. 2026-09-14 실측에서 구분 폭이 4배
차이났다. 검색하는 쪽(`app/services/chat_embedding_service`)과 짝이
맞아야 한다 — 한쪽만 바꾸면 검색이 조용히 나빠진다.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request

PG_CONTAINER = "aads-postgres"
PG_PASSWORD = "aads2026secure"

OLLAMA_URL = os.getenv("LOCAL_OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
EMBED_MODEL = os.getenv("CHAT_EMBED_MODEL", "nomic-embed-text")
EMBED_DIM = 768
EMBED_BATCH = int(os.getenv("CHAT_EMBED_BATCH", "4"))
EMBED_PARALLEL = int(os.getenv("CHAT_EMBED_PARALLEL", "3"))
EMBED_TIMEOUT = int(os.getenv("CHAT_EMBED_TIMEOUT", "300"))
EMBED_TEXT_CHARS = int(os.getenv("CHAT_EMBED_TEXT_CHARS", "1600"))

DOC_PREFIX = "search_document: "
EMBED_VER = 2

# #310 하네스 담당 8명. 이들 사이의 상호 가시성이 이 백필의 목적이다.
ROSTER = (
    "StrategyCardLead", "WaveEngineOwner", "LiveTradingOwner", "DataEngineOwner",
    "StockDiscoveryOwner", "BacktestEngineOwner", "MLModelOwner", "OpsInfraOwner",
)


def _psql_argv() -> tuple[list[str], dict]:
    """error_book.py / index_docs.py 와 같은 규약. 서버별 사본을 만들지 마라."""
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
        print(f"[backfill] DB 오류: {proc.stderr.strip()[:400]}", file=sys.stderr)
        raise SystemExit(2)
    return proc.stdout


def lit(v) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, (int, float)):
        return str(v)
    return "'" + str(v).replace("\\", "\\\\").replace("'", "''") + "'"


def ollama_embed(texts: list[str]) -> list[list[float]] | None:
    """진짜 임베딩만 돌려준다. 실패하면 None — 더미를 만들지 않는다."""
    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/embed",
        data=json.dumps({"model": EMBED_MODEL, "input": texts}).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=EMBED_TIMEOUT) as r:
            body = json.loads(r.read())
    except Exception as exc:
        print(f"[backfill] 임베딩 실패 ({type(exc).__name__}): {str(exc)[:120]}", file=sys.stderr)
        return None
    vectors = body.get("embeddings") or []
    if len(vectors) != len(texts):
        print(f"[backfill] 개수 불일치 {len(vectors)}/{len(texts)}", file=sys.stderr)
        return None
    for v in vectors:
        if len(v) != EMBED_DIM:
            print(f"[backfill] 차원 불일치 {len(v)} != {EMBED_DIM}", file=sys.stderr)
            return None
    return vectors


def _scope_sql(args) -> str:
    """어디를 채울지. 좁은 것부터 — 전부 채우려면 50시간이 든다."""
    if args.session:
        return f"AND m.session_id = {lit(args.session)}::uuid"
    if args.roster:
        keys = ",".join(lit(r) for r in ROSTER)
        return f"AND s.role_key IN ({keys})"
    if args.workspace:
        return (
            "AND s.workspace_id IN (SELECT id FROM chat_workspaces "
            f"WHERE project_key = {lit(args.workspace.upper())} "
            f"   OR name ILIKE {lit('%' + args.workspace + '%')})"
        )
    return ""


_TARGET = (
    "m.embedding_ver IS DISTINCT FROM {ver} "
    "AND length(m.content) >= 10 "
    "AND m.deleted_at IS NULL AND m.is_hidden = false"
)


def cmd_status(args) -> None:
    print(psql(f"""

SELECT coalesce(s.role_key, '(그 외)') AS 담당,
       count(*) AS 대상,
       count(*) FILTER (WHERE m.embedding_ver = {EMBED_VER}) AS 완료,
       count(*) FILTER (WHERE m.embedding IS NULL) AS 벡터없음,
       count(*) FILTER (WHERE m.embedding IS NOT NULL
                          AND m.embedding_ver IS DISTINCT FROM {EMBED_VER}) AS 재작성필요
FROM chat_messages m JOIN chat_sessions s ON s.id = m.session_id
WHERE length(m.content) >= 10 AND m.deleted_at IS NULL AND m.is_hidden = false
  AND (s.role_key IN ({",".join(lit(r) for r in ROSTER)}) OR {'true' if args.all else 'false'})
GROUP BY 1 ORDER BY 2 DESC LIMIT 20;
""").replace("\x1f", " | "))


def cmd_run(args) -> None:
    scope = _scope_sql(args)
    target = _TARGET.format(ver=EMBED_VER)
    deadline = time.time() + args.max_seconds
    budget = args.limit or 10**9
    done = 0
    t0 = time.time()

    total = psql(
        f"SELECT count(*) FROM chat_messages m JOIN chat_sessions s ON s.id = m.session_id "
        f"WHERE {target} {scope};"
    ).strip()
    print(f"[backfill] 대상 {total}건, 상한 {args.max_seconds}초")

    while done < budget:
        if time.time() > deadline:
            print(f"[backfill] 시간 상한({args.max_seconds}초) 도달 — 중단. 다시 실행하면 이어간다.")
            break

        rows = []
        take = min(EMBED_BATCH * EMBED_PARALLEL, budget - done)
        # 최근 것부터. 오래된 대화보다 지금 진행 중인 작업이 먼저 보여야 한다.
        for line in psql(
            f"SELECT m.id::text, coalesce(s.title,''), m.role, left(m.content, {EMBED_TEXT_CHARS}) "
            f"FROM chat_messages m JOIN chat_sessions s ON s.id = m.session_id "
            f"WHERE {target} {scope} "
            f"ORDER BY m.created_at DESC LIMIT {int(take)};"
        ).splitlines():
            parts = line.split("\x1f")
            if len(parts) >= 4:
                rows.append(parts)
        if not rows:
            print("[backfill] 남은 대상 없음 — 완료")
            break

        groups = [rows[i:i + EMBED_BATCH] for i in range(0, len(rows), EMBED_BATCH)]

        def run(group):
            # 세션 제목을 본문 앞에 붙인다. 본문만 넣으면 "어느 담당의 말인지"가
            # 벡터에 안 들어가서, 담당이 8명으로 늘어난 지금 엉뚱한 쪽이 잡힌다.
            texts = [
                DOC_PREFIX + f"[{g[1]}] {g[2]}\n{g[3]}"[:EMBED_TEXT_CHARS]
                for g in group
            ]
            return group, ollama_embed(texts)

        wrote = 0
        with cf.ThreadPoolExecutor(max_workers=EMBED_PARALLEL) as ex:
            for group, vectors in ex.map(run, groups):
                if not vectors:
                    continue
                stmts = []
                for g, vec in zip(group, vectors):
                    body = "[" + ",".join(f"{x:.7g}" for x in vec) + "]"
                    stmts.append(
                        f"UPDATE chat_messages SET embedding={lit(body)}::vector, "
                        f"embedding_ver={EMBED_VER} WHERE id={lit(g[0])}::uuid;"
                    )
                if stmts:
                    psql("BEGIN;" + "".join(stmts) + "COMMIT;")
                    wrote += len(stmts)

        if wrote == 0:
            # 임베딩 경로가 죽었다는 뜻이다. 계속 돌면 DB 만 두드린다.
            print("[backfill] 이번 회차에 하나도 못 채웠다 — 중단", file=sys.stderr)
            raise SystemExit(3)

        done += wrote
        remain = psql(
            f"SELECT count(*) FROM chat_messages m JOIN chat_sessions s ON s.id = m.session_id "
            f"WHERE {target} {scope};"
        ).strip()
        rate = done / max(1e-6, time.time() - t0)
        eta = int(int(remain or 0) / rate / 60) if rate > 0 else -1
        print(f"[backfill] {done}건 완료, 남음 {remain}, 시간당 {rate*3600:.0f}건, 예상 {eta}분", flush=True)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    st = sub.add_parser("status", help="현황")
    st.add_argument("--all", action="store_true", help="담당 8명 밖까지 전부")
    st.set_defaults(func=cmd_status)

    rn = sub.add_parser("run", help="채우기")
    g = rn.add_mutually_exclusive_group()
    g.add_argument("--roster", action="store_true", help="#310 담당 8명 (기본으로 권장)")
    g.add_argument("--workspace", help="워크스페이스 project_key 또는 이름 일부")
    g.add_argument("--session", help="세션 id 하나만")
    g.add_argument("--all", action="store_true", help="전부 — 50시간 든다")
    rn.add_argument("--limit", type=int, default=0, help="이번 실행에서 채울 최대 건수")
    rn.add_argument("--max-seconds", type=int, default=7200,
                    help="시간 상한 (R-BG: 끝날 시점을 모르면 띄우면 안 된다)")
    rn.set_defaults(func=cmd_run)

    return ap


def main_argv(argv: list[str] | None = None) -> None:
    ap = build_parser()
    args = ap.parse_args(argv)
    if args.cmd == "run" and not (args.roster or args.workspace or args.session or args.all):
        # 범위 없이 돌면 33,000건 = 34시간이다. 끝날 시점을 모르는 작업을
        # 띄우지 않는다(R-BG).
        ap.error("범위를 지정해라: --roster / --workspace / --session / --all")
    args.func(args)


if __name__ == "__main__":
    main_argv()
