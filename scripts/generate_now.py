#!/usr/bin/env python3
"""'지금 상태' 한 장 생성 — docs/generated/NOW.md

2026-09-14. 아키텍처 문서가 손으로 그린 ASCII 였고 2026-06-02 기준이었다.
`page.tsx (4501L)` 이라고 적혀 있는데 실제로는 12,865줄이었고, 엔드포인트는
"30+" 라고 돼 있는데 77개였다. 문서 최종 수정 이후 채팅 코드 커밋만
서버 175 / 대시보드 160건이 쌓여 있었다.

**손으로 적은 숫자는 반드시 틀어진다.** 그래서 이 문서에는 손으로 적는
칸이 없다. 전부 도는 것에서 뽑는다.

대상 독자는 비전문가다. 전문용어 옆에 한 줄 설명을 붙이고, 숫자만 나열하지
않는다. "지금 뭐가 돌고 있나 / 오늘 뭐가 바뀌었나" 두 질문에 답하는 것이
목적이다.

한 항목이 실패해도 나머지는 낸다. 전부 실패시키면 아무것도 못 본다.

    generate_now.py            docs/generated/NOW.md 갱신
    generate_now.py --stdout   화면에만 출력
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OUT = REPO / "docs" / "generated" / "NOW.md"
PG_CONTAINER = "aads-postgres"
PG_PASSWORD = "aads2026secure"


def run(argv: list[str], *, timeout: int = 30, stdin: str | None = None) -> str | None:
    """실패하면 None. 호출자가 '확인 불가'로 적는다."""
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, input=stdin)
        return p.stdout if p.returncode == 0 else None
    except Exception:
        return None


def psql(sql: str) -> str | None:
    env = dict(os.environ)
    host = env.get("PGHOST", "")
    if host:
        argv = ["psql", "-h", host, "-p", env.get("PGPORT", "5432"),
                "-U", env.get("PGUSER", "aads"), "-d", env.get("PGDATABASE", "aads")]
    else:
        argv = ["docker", "exec", "-i", "-e", f"PGPASSWORD={PG_PASSWORD}", PG_CONTAINER,
                "psql", "-U", "aads", "-d", "aads"]
    return run(argv + ["-At", "-F", "\x1f", "-f", "-"], timeout=60, stdin=sql)


def unknown(what: str) -> list[str]:
    return [f"> {what} — **확인 불가**. 이 항목만 실패했고 나머지는 실측값이다.", ""]


# ── 절별 생성기 ──────────────────────────────────────────────────────────

def section_services() -> list[str]:
    out = run(["docker", "ps", "--format", "{{.Names}}\t{{.Image}}\t{{.Status}}"])
    if out is None:
        return unknown("돌고 있는 서비스")
    rows = [l.split("\t") for l in out.strip().split("\n") if l.strip()]
    lines = [
        "## 지금 돌고 있는 것", "",
        f"컨테이너 **{len(rows)}개**가 떠 있다. 컨테이너는 프로그램 하나를 담아 두는 상자다.", "",
        "| 이름 | 무엇인가 | 상태 |", "|---|---|---|",
    ]
    what = {
        "aads-server": "본체 API — 채팅과 모든 기능이 여기서 돈다",
        "aads-server-green": "본체 API 예비 슬롯 — 배포할 때 번갈아 쓴다",
        "aads-dashboard": "화면(웹페이지)",
        "aads-dashboard-green": "화면 예비 슬롯",
        "aads-postgres": "데이터베이스 — 대화·기억·설정이 전부 여기 있다",
        "aads-redis": "임시 저장소 — 스트리밍 중간값",
        "aads-nginx": "출입구 — 외부 요청을 안쪽으로 넘긴다",
        "aads-litellm": "여러 AI 공급자를 한 규격으로 묶는 중계",
        "aads-searxng": "검색 엔진",
        "aads-socket-proxy": "도커 제어 권한을 제한해서 넘기는 중계",
    }
    for r in sorted(rows):
        name = r[0]
        desc = what.get(name, "—")
        status = r[2] if len(r) > 2 else "?"
        mark = "정상" if "healthy" in status or "Up" in status else "확인 필요"
        lines.append(f"| `{name}` | {desc} | {mark} ({status}) |")
    lines.append("")
    return lines


def section_public_paths() -> list[str]:
    conf = Path("/etc/nginx/conf.d/aads.conf")
    if not conf.is_file():
        return unknown("공개 경로")
    try:
        text = conf.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return unknown("공개 경로")
    locs = []
    for line in text.split("\n"):
        s = line.strip()
        if s.startswith("location ") and s.endswith("{"):
            locs.append(s[len("location "):-1].strip())
    active = None
    port_file = REPO / ".active_port"
    if port_file.is_file():
        try:
            active = port_file.read_text().strip()
        except OSError:
            active = None
    slot = {"8100": "blue", "8102": "green"}.get(active or "", "?")
    return [
        "## 바깥에서 들어오는 길", "",
        f"nginx 가 외부 요청을 받아 안쪽으로 넘긴다. 공개 경로 **{len(locs)}개**.",
        f"지금 요청을 받는 API 슬롯은 **{slot}** (포트 {active or '?'}) 이다.", "",
    ]


def section_api_size() -> list[str]:
    routers = sorted((REPO / "app" / "routers").glob("*.py")) if (REPO / "app" / "routers").is_dir() else []
    total = 0
    rows = []
    for f in routers:
        try:
            n = sum(1 for l in f.read_text(encoding="utf-8", errors="ignore").split("\n")
                    if l.startswith("@router."))
        except OSError:
            continue
        if n:
            total += n
            rows.append((n, f.name))
    if not rows:
        return unknown("API 규모")
    rows.sort(reverse=True)
    lines = [
        "## API 규모", "",
        f"바깥에서 부를 수 있는 창구가 **{total}개**다. 창구 하나가 기능 하나라고 보면 된다.", "",
        "| 파일 | 창구 수 |", "|---|---:|",
    ]
    for n, name in rows[:8]:
        lines.append(f"| `app/routers/{name}` | {n} |")
    lines.append("")
    return lines


def section_code_size() -> list[str]:
    targets = [
        ("app/services/chat_service.py", "채팅 핵심 로직"),
        ("app/routers/chat.py", "채팅 창구"),
        ("app/services/context_builder.py", "AI 에게 보낼 맥락 조립"),
        ("../aads-dashboard/src/app/chat/page.tsx", "채팅 화면 (한 파일)"),
    ]
    rows = []
    for rel, desc in targets:
        p = (REPO / rel).resolve()
        if not p.is_file():
            continue
        try:
            n = sum(1 for _ in p.open(encoding="utf-8", errors="ignore"))
        except OSError:
            continue
        rows.append((rel.replace("../", ""), desc, n))
    if not rows:
        return unknown("코드 규모")
    lines = ["## 코드 규모", "",
             "문서에 손으로 적어 두면 반드시 틀어지는 숫자다. 그래서 여기서 뽑는다.", "",
             "| 파일 | 무엇인가 | 줄 수 |", "|---|---|---:|"]
    for rel, desc, n in rows:
        lines.append(f"| `{rel}` | {desc} | {n:,} |")
    lines.append("")
    return lines


def section_data() -> list[str]:
    out = psql("""
        SELECT c.relname, pg_size_pretty(pg_total_relation_size(c.oid)),
               COALESCE(s.n_live_tup, 0)
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        LEFT JOIN pg_stat_user_tables s ON s.relid = c.oid
        WHERE n.nspname = 'public' AND c.relkind = 'r'
        ORDER BY pg_total_relation_size(c.oid) DESC LIMIT 8;
    """)
    if out is None:
        return unknown("데이터 현황")
    lines = ["## 쌓여 있는 데이터", "",
             "가장 큰 표 8개다. 표 하나가 자료 한 종류다.", "",
             "| 표 | 크기 | 행 수 |", "|---|---:|---:|"]
    for line in out.strip().split("\n"):
        f = line.split("\x1f")
        if len(f) >= 3:
            lines.append(f"| `{f[0]}` | {f[1]} | {int(f[2]):,} |")
    lines.append("")
    return lines


def section_deploys() -> list[str]:
    out = psql("""
        SELECT id, left(release_sha, 12), status, phase,
               to_char(created_at, 'MM-DD HH24:MI')
        FROM deploy_runs ORDER BY id DESC LIMIT 5;
    """)
    if out is None:
        return unknown("최근 배포")
    lines = ["## 최근 배포", "",
             "배포는 고친 코드를 실제 서비스에 올리는 일이다.", "",
             "| 번호 | 버전 | 결과 | 단계 | 시각 |", "|---:|---|---|---|---|"]
    for line in out.strip().split("\n"):
        f = line.split("\x1f")
        if len(f) >= 5:
            mark = {"success": "성공", "failed": "실패", "running": "진행 중",
                    "blocked": "막힘", "superseded": "대체됨"}.get(f[2], f[2])
            lines.append(f"| {f[0]} | `{f[1]}` | {mark} | {f[3]} | {f[4]} |")
    lines.append("")
    return lines


def section_today() -> list[str]:
    repos = [("aads-server", REPO), ("aads-dashboard", REPO.parent / "aads-dashboard")]
    lines = ["## 오늘 바뀐 것", ""]
    any_row = False
    for name, path in repos:
        if not (path / ".git").exists():
            continue
        out = run(["git", "-C", str(path), "log", "--since=midnight",
                   "--format=%h %s", "--no-merges"], timeout=20)
        if out is None:
            lines += [f"**{name}** — 확인 불가", ""]
            continue
        rows = [l for l in out.strip().split("\n") if l.strip()]
        any_row = any_row or bool(rows)
        lines.append(f"**{name}** — 변경 {len(rows)}건")
        lines.append("")
        for r in rows[:12]:
            sha, _, subject = r.partition(" ")
            lines.append(f"- `{sha}` {subject}")
        if len(rows) > 12:
            lines.append(f"- … 외 {len(rows) - 12}건")
        lines.append("")
    if not any_row:
        lines += ["오늘 코드 변경 없음.", ""]
    return lines


def section_docs() -> list[str]:
    out = psql("""
        SELECT count(*), count(embedding), count(DISTINCT doc_path), count(DISTINCT server)
        FROM doc_chunks;
    """)
    if out is None:
        return []
    f = out.strip().split("\x1f")
    if len(f) < 4:
        return []
    chunks, embedded, docs, servers = (int(x or 0) for x in f[:4])
    pct = round(100.0 * embedded / chunks, 1) if chunks else 0.0
    return [
        "## 문서 검색", "",
        f"문서 **{docs:,}건**을 **{chunks:,}조각**으로 나눠 두었다. 서버 {servers}대에서 모았다.",
        f"그중 **{pct}%** 가 검색 가능한 상태다(조각을 숫자로 바꿔 두면 뜻이 비슷한 것을 찾을 수 있다).",
        "",
        "채팅에 물으면 이 문서들을 근거로 답한다.", "",
    ]


def build() -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M KST")
    head = [
        "# 지금 상태", "",
        f"_{now} 자동 생성 — **손으로 고치지 마라.** 고쳐도 다음 실행에 덮어쓴다._",
        "",
        "`scripts/generate_now.py` 가 실제로 도는 것에서 뽑는다. 이 문서에 적힌",
        "숫자는 전부 생성 시점의 실측값이다.",
        "",
        "---", "",
    ]
    body: list[str] = []
    for fn in (section_services, section_public_paths, section_api_size,
               section_code_size, section_data, section_docs,
               section_deploys, section_today):
        try:
            body += fn()
        except Exception as exc:  # 한 절이 죽어도 나머지는 낸다
            body += unknown(f"{fn.__name__} ({type(exc).__name__})")
    return "\n".join(head + body).rstrip() + "\n"


def main() -> None:
    ap = argparse.ArgumentParser(description="지금 상태 한 장 생성")
    ap.add_argument("--stdout", action="store_true", help="파일로 쓰지 않고 화면에만")
    args = ap.parse_args()
    text = build()
    if args.stdout:
        sys.stdout.write(text)
        return
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(text, encoding="utf-8")
    print(f"[generate_now] {OUT} ({len(text):,}자)")


if __name__ == "__main__":
    main()
