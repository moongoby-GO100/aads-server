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
    index_docs.py index    수집·분할·업서트 (뒤이어 정본 문서도 색인)
    index_docs.py index-canonical [--dry-run]  정본 문서(label='정본')만 색인
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

    # 원격 서버에서 당겨온 문서 — scripts/sync_remote_docs.sh 가 채운다.
    # 서버마다 색인기 사본을 두지 않는다. 문서를 한 곳으로 모으고 색인은
    # contabo116 에서만 돈다(2026-09-19).
    #
    # 좁은 경로를 먼저 둔다. collect() 는 같은 파일을 **먼저 만난 root** 의
    # 프로젝트로 확정하므로, docs 를 먼저 두면 그 아래가 전부 KIS 가 된다.
    ("/root/aads/_remote_docs/contabo14/kis-autotrade-v4/docs/go100", "GO100", "GO100 문서(contabo14)"),
    ("/root/aads/_remote_docs/contabo14/kis-autotrade-v4/docs/technical", "GO100", "GO100 기술문서(contabo14)"),
    ("/root/aads/_remote_docs/contabo14/kis-autotrade-v4/docs/plans", "GO100", "GO100 기획문서(contabo14)"),
    ("/root/aads/_remote_docs/contabo14/kis-autotrade-v4/docs/operations", "GO100", "GO100 운영문서(contabo14)"),
    # kis-autotrade-v4 는 이름과 달리 GO100 의 워크디렉터리다. docs/ 879개 중
    # 파일명이 GO100 로 시작하는 것이 213개, KIS 로 시작하는 것은 2개뿐이고
    # 상위 문서(ARCHITECTURE.md 등)도 전부 GO100 내용이다. 그래서 docs/ 전체를
    # GO100 으로 본다. 실제 KIS 문서는 kis-api-portal/ 아래에만 있다.
    ("/root/aads/_remote_docs/contabo14/kis-autotrade-v4/docs/kis-api-portal", "KIS", "KIS 문서(contabo14)"),
    ("/root/aads/_remote_docs/contabo14/kis-autotrade-v4/docs", "GO100", "GO100 문서(contabo14)"),
    ("/root/aads/_remote_docs/contabo14/kis-autotrade-v4/report", "GO100", "GO100 리포트(contabo14)"),
    ("/root/aads/_remote_docs/contabo14/kis-autotrade-v4/reports", "GO100", "GO100 리포트(contabo14)"),
    ("/root/aads/_remote_docs/cafe24_114/shortflow/docs", "SF", "SF 문서(cafe24_114)"),
    ("/root/aads/_remote_docs/cafe24_114/shortflow/reports", "SF", "SF 리포트(cafe24_114)"),
    ("/root/aads/_remote_docs/cafe24_114/newtalk-v2/docs", "NTV2", "NTV2 문서(cafe24_114)"),
    ("/root/aads/_remote_docs/cafe24_114/newtalk-v2/reports", "NTV2", "NTV2 리포트(cafe24_114)"),
    ("/root/aads/_remote_docs/cafe24_114/nas-image", "NAS", "NAS 문서(cafe24_114)"),

    # 발행 리포트(HTML). 대표님께 보고한 문서를 채팅이 근거로 쓰려면 색인돼야 한다.
    ("/root/aads/aads-server/app/static/reports", "AADS", "발행 리포트"),

    # 디렉터리뿐 아니라 **파일 경로 하나**도 넣을 수 있다. 릴리스 정본(AGENTS.md)과
    # 규칙(CLAUDE.md)은 저장소 루트에 있어 docs/ 스캔에 걸리지 않는데, 사고를 가장
    # 많이 막는 문서가 바로 이 둘이다 — 2026-09-13 헛빌드는 낡은 규칙을 읽어 났다.
    ("/root/aads/AGENTS.md", "AADS", "릴리스 계약(전역)"),
    ("/root/aads/aads-server/AGENTS.md", "AADS", "API 릴리스 계약"),
    ("/root/aads/aads-dashboard/AGENTS.md", "AADS", "대시보드 릴리스 계약"),
    ("/root/aads/aads-server/CLAUDE.md", "AADS", "프로젝트 규칙"),
]

# AADS 서버 문서·리포트의 읽기 원본.
#
# 2026-10-02 실측. 위 ROOTS 의 /root/aads/aads-server/{docs,reports} 는 개발
# 작업트리라 `main...origin/main [ahead 4, behind 48]` 로 뒤처져 있었고,
# origin/main 에만 있는 신규 문서 5건의 doc_chunks 가 0건이었다. 그 트리는
# 사용자 작업 커밋을 들고 있어 fetch/reset 을 할 수 없다. 그래서 색인은
# origin/main 전용 미러(scripts/refresh_docs_mirror.sh 가 갱신)에서 읽는다.
#
# **doc_chunks.doc_path 는 바꾸지 않는다.** 읽는 위치만 미러로 옮기고 저장하는
# 경로는 ROOTS 의 원래 경로(/root/aads/aads-server/...)로 되돌려 쓴다. 경로가
# 바뀌면 cmd_index 의 known/sha 비교가 어긋나 전 문서가 새로 INSERT 되고 옛 행은
# "사라진 문서" 로 지워진다.
LIVE_TREE = "/root/aads/aads-server"
MIRROR_SUBDIRS = ("docs", "reports")
DEFAULT_MIRROR_DIR = "/root/aads/mirrors/aads-server"


def mirror_dir() -> str:
    return os.getenv("AADS_DOCS_MIRROR") or DEFAULT_MIRROR_DIR


def mirror_ready(mirror: str | None = None) -> bool:
    """미러가 쓸 수 있는 상태인가 — git 저장소이고 docs/ 가 체크아웃돼 있다."""
    m = Path(mirror or mirror_dir())
    return (m / ".git").exists() and (m / "docs").is_dir()


def resolve_root(root: str, mirror: str | None = None, *, use_mirror: bool | None = None) -> str:
    """ROOTS 의 논리 경로를 실제로 읽을 경로로 바꾼다. 미러 대상이 아니면 그대로."""
    m = (mirror or mirror_dir()).rstrip("/")
    if use_mirror is None:
        use_mirror = mirror_ready(m)
    if not use_mirror:
        return root
    for sub in MIRROR_SUBDIRS:
        base = f"{LIVE_TREE}/{sub}"
        if root == base or root.startswith(base + "/"):
            return m + root[len(LIVE_TREE):]
    return root


def to_logical_path(physical: str, logical_root: str, physical_root: str) -> str:
    """읽은 경로를 doc_chunks 에 저장할 원래(논리) 경로로 되돌린다."""
    if physical_root == logical_root:
        return physical
    return logical_root + physical[len(physical_root):]


# 경로에 이 조각이 들어가면 제외한다. 전부 "같은 문서의 다른 사본"이 생기는
# 곳이다. 파일을 지우는 것이 아니라 색인에서만 뺀다 — 지우는 것은 되돌릴 수
# 없고, 지금 문제는 용량이 아니라 색인이다.
EXCLUDE = (
    "/node_modules/", "/.git/", "/.venvs/", "/venv/", "/.next/",
    "/.worktrees/", "-releases/", "-unified-p0/", "/claude-model-release-",
    "/aads-dashboard-unni/", "/backups/", "/backup/", "/.tmp-",
    "/site-packages/", "/dist/", "/build/",
)
# go100 클론 디렉터리(go100- 또는 go100_ 접두 이름). 정본은 /root/aads/go100 하나다.
_GO100_CLONE = re.compile(r"/go100[-_][A-Za-z0-9._-]+/")

EXTENSIONS = {".md", ".html"}

# 발행 리포트는 HTML 이다. 태그를 걷어내고 본문만 넣는다 — 안 걷어내면 마크업이
# 청크의 절반을 차지해 검색이 엉뚱한 곳을 잡는다.
# 중첩 반복을 쓰지 않는다(R-BG 3). 아래 둘 다 단일 반복이다.
_HTML_DROP = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.S | re.I)
_HTML_TAG = re.compile(r"<[^>]+>")


def html_to_text(raw: str) -> str:
    t = _HTML_DROP.sub(" ", raw)
    t = _HTML_TAG.sub("\n", t)
    for ent, ch in (("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"),
                    ("&gt;", ">"), ("&quot;", '"'), ("&#39;", "'")):
        t = t.replace(ent, ch)
    return "\n".join(ln for ln in (x.strip() for x in t.split("\n")) if ln)


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
    use_mirror = mirror_ready()
    if not use_mirror and any(Path(f"{LIVE_TREE}/{sub}").is_dir() for sub in MIRROR_SUBDIRS):
        print(f"[index_docs] 경고: 미러 {mirror_dir()} 없음 — 개발 작업트리({LIVE_TREE})로 "
              f"폴백한다. origin/main 의 신규 문서가 빠질 수 있다.", file=sys.stderr)
    for root, project, label in ROOTS:
        if root in seen_roots:
            continue
        seen_roots.add(root)
        phys_root = resolve_root(root, use_mirror=use_mirror)
        base = Path(phys_root)
        # ROOTS 항목은 디렉터리일 수도, 파일 하나일 수도 있다.
        if base.is_dir():
            entries = base.rglob("*")
        elif base.is_file():
            entries = [base]
        else:
            continue
        for p in entries:
            try:
                if not p.is_file() or p.suffix.lower() not in EXTENSIONS:
                    continue
                full = to_logical_path(str(p), root, phys_root)
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
            if p.suffix.lower() == ".html":
                text = html_to_text(text)
                if len(text) < MIN_BYTES:
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


def record_run(srv: str, docs: int, changed: int, chunks: int, secs: float) -> None:
    """색인이 '돌았다'는 사실 자체를 남긴다.

    doc_chunks.indexed_at 은 컬럼 기본값 now() 로 INSERT 시점에만 찍힌다.
    cmd_index 는 sha256 이 바뀐 문서만 INSERT 하므로, 문서가 하루 동안
    하나도 안 바뀌면 색인이 정상 동작해도 MAX(indexed_at) 이 전진하지 않는다.
    그러면 "색인이 멈췄다" 와 "문서가 안 바뀌었다" 를 구분할 수 없다.

    2026-09-19 실측 — GO100 파티션이 09-14 이후 5일째 그대로였는데 원인은
    색인 정지가 아니라 문서 무변경이었다. 최신 시각만 보고 정지로 오진했다.

    테이블은 여기서 만든다. 이 스크립트는 모든 서버 공용이고 마이그레이션이
    돌지 않는 서버에서도 실행되므로, 별도 migration 에 두면 그쪽에서 INSERT 가
    실패해 색인 자체가 죽는다.
    """
    psql(
        "CREATE TABLE IF NOT EXISTS doc_index_runs ("
        " id bigserial PRIMARY KEY,"
        " server text NOT NULL,"
        " ran_at timestamptz NOT NULL DEFAULT now(),"
        " docs integer NOT NULL,"
        " changed integer NOT NULL,"
        " chunks integer NOT NULL,"
        " seconds numeric(10,2) NOT NULL);"
        "CREATE INDEX IF NOT EXISTS doc_index_runs_ran_at_idx"
        " ON doc_index_runs (ran_at DESC);"
        "INSERT INTO doc_index_runs (server,docs,changed,chunks,seconds)"
        f" VALUES ({lit(srv)},{int(docs)},{int(changed)},{int(chunks)},{secs:.2f});"
    )


# ── 정본 문서(project_document_heads / revisions) 색인 ─────────────────────
#
# 정본은 API 로 등록돼 파일시스템에 없으므로 위 ROOTS 루프에 걸리지 않는다. 그래서
# 문서 내용 검색에서 빠져 있었다(2026-10-02 실측, revisions 15건 중 검색 0건).
#
# 롤백은 `DELETE FROM doc_chunks WHERE label='정본'` 한 줄이어야 한다 — label 을
# 이 값 하나로 고정하고 다른 용도로 쓰지 않는다.
CANONICAL_LABEL = "정본"
CANONICAL_PREFIX = "canonical://"

# app/api/canonical_documents.py 의 SECRET 과 **같은 패턴**이다. 이 스크립트는 모든
# 서버 공용이라 app 패키지를 import 할 수 없어 사본을 둔다. 두 정의가 갈라지지
# 않게 tests/unit/test_doc_index_canonical.py 가 패턴 문자열을 대조한다.
SECRET = re.compile(
    r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|"
    r"\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|"
    r"xox[baprs]-[A-Za-z0-9-]{16,}|AIza[0-9A-Za-z_-]{20,}|AKIA[0-9A-Z]{16})|"
    r"(?i:(?:api[_-]?key|auth[_-]?token|access[_-]?token|refresh[_-]?token|password|client[_-]?secret|secret[_-]?key|private[_-]?key|database[_-]?url)['\"]?\s*[:=]\s*['\"]?[^\s'\"]+)"
)


def canonical_doc_path(project_key: str, document_key: str, revision_id: str) -> str:
    """실제 파일 경로와 겹치지 않는 가상 키."""
    return f"{CANONICAL_PREFIX}{project_key}/{document_key}@{revision_id}"


def is_canonical_path(path: str) -> bool:
    return path.startswith(CANONICAL_PREFIX)


def pick_canonical_revisions(heads: list[dict]) -> list[tuple[str, dict]]:
    """head 마다 색인할 revision 을 고른다. (상태, revision) 목록을 돌려준다.

    - 보관(archived) head 는 통째로 제외한다.
    - 승인본(approved_revision_id)이 있으면 "승인" 으로 넣는다.
    - 최신 revision 이 승인본과 다르면(승인본이 없거나 승인본보다 새로우면) "초안" 으로 추가한다.
    """
    picked: list[tuple[str, dict]] = []
    for h in heads:
        if h.get("archived"):
            continue
        by_id = {str(r["id"]): r for r in (h.get("revisions") or [])}
        approved_id = h.get("approved_revision_id")
        latest_id = h.get("latest_revision_id")
        approved = by_id.get(str(approved_id)) if approved_id else None
        latest = by_id.get(str(latest_id)) if latest_id else None
        if approved is not None:
            picked.append(("승인", {**approved, "project_key": h["project_key"],
                                    "document_key": h["document_key"]}))
        if latest is not None and (approved is None or str(latest["id"]) != str(approved["id"])):
            picked.append(("초안", {**latest, "project_key": h["project_key"],
                                    "document_key": h["document_key"]}))
    return picked


def build_canonical_docs(heads: list[dict]) -> tuple[list[dict], int]:
    """collect() 가 만드는 dict 와 같은 모양으로 바꾼다. (문서, SECRET 으로 거른 수)."""
    docs: list[dict] = []
    skipped = 0
    for status, r in pick_canonical_revisions(heads):
        text = r.get("content") or ""
        title = r.get("title") or r["document_key"]
        if not text.strip() or SECRET.search(text) or SECRET.search(title):
            skipped += 1
            continue
        docs.append({
            "path": canonical_doc_path(r["project_key"], r["document_key"], str(r["id"])),
            "project": r["project_key"],
            "label": CANONICAL_LABEL,
            "sha256": r["content_hash"],
            "size": len(text.encode("utf-8")),
            "mtime": float(r.get("mtime") or 0),
            "title": f"[{status} v{r['version']}] {title}"[:300],
            "text": text,
        })
    return docs, skipped


def canonical_chunks(text: str) -> list[tuple[str, str]]:
    """chunk() 를 재사용하되, 짧은 정본도 검색되도록 최소 한 조각은 남긴다."""
    return chunk(text) or [("", text.strip())]


def stale_paths(known: dict, live: set[str], *, canonical: bool) -> list[str]:
    """지울 doc_path. 파일 단계는 canonical:// 을, 정본 단계는 파일 경로를 건드리지 않는다."""
    return [p for p in known if is_canonical_path(p) == canonical and p not in live]


# 정본 도메인은 **내부 테넌트 것만** 읽는다. doc_chunks 에는 tenant 칸이 없고
# /project-docs/search 는 테넌트 권한을 확인하지 않는다 — 고객 테넌트 정본을 넣으면
# project 값이 같을 때 다른 테넌트에 그대로 노출된다.
CANONICAL_HEADS_SQL = (
    "SELECT json_build_object("
    "'project_key', h.project_key, 'document_key', h.document_key,"
    "'approved_revision_id', h.approved_revision_id,"
    "'latest_revision_id', h.latest_revision_id,"
    "'archived', (h.approved_revision_id IS NULL AND COALESCE((SELECT e.action"
    " FROM project_document_events e WHERE e.head_id=h.id"
    " AND e.action IN ('approved','archived') ORDER BY e.id DESC LIMIT 1),'')='archived'),"
    "'revisions', (SELECT json_agg(json_build_object("
    "'id', r.id, 'version', r.version, 'title', r.title, 'content', r.content,"
    "'content_hash', r.content_hash, 'mtime', extract(epoch from r.created_at)))"
    " FROM project_document_revisions r"
    " WHERE r.head_id=h.id AND r.id IN (h.approved_revision_id, h.latest_revision_id)))::text"
    " FROM project_document_heads h"
    " WHERE h.tenant_id = public.aads_internal_tenant_id()"
    " ORDER BY h.project_key, h.document_key;"
)


def fetch_canonical_heads() -> list[dict] | None:
    """정본 head 를 읽는다. 테이블이 없는 DB 면 None."""
    if not psql("SELECT to_regclass('project_document_heads') IS NOT NULL;").strip().startswith("t"):
        return None
    # psql -At 은 줄 단위 출력이라 본문 개행이 섞이면 깨진다 — json 은 개행을 이스케이프한다.
    return [json.loads(line) for line in psql(CANONICAL_HEADS_SQL).splitlines() if line.strip()]


def index_canonical(srv: str, *, dry_run: bool = False) -> dict:
    """정본을 doc_chunks 에 맞춘다. 파일 청크는 읽지도 지우지도 않는다."""
    heads = fetch_canonical_heads()
    if heads is None:
        print("[index_docs] 정본 테이블 없음 — 정본 색인 건너뜀")
        return {"docs": 0, "changed": 0, "chunks": 0, "removed": 0, "skipped_secret": 0}
    docs, skipped = build_canonical_docs(heads)
    if skipped:
        print(f"[index_docs] 정본 {skipped}건은 SECRET 패턴이 있어 색인에서 뺐다")

    # title 도 비교한다. 초안이 승인되면 revision(=doc_path)과 content_hash 는 그대로고
    # `[초안 v1]` → `[승인 v1]` 접두만 바뀐다 — sha 만 보면 표시가 낡은 채 남는다.
    known: dict[str, tuple[str, str]] = {}
    for line in psql(
        f"SELECT doc_path, doc_sha256, title FROM doc_chunks WHERE server={lit(srv)} "
        f"AND label={lit(CANONICAL_LABEL)} AND doc_path LIKE {lit(CANONICAL_PREFIX + '%')} "
        f"GROUP BY doc_path, doc_sha256, title"
    ).splitlines():
        parts = line.split("\x1f", 2)
        if len(parts) == 3:
            known[parts[0]] = (parts[1], parts[2])

    live = {d["path"] for d in docs}
    changed = [d for d in docs if known.get(d["path"]) != (d["sha256"], d["title"])]
    stale = stale_paths(known, live, canonical=True)
    n_chunks = sum(len(canonical_chunks(d["text"])) for d in changed)
    print(f"[index_docs] 정본 {len(docs)}건 중 변경/신규 {len(changed)}건, 제거 {len(stale)}건"
          + (" (dry-run: 쓰지 않음)" if dry_run else ""))
    if dry_run or not (changed or stale):
        return {"docs": len(docs), "changed": len(changed), "chunks": n_chunks,
                "removed": len(stale), "skipped_secret": skipped}

    stmts = []
    if stale:
        batch = ",".join(lit(p) for p in stale)
        stmts.append(f"DELETE FROM doc_chunks WHERE server={lit(srv)} AND doc_path IN ({batch});")
    for d in changed:
        stmts.append(f"DELETE FROM doc_chunks WHERE server={lit(srv)} AND doc_path={lit(d['path'])};")
        rows = [
            f"({lit(srv)},{lit(d['path'])},{lit(d['sha256'])},{lit(d['project'])},"
            f"{lit(d['label'])},{lit(d['title'])},{lit(heading)},{idx},{lit(body)},"
            f"to_timestamp({d['mtime']:.0f}))"
            for idx, (heading, body) in enumerate(canonical_chunks(d["text"]))
        ]
        stmts.append(
            "INSERT INTO doc_chunks (server,doc_path,doc_sha256,project,label,"
            "title,heading,chunk_index,content,mtime) VALUES " + ",".join(rows) + ";"
        )
    psql("BEGIN;" + "".join(stmts) + "COMMIT;")
    print(f"[index_docs] 정본 청크 {n_chunks:,}개 저장. 임베딩은 embed 가 채운다.")
    return {"docs": len(docs), "changed": len(changed), "chunks": n_chunks,
            "removed": len(stale), "skipped_secret": skipped}


def run_canonical_stage(srv: str) -> None:
    """파일 색인 뒤에 돈다. 정본 단계가 실패해도 파일 색인 결과는 이미 반영됐다 —
    실패를 삼키지 않고 종료코드로 드러낸다."""
    try:
        index_canonical(srv)
    except SystemExit:
        print("[index_docs] 정본 색인 실패 — 파일 색인은 반영됨", file=sys.stderr)
        raise


def cmd_index_canonical(args) -> None:
    index_canonical(server_name(), dry_run=args.dry_run)


def cmd_index(args) -> None:
    srv = server_name()
    t0 = time.time()
    docs = collect()
    if args.limit:
        docs = docs[: args.limit]

    known = {}
    for line in psql(
        f"SELECT doc_path, doc_sha256 FROM doc_chunks WHERE server={lit(srv)} "
        f"AND doc_path NOT LIKE {lit(CANONICAL_PREFIX + '%')} "
        f"GROUP BY doc_path, doc_sha256"
    ).splitlines():
        if "\x1f" in line:
            path, sha = line.split("\x1f", 1)
            known[path] = sha

    changed = [d for d in docs if known.get(d["path"]) != d["sha256"]]
    print(f"[index_docs] 변경/신규 {len(changed):,}개 (그대로 {len(docs)-len(changed):,}개 건너뜀)")
    if not changed:
        record_run(srv, len(docs), 0, 0, time.time() - t0)
        run_canonical_stage(srv)
        return

    live = {d["path"] for d in docs}
    stale = stale_paths(known, live, canonical=False)
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
    # 변경이 있었던 런도 기록한다. 2026-10-01 실측 — record_run() 이 위
    # `if not changed:` 조기반환 안에만 있어서, 문서가 바뀐 런 11회
    # (09-29 06:10 ~ 10-01 06:10 CEST)가 doc_index_runs 에 한 줄도 남지 않았다.
    # 그래서 원장은 "무변경 런" 만 모은 표가 되고, 색인 정지와 구분이 안 됐다.
    record_run(srv, len(docs), len(changed), inserted, time.time() - t0)
    run_canonical_stage(srv)


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
    ic = sub.add_parser("index-canonical", help="정본 문서만 색인 (index 가 파일 색인 뒤 자동 호출)")
    ic.add_argument("--dry-run", action="store_true", help="DB 에 쓰지 않고 계획만 출력")
    ic.set_defaults(fn=cmd_index_canonical)
    e = sub.add_parser("embed")
    e.add_argument("--limit", type=int, default=0, help="개수 제한 (시험용)")
    e.set_defaults(fn=cmd_embed)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
