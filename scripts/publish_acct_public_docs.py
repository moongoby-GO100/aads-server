"""ACCT(회계프로그램) 산출물 HTML 을 무인증 공개 포털(/education/)에 게시한다.

2026-09-15 대표님 지시: "여기서 작업한 html 문서들 승인권한 없이 외부에서
볼 수 있게 줘".

게시 위치는 이미 있는 공개 포털을 쓴다 — `app/static/reports/` 는 nginx
`/education/` 이 로그인 없이 그대로 서빙한다(`aads.conf`: "Keep this before
the dashboard catch-all so CEO review links do not require login"). **새 공개
경로를 만들지 않는다.** 새로 뚫으면 인증 경계가 하나 더 생기고, 그건 나중에
아무도 관리하지 않는다.

두 가지를 덧붙인다.

1. `<meta name="robots" content="noindex,nofollow">` 를 각 공개본에 넣는다.
   robots.txt 는 일반 검색엔진에 `Allow: /` 다. 이 문서들에는 서버 IP 와
   "전역 비밀번호 1개, RBAC 없음" 같은 **약점 서술**이 들어 있다. 링크를
   아는 사람이 보는 것과 검색에 잡히는 것은 다른 이야기다. 원본은 건드리지
   않고 공개본에만 넣는다.
2. 게시 전 키 패턴을 다시 검사한다. 통과하지 못하면 그 파일은 게시하지
   않는다 — 사람이 한 번 봤다는 것으로는 부족하다.

재실행해도 같은 결과가 나온다(멱등).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

PUBLIC_DIR = Path("/root/aads/aads-server/app/static/reports")

# (원본 경로, 공개본 파일명, 화면에 보일 제목, 한 줄 설명)
SOURCES: list[tuple[str, str, str, str]] = [
    (
        "/tmp/acct_pub/ACCT_flowmap_20260915.html",
        "ACCT_flowmap_20260915.html",
        "진아실장 업무 흐름 도식화",
        "매출 수집 → 전표 → 신고까지 실제 업무 흐름과 담당 도구를 그래프로 정리",
    ),
    (
        "/tmp/acct_pub/ACCT_forms_inventory_20260915.html",
        "ACCT_forms_inventory_20260915.html",
        "사용 양식 · 참조파일 인벤토리",
        "위하고·홈택스·급여 등 실제로 쓰는 양식과 참조파일 목록",
    ),
    (
        "/tmp/acct_pub/ACCT_PRD_HermesLangGraph_20260909.html",
        "ACCT_PRD_HermesLangGraph_20260909.html",
        "헤르메스 게이트웨이 기반 PRD",
        "LangGraph/LangSmith 기반 회계 자동화 설계·기술스택·PRD",
    ),
    (
        "/tmp/acct_pub/ACCT_next_steps_20260902.html",
        "ACCT_next_steps_20260902.html",
        "진아실장 합류 후 다음 단계",
        "합류 조치 결과와 공용 회계프로그램 전환 로드맵",
    ),
]

# 이미 공개돼 있던 ACCT 문서. 색인 페이지에는 같이 걸어 준다.
ALREADY_PUBLIC: list[tuple[str, str, str]] = [
    (
        "ACCT_HERMES_FOUNDATION_PRD_MOCKUP_20260909.html",
        "헤르메스 파운데이션 PRD 목업",
        "재사용 가능한 수준으로 구현한 작동형 목업",
    ),
]

INDEX_NAME = "ACCT_index_20260915.html"

ROBOTS_META = '<meta name="robots" content="noindex,nofollow">'

SECRET_RE = re.compile(
    r"sk-ant-[A-Za-z0-9_-]{6,}"
    r"|sk-proj-[A-Za-z0-9_-]{6,}"
    r"|sk-[A-Za-z0-9]{32,}"
    r"|AIza[A-Za-z0-9_-]{10,}"
    r"|ghp_[A-Za-z0-9]{10,}"
    r"|xoxb-[A-Za-z0-9-]{10,}"
    r"|BEGIN (?:RSA|OPENSSH|EC|DSA) PRIVATE KEY"
)

HEAD_RE = re.compile(r"<head[^>]*>", re.IGNORECASE)


def _with_robots(html: str) -> str:
    if 'name="robots"' in html or "name='robots'" in html:
        return html
    m = HEAD_RE.search(html)
    if not m:
        return ROBOTS_META + "\n" + html
    return html[: m.end()] + "\n" + ROBOTS_META + html[m.end():]


def _build_index(published: list[tuple[str, str, str]]) -> str:
    rows = []
    for name, title, desc in published:
        rows.append(
            f'''    <a class="doc" href="/education/{name}">
      <div class="t">{title}</div>
      <div class="d">{desc}</div>
      <div class="f">{name}</div>
    </a>'''
        )
    body = "\n".join(rows)
    return f"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
{ROBOTS_META}
<title>회계프로그램(ACCT) 공개 문서</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'Malgun Gothic',sans-serif;
background:#0f172a;color:#e2e8f0;padding:32px 20px;line-height:1.6}}
.wrap{{max-width:860px;margin:0 auto}}
h1{{font-size:22px;margin-bottom:6px}}
.sub{{color:#94a3b8;font-size:13px;margin-bottom:24px}}
.doc{{display:block;background:#1e293b;border:1px solid #334155;border-radius:10px;
padding:16px 18px;margin-bottom:12px;text-decoration:none;color:inherit}}
.doc:hover{{background:#334155}}
.t{{font-size:15.5px;font-weight:700;color:#e2e8f0}}
.d{{font-size:13px;color:#94a3b8;margin-top:4px}}
.f{{font-size:11px;color:#64748b;margin-top:6px;font-family:ui-monospace,monospace;word-break:break-all}}
.note{{margin-top:24px;padding:14px 16px;border:1px solid #f59e0b40;background:#f59e0b12;
border-radius:10px;font-size:12.5px;color:#fcd34d}}
</style>
</head>
<body>
<div class="wrap">
  <h1>회계프로그램(ACCT) 공개 문서</h1>
  <div class="sub">로그인 없이 열립니다. 링크를 아는 사람은 누구나 볼 수 있습니다.</div>
{body}
  <div class="note">
    이 페이지와 각 문서는 검색엔진 색인을 막아 뒀습니다(noindex). 다만 <b>인증이 없으므로
    링크가 유출되면 그대로 공개됩니다.</b> 서버 주소와 내부 구조 서술이 포함돼 있으니
    전달 범위를 관리해 주십시오.
  </div>
</div>
</body>
</html>
"""


def main() -> int:
    if not PUBLIC_DIR.is_dir():
        print(f"ABORT: 공개 디렉터리 없음 {PUBLIC_DIR}")
        return 1

    published: list[tuple[str, str, str]] = []
    for src, name, title, desc in SOURCES:
        src_path = Path(src)
        if not src_path.is_file():
            print(f"SKIP  {name}: 원본 없음 ({src})")
            continue
        html = src_path.read_text(encoding="utf-8", errors="replace")
        hit = SECRET_RE.search(html)
        if hit:
            print(f"BLOCK {name}: 키 패턴 감지 → 게시하지 않음 ({hit.group(0)[:12]}…)")
            continue
        (PUBLIC_DIR / name).write_text(_with_robots(html), encoding="utf-8")
        print(f"PUB   {name}  ({len(html):,} bytes)")
        published.append((name, title, desc))

    for name, title, desc in ALREADY_PUBLIC:
        if (PUBLIC_DIR / name).is_file():
            published.append((name, title, desc))
            print(f"LINK  {name} (기존 공개본)")

    if not published:
        print("ABORT: 게시된 문서가 없다")
        return 1

    (PUBLIC_DIR / INDEX_NAME).write_text(_build_index(published), encoding="utf-8")
    print(f"PUB   {INDEX_NAME} (색인 {len(published)}건)")
    print("\n공개 주소:")
    print(f"  https://aads.newtalk.kr/education/{INDEX_NAME}")
    for name, _, _ in published:
        print(f"  https://aads.newtalk.kr/education/{name}")
    return 0


sys.exit(main())
