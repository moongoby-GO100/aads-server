#!/usr/bin/env python3
"""문서 색인 — 흩어진 문서를 채팅이 찾을 수 있게 만든다.

2026-09-14 실측. `/root/aads` 아래 `.md` 가 13,219개인데 **고유 내용은
2,604개** 였다. 80%가 사본이다. 릴리스 스냅숏·워크트리·클론이 `docs/` 트리를
통째로 들고 다녀서 같은 문서가 33벌까지 생겼고, 한글·영문 두 이름으로 갈린
것도 있었다.

    /root/aads/aads-server/reports/20260615_e_contract_templates_draft.md
    /root/aads/aads-docs/reports/20260615_전자계약서_3종_템플릿_초안.md
    ... 31곳 더

그런데 정작 **채팅은 이 문서들을 못 본다.** Auto-RAG 는 memory_facts 와 채팅
기록만 검색한다. 대표가 "이거 왜 이렇게 돼 있지"라고 물어도 문서가 근거로
잡히지 않는다. 문서를 읽기 좋게 고치는 것보다 **물어볼 수 있게 만드는 것이**
먼저다.

이 도구가 문서를 잘라 `doc_chunks` 에 넣는다. 임베딩은 넣지 않는다 —
임베딩 경로(LLM 키·라우트)는 contabo116 의 aads-server 에만 있고, 원격
서버에 키를 복사하는 것은 R-KEY 위반이다. 텍스트만 올리고, 임베딩은
contabo116 의 백필이 채운다.

    index_docs.py scan     무엇이 색인될지만 본다 (쓰기 없음)
    index_docs.py index    수집·분할·업서트
    index_docs.py embed    임베딩 채우기 (Ollama 있는 서버에서만)
    index_docs.py status   서버별 현황과 임베딩 커버리지

**서버별 사본을 만들지 마라.** contabo116 은 컨테이너 경유, 원격 서버는
PGHOST 터널로 같은 DB 를 본다 — error_book.py 와 같은 규약이다. 2026-09-14
kiwoom_key_manager 가 사본이 갈라져서 생긴 사고였다.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import socket
import subprocess
import time
import sys
from pathlib import Path

PG_CONTAINER = "aads-postgres"
PG_PASSWORD = "aads2026secure"

# 색인 대상 뿌리. 서버마다 있는 것만 쓴다 — 없는 경로는 조용히 건너뛴다.
ROOTS = [
    # contabo116 (AADS)
    ("/root/aads/aads-server/docs", "AADS", "서버 문서"),
    ("/root/aads/aads-server/reports", "AADS", "서버 리포트"),
    ("/root/aads/aads-docs/docs", "AADS", "공용 문서"),
    ("/root/aads/aads-docs/reports", "AADS", "공용 리포트"),
    ("/root/aads/aads-dashboard/docs", "AADS", "대시보드 문서"),
    ("/root/aads/aads-dashboard/reports", "AADS", "대시보드 리포트"),
    ("/root/aads/aads-core/docs", "AADS", "코어 문서"),
    ("/root/aads/aads-core/reports", "AADS", "코어 리포트"),
    ("/root/project-docs", "AADS", "프로젝트 문서"),
    ("/root/aads/go100/docs", "GO100", "GO100 문서"),
    ("/root/aads/go100/backend/docs", "GO100", "GO100 백엔드 문서"),
    # 컨테이너 안에서 돌 때의 같은 경로
    ("/app/docs", "AADS", "서버 문서"),
    ("/app/reports", "AADS", "서버 리포트"),
    # contabo14 (KIS/GO100)
    ("/root/kis-autotrade-v4/docs", "KIS", "KIS 문서"),
    ("/root/kis-autotrade-v4/docs/go100", "GO100", "GO100 문서"),
    ("/root/kis-autotrade-v4/docs/technical", "GO100", "기술문서"),
    ("/root/kis-autotrade-v4/docs/plans", "GO100", "기획문서"),
    ("/root/kis-autotrade-v4/docs/operations", "GO100", "운영문서"),
    ("/root/kis-autotrade-v4/report", "GO100", "리포트"),
]

# 경로에 이 조각이 들어가면 제외한다. 전부 "같은 문서의 다른 사본"이 생기는
# 곳이다. 파일을 지우는 것이 아니라 색인에서만 뺀다 — 지우는 것은 되돌릴 수
# 없고, 지금 문제는 용량이 아니라 색인이다.
EXCLUDE = (
    "/node_modules/", "/.git/", "/.venvs/", "/venv/", "/.next/",
    "/.worktrees/", "-releases/", "-unified-p0/", "/claude-model-release-",
    "/aads-dashboard-unni/", "/backups/", "/backup/", "/.tmp-",
    "/site-packages/", "/dist/", "/build/",
)
# go100 클론들(go100-token-opt, go100-direct-yBauuU ...). 정본은 /root/aads/go100 하나다.
_GO100_CLONE = re.compile(r"/go100[-_][A-Za-z0-9._-]+/")

EXTENSIONS = {".md"}
MIN_BYTES = 200
MAX_BYTES = 4 * 1024 * 1024

CHUNK_CHARS = int(os.getenv("DOC_CHUNK_CHARS", "1400"))
CHUNK_OVERLAP = int(os.getenv("DOC_CHUNK_OVERLAP", "200"))
# 문서당 청크 상한. HANDOVER.md 는 1.67MB 다 — 상한이 없으면 이 한 건이
# 1,200청크를 만들어 검색 결과를 독점한다.
MAX_CHUNKS_PER_DOC = int(os.getenv("DOC_MAX_CHUNKS", "60"))


def server_name() -> str:
    return os.getenv("AADS_SERVER_NAME") or socket.gethostname()


def _psql_argv() -> tuple[list[str], dict]:
    """접속 방식을 고른다 — error_book.py 와 같은 규약.

    PGHOST 가 있으면 그쪽(원격 서버의 SSH 터널), 없으면 로컬 컨테이너.
    """
    env = dict(os.environ)
    host = env.get("PGHOST", "")
    if host:
        return ([
            "psql", "-h", host,
            "-p", env.get("PGPORT", "5432"),
            "-U", env.get("PGUSER", "aads"),
            "-d", env.get("PGDATABASE", "aads"),
        ], env)
    env["PGPASSWORD"] = PG_PASSWORD
    return ([
        "docker", "exec", "-i", "-e", f"PGPASSWORD={PG_PASSWORD}", PG_CONTAINER,
        "psql", "-U", "aads", "-d", "aads",
    ], env)


def psql(sql: str, *, quiet: bool = False) -> str:
    argv, env = _psql_argv()
    proc = subprocess.run(
        argv + ["-At", "-F", "\x1f", "-f", "-"],
        input=sql, text=True, capture_output=True, timeout=300, env=env,
    )
    if proc.returncode != 0:
        if not quiet:
            print(f"[index_docs] DB 오류: {proc.stderr.strip()[:400]}", file=sys.stderr)
        raise SystemExit(2)
    return proc.stdout


def lit(v) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, (int, float)):
        return str(v)
    return "'" + str(v).replace("\\", "\\\\").replace("'", "''") + "'"


def excluded(path: str) -> bool:
    return any(f in path for f in EXCLUDE) or bool(_GO100_CLONE.search(path))


def extract_title(text: str, fallback: str) -> str:
    for line in text.split("\n")[:40]:
        s = line.strip()
        if s.startswith("# "):
            return s[2:].strip()[:300]
    return fallback[:300]


def collect() -> list[dict]:
    """정본 문서만 모은다 — 내용 해시로 중복을 제거한다.

    파일명이 달라도 내용이 같으면 한 벌이다. 정본은 경로가 짧은 쪽을 고른다
    (사본은 대개 더 깊은 곳에 있다).
    """
    by_hash: dict[str, dict] = {}
    scanned = 0
    seen_roots: set[str] = set()
    for root, project, label in ROOTS:
        base = Path(root)
        if not base.is_dir() or root in seen_roots:
            continue
        seen_roots.add(root)
        for p in base.rglob("*"):
            try:
                if not p.is_file() or p.suffix.lower() not in EXTENSIONS:
                    continue
                full = str(p)
                if excluded(full):
                    continue
                st = p.stat()
                if not (MIN_BYTES <= st.st_size <= MAX_BYTES):
                    continue
                raw = p.read_bytes()
            except OSError:
                continue
            scanned += 1
            digest = hashlib.sha256(raw).hexdigest()
            prev = by_hash.get(digest)
            if prev is not None and len(prev["path"]) <= len(full):
                continue
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                continue
            by_hash[digest] = {
                "path": full, "project": project, "label": label,
                "sha256": digest, "size": st.st_size, "mtime": st.st_mtime,
                "title": extract_title(text, p.stem), "text": text,
            }
    docs = sorted(by_hash.values(), key=lambda d: d["path"])
    print(f"[index_docs] 훑음 {scanned:,}개 → 정본 {len(docs):,}개 "
          f"(사본 {scanned - len(docs):,}개 제외), {sum(d['size'] for d in docs)/1048576:.1f}MB")
    return docs


def chunk(text: str) -> list[tuple[str, str]]:
    """(제목맥락, 본문) 목록으로 자른다.

    제목 경계를 우선한다. 검색 결과가 "어느 절의 이야기인지" 없이 나오면
    비전문가가 읽을 수 없다.
    """
    out: list[tuple[str, str]] = []
    heading = ""
    buf: list[str] = []
    size = 0

    def flush():
        nonlocal buf, size
        body = "\n".join(buf).strip()
        if len(body) >= 80:
            out.append((heading, body))
        # 겹침: 끝부분을 다음 청크 앞에 남긴다. 문장이 경계에서 잘려
        # 검색이 못 잡는 것을 막는다.
        tail = body[-CHUNK_OVERLAP:] if len(body) > CHUNK_OVERLAP else ""
        buf = [tail] if tail else []
        size = len(tail)

    for line in text.split("\n"):
        s = line.strip()
        if s.startswith("#") and size > CHUNK_CHARS // 2:
            flush()
        if s.startswith("#"):
            heading = s.lstrip("#").strip()[:200]
        buf.append(line)
        size += len(line) + 1
        if size >= CHUNK_CHARS:
            flush()
        if len(out) >= MAX_CHUNKS_PER_DOC:
            return out
    flush()
    return out[:MAX_CHUNKS_PER_DOC]


def cmd_scan(_args) -> None:
    docs = collect()
    total = 0
    for d in docs:
        total += len(chunk(d["text"]))
    print(f"[index_docs] 예상 청크 {total:,}개")
    by_label: dict[str, int] = {}
    for d in docs:
        by_label[d["label"]] = by_label.get(d["label"], 0) + 1
    for k, v in sorted(by_label.items(), key=lambda x: -x[1]):
        print(f"   {k:<20} {v:>5}개")


def cmd_index(args) -> None:
    srv = server_name()
    docs = collect()
    if args.limit:
        docs = docs[: args.limit]

    known = {}
    for line in psql(
        f"SELECT doc_path, doc_sha256 FROM doc_chunks WHERE server={lit(srv)} "
        f"GROUP BY doc_path, doc_sha256"
    ).splitlines():
        if "\x1f" in line:
            path, sha = line.split("\x1f", 1)
            known[path] = sha

    changed = [d for d in docs if known.get(d["path"]) != d["sha256"]]
    print(f"[index_docs] 변경/신규 {len(changed):,}개 (그대로 {len(docs)-len(changed):,}개 건너뜀)")
    if not changed:
        return

    live = {d["path"] for d in docs}
    stale = [p for p in known if p not in live]
    if stale:
        batch = ",".join(lit(p) for p in stale)
        psql(f"DELETE FROM doc_chunks WHERE server={lit(srv)} AND doc_path IN ({batch});")
        print(f"[index_docs] 사라진 문서 {len(stale)}개 색인 제거")

    inserted = 0
    for i in range(0, len(changed), 20):
        stmts = []
        for d in changed[i:i + 20]:
            stmts.append(
                f"DELETE FROM doc_chunks WHERE server={lit(srv)} AND doc_path={lit(d['path'])};"
            )
            rows = []
            for idx, (heading, body) in enumerate(chunk(d["text"])):
                rows.append(
                    f"({lit(srv)},{lit(d['path'])},{lit(d['sha256'])},{lit(d['project'])},"
                    f"{lit(d['label'])},{lit(d['title'])},{lit(heading)},{idx},{lit(body)},"
                    f"to_timestamp({d['mtime']:.0f}))"
                )
            if rows:
                stmts.append(
                    "INSERT INTO doc_chunks (server,doc_path,doc_sha256,project,label,"
                    "title,heading,chunk_index,content,mtime) VALUES "
                    + ",".join(rows) + ";"
                )
                inserted += len(rows)
        psql("BEGIN;" + "".join(stmts) + "COMMIT;")
        print(f"[index_docs] {min(i+20, len(changed)):,}/{len(changed):,} 문서", flush=True)
    print(f"[index_docs] 청크 {inserted:,}개 저장. 임베딩은 contabo116 백필이 채운다.")


OLLAMA_URL = os.getenv("LOCAL_OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
EMBED_MODEL = os.getenv("DOC_EMBED_MODEL", "nomic-embed-text")
EMBED_DIM = 768
# CPU Ollama 실측(2026-09-14, 8코어·GPU 없음): 1,400자 한 건에 약 6초.
# 한 요청에 여러 건을 넣어도 내부에서 직렬 처리되므로 배치를 키워도 안 빨라지고
# 타임아웃만 커진다. 동시 요청 3개로 시간당 약 688건.
EMBED_BATCH = int(os.getenv("DOC_EMBED_BATCH", "4"))
EMBED_PARALLEL = int(os.getenv("DOC_EMBED_PARALLEL", "3"))
EMBED_TIMEOUT = int(os.getenv("DOC_EMBED_TIMEOUT", "300"))
EMBED_TEXT_CHARS = int(os.getenv("DOC_EMBED_TEXT_CHARS", "1600"))

# nomic-embed-text 는 **작업 접두어를 요구한다.** 문서에는
# `search_document: `, 질문에는 `search_query: ` 를 붙여야 한다. 붙이지
# 않아도 벡터는 나오지만 검색 품질이 무너진다.
#
# 2026-09-14 실측. 질문 "채팅 응답이 왜 느려졌지" 에 대해:
#   접두어 없음   정답 0.8158 / 무관한 계약서 0.8012  → 차이 0.015
#   접두어 있음   정답 0.7830 / 무관한 계약서 0.7225  → 차이 0.061
# 구분 폭이 4배다. 조각 7,847개 중에서 순위를 매기려면 이 폭이 필요하다.
#
# **검색하는 쪽(app/services/doc_index.QUERY_PREFIX)과 짝이 맞아야 한다.**
# 한쪽만 바꾸면 검색이 조용히 나빠진다. 회귀 테스트가 둘을 대조한다.
DOC_PREFIX = "search_document: "


def ollama_embed(texts: list[str]) -> list[list[float]] | None:
    """진짜 임베딩만 돌려준다. 실패하면 None — 더미를 만들지 않는다.

    2026-09-14 — 앱 쪽 임베딩 서비스는 경로가 없으면 조용히 해시 기반 더미를
    돌려줬고, 그게 진짜인 것처럼 저장돼 검색이 무의미해졌다. 여기서는
    실패를 실패로 둔다.
    """
    import urllib.error
    import urllib.request

    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/embed",
        data=json.dumps({"model": EMBED_MODEL, "input": texts}).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=EMBED_TIMEOUT) as r:
            body = json.loads(r.read())
    except Exception as exc:
        print(f"[index_docs] 임베딩 실패 ({type(exc).__name__}): {str(exc)[:120]}", file=sys.stderr)
        return None
    vectors = body.get("embeddings") or []
    if len(vectors) != len(texts):
        print(f"[index_docs] 임베딩 개수 불일치 {len(vectors)}/{len(texts)}", file=sys.stderr)
        return None
    for v in vectors:
        if len(v) != EMBED_DIM:
            print(f"[index_docs] 차원 불일치 {len(v)} != {EMBED_DIM}", file=sys.stderr)
            return None
    return vectors


def cmd_embed(args) -> None:
    """임베딩이 빈 청크를 채운다. 중단해도 안전하다 — 채운 것만 남는다."""
    import concurrent.futures as cf

    srv = server_name()
    budget = args.limit or 10**9
    done = 0
    t0 = time.time()
    while done < budget:
        rows = []
        take = min(EMBED_BATCH * EMBED_PARALLEL, budget - done)
        for line in psql(
            f"SELECT id, title, heading, content FROM doc_chunks "
            f"WHERE embedding IS NULL AND server={lit(srv)} "
            f"ORDER BY indexed_at ASC LIMIT {int(take)};"
        ).splitlines():
            parts = line.split("\x1f")
            if len(parts) >= 4:
                rows.append(parts)
        if not rows:
            break

        groups = [rows[i:i + EMBED_BATCH] for i in range(0, len(rows), EMBED_BATCH)]

        def run(group):
            # 제목·절 제목을 본문 앞에 붙인다. 본문만 넣으면 "어느 문서의 어느
            # 절인지"가 벡터에 안 들어가서 비슷한 문장이 여러 문서에 있을 때
            # 엉뚱한 쪽이 잡힌다.
            texts = [
                DOC_PREFIX + f"{g[1]}\n{g[2]}\n{g[3]}"[:EMBED_TEXT_CHARS]
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
                        f"UPDATE doc_chunks SET embedding={lit(body)}::vector "
                        f"WHERE id={lit(g[0])};"
                    )
                if stmts:
                    psql("BEGIN;" + "".join(stmts) + "COMMIT;")
                    wrote += len(stmts)
        if wrote == 0:
            print("[index_docs] 이번 회차에 하나도 못 채웠다 — 중단", file=sys.stderr)
            break
        done += wrote
        remain = psql(
            f"SELECT count(*) FROM doc_chunks WHERE embedding IS NULL AND server={lit(srv)};"
        ).strip()
        rate = done / max(1e-6, time.time() - t0)
        eta = int(int(remain or 0) / rate / 60) if rate > 0 else -1
        print(f"[index_docs] {done:,}개 완료, 남음 {remain}, "
              f"{rate*3600:.0f}건/시간, 예상 {eta}분", flush=True)
    print(f"[index_docs] 임베딩 {done:,}개 / {time.time()-t0:.0f}초")


def cmd_status(_args) -> None:
    out = psql(
        "SELECT server, project, count(*), count(embedding), "
        "count(DISTINCT doc_path), max(indexed_at)::date "
        "FROM doc_chunks GROUP BY server, project ORDER BY 3 DESC;"
    )
    if not out.strip():
        print("[index_docs] 색인 없음")
        return
    print(f"{'서버':<14}{'프로젝트':<10}{'청크':>9}{'임베딩':>9}{'문서':>8}  최종")
    for line in out.splitlines():
        f = line.split("\x1f")
        if len(f) >= 6:
            print(f"{f[0]:<14}{f[1]:<10}{int(f[2]):>9,}{int(f[3]):>9,}{int(f[4]):>8,}  {f[5]}")


def main() -> None:
    ap = argparse.ArgumentParser(description="문서 색인 — 모든 서버 공용")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("scan").set_defaults(fn=cmd_scan)
    p = sub.add_parser("index")
    p.add_argument("--limit", type=int, default=0, help="문서 수 제한 (시험용)")
    p.set_defaults(fn=cmd_index)
    e = sub.add_parser("embed")
    e.add_argument("--limit", type=int, default=0, help="개수 제한 (시험용)")
    e.set_defaults(fn=cmd_embed)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
