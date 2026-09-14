#!/usr/bin/env python3
"""프론트 진단기 — 로딩 속도·데이터 속도·런타임 오류를 한 번에 잰다.

설계: docs/plans/AADS-FRONTEND-DIAGNOSTICS-PRD.md

프론트가 고장나면 CEO 가 말해줘야 안다. 2026-09-13 채팅 렌더링이 깨졌을 때
서버 헬스체크·배포 상태·시각 QA 가 전부 초록이었다(원인은 React 최소화 오류
#31). 셋 중 어느 것도 브라우저에서 무슨 일이 벌어지는지 보지 않기 때문이다.

속도도 같다. "느리다"는 체감은 있는데 서버가 느린지, 번들이 큰지, API 가
느린지, API 를 순서대로 부르는지 구분할 자료가 없었다. 각 API 가 300ms 여도
열 개를 직렬로 부르면 3초이고 개별 로그만 보면 전부 "빠름"이다.

측정만으로는 개선되지 않으므로 세 가지를 같이 한다.
  - 직전 실행과 비교해 회귀를 지목한다
  - 절대 예산을 둔다 (조금씩 나빠지는 것은 회귀로 안 잡힌다)
  - "느리다"가 아니라 "어느 API 가 몇 ms" 로 원인을 지목한다

사용:
    frontend_diagnostics.py --quick            핵심 라우트
    frontend_diagnostics.py --full             전체 라우트
    frontend_diagnostics.py --url /ops         지정 경로
    frontend_diagnostics.py --quick --no-db    DB 적재 없이 출력만
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

# 대상 사이트는 코드가 아니라 설정에서 읽는다. 서버마다 스크립트 사본을 두면
# 사본이 갈라진다 — 2026-09-14 kiwoom_key_manager 가 그 사례였다(사본 3개 전부
# 옛 판). 코드는 한 벌만 두고 대상만 늘린다.
SITES_FILE = Path(os.getenv(
    "AADS_FRONTEND_SITES",
    "/root/aads/aads-server/config/frontend_diagnostics_sites.json",
))
PG_CONTAINER = os.getenv("AADS_PG_CONTAINER", "aads-postgres")
PG_PASSWORD = os.getenv("AADS_PG_PASSWORD", "aads2026secure")

# 아래 셋은 선택한 사이트에 따라 main() 에서 채워진다.
BASE_URL = os.getenv("AADS_FRONTEND_BASE_URL", "https://aads.newtalk.kr")
STORAGE_STATE = Path("")
REFRESHER = Path("")


def load_sites() -> dict:
    """사이트 정의를 읽는다. 파일이 없으면 AADS 기본값으로 돈다."""
    try:
        raw = json.loads(SITES_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"aads": {
            "label": "AADS",
            "base_url": "https://aads.newtalk.kr",
            "core_routes": ["/", "/chat", "/ops", "/tasks", "/project-status"],
            "major_routes": [],
            "auth": {
                "storage_state": "/root/aads/aads-server/browser-bridge-state/qa-storage-state.json",
                "refresh": "/root/aads/aads-server/scripts/refresh_qa_storage_state.py",
            },
        }}
    return {k: v for k, v in raw.items() if not k.startswith("_") and isinstance(v, dict)}


def capture_load() -> dict:
    """측정 시점의 서버 상태. 부하를 같이 남기지 않으면 회귀로 오독한다.

    2026-09-14 배포 세 번 직후에 잰 값에서 LCP 가 +124% 로 찍혔는데, 같은 순간
    아무 일도 하지 않는 /api/v1/health 가 2,011ms 였다. 프론트가 나빠진 게 아니라
    서버가 밀리고 있었다. 부하 정보가 없으면 이 둘을 구분할 수 없다.
    """
    info: dict[str, Any] = {}
    try:
        one, five, fifteen = os.getloadavg()
        info["load1"], info["load5"], info["load15"] = round(one, 2), round(five, 2), round(fifteen, 2)
        info["cpus"] = os.cpu_count() or 0
    except OSError:
        pass
    try:
        out = subprocess.run(["pgrep", "-af", "deploy.sh"], capture_output=True, text=True, timeout=10).stdout
        lines = [ln for ln in out.splitlines() if "deploy.sh" in ln and "pgrep" not in ln]
        info["deploy_running"] = bool(lines)
    except Exception:
        info["deploy_running"] = False
    return info


def load_is_noisy(info: dict) -> str:
    """측정을 믿기 어려운 상태면 사유를 돌려준다."""
    if info.get("deploy_running"):
        return "측정 중 배포가 진행 중이었다"
    cpus = info.get("cpus") or 0
    load1 = info.get("load1")
    if cpus and load1 is not None and load1 > cpus * 0.9:
        return f"부하가 높다 (load1={load1}, cpu={cpus})"
    return ""

# /chat 은 세션이 있어야 실제 렌더 경로를 탄다. 목록 화면만 보면 2026-09-13 의
# React #31 처럼 특정 세션에서만 터지는 결함을 놓친다.
CHAT_SESSION = os.getenv("AADS_FRONTEND_CHAT_SESSION", "")

CORE_ROUTES: list[str] = []
MAJOR_ROUTES: list[str] = []

# 예산. PRD §7. 실측으로 보정하되 올리지는 않는다 —
# 올려야 할 이유가 생기면 그건 별도 의사결정이다.
BUDGET = {
    "ttfb_ms":        (600, 1500),
    "fcp_ms":         (1800, 3000),
    "lcp_ms":         (2500, 4000),
    "data_wait_ms":   (1000, 3000),
    "api_slowest_ms": (800, 2500),
    # 압축 해제 후 크기. 대역폭이 아니라 브라우저의 파싱·메모리 비용을 본다.
    "payload_bytes":  (2 * 1024 * 1024, 5 * 1024 * 1024),
}
# 런타임 오류는 경고 단계를 두지 않는다. 예외 하나면 그 화면은 이미 깨진 것이다.
API_RESPONSE_WARN = 512 * 1024
API_RESPONSE_FAIL = 2 * 1024 * 1024
REGRESSION_PCT = 0.30
REGRESSION_MIN_MS = 500

# React 최소화 오류는 번호만 나온다. 사람이 읽을 문장으로 바꾼다 — 오늘 #31 을
# 해독하는 데 시간을 썼다.
REACT_ERRORS = {
    "31": "객체를 React 자식으로 렌더하려 했다 (배열·문자열로 바꿔야 한다)",
    "130": "정의되지 않은 컴포넌트를 렌더했다 (import 오류일 가능성)",
    "185": "무한 렌더 루프 (setState 가 렌더 중에 호출된다)",
    "418": "hydration 불일치 — 서버 HTML 과 클라이언트 렌더 결과가 다르다",
    "419": "서버 렌더 중단",
    "421": "hydration 중 suspend",
    "423": "hydration 오류로 클라이언트 렌더로 전환됨",
    "425": "hydration 텍스트 불일치",
}

# 에러 바운더리가 떴을 때 화면에 남는 문구.
ERROR_BOUNDARY_MARKERS = (
    "오류가 발생했습니다", "문제가 발생했습니다", "Something went wrong",
    "Minified React error", "Application error", "렌더링 오류",
)
LOGIN_MARKERS = ("/login", "/signin")

# 브라우저에 먼저 심는다. LCP 는 관측을 페이지 로드 전에 붙여야 잡힌다.
INIT_SCRIPT = """
window.__lcp = 0;
try {
  new PerformanceObserver((list) => {
    for (const e of list.getEntries()) {
      if (e.startTime > window.__lcp) window.__lcp = e.startTime;
    }
  }).observe({ type: 'largest-contentful-paint', buffered: true });
} catch (e) {}
"""

# 수집은 한 번의 evaluate 로 끝낸다. 왕복이 싼 레인이지만 계측 사이에 페이지가
# 더 움직이면 값이 어긋난다.
COLLECT_SCRIPT = """
() => {
  const nav = performance.getEntriesByType('navigation')[0] || {};
  const paints = {};
  for (const p of performance.getEntriesByType('paint')) paints[p.name] = p.startTime;
  // transferSize(실제 전송) 와 decodedBodySize(압축 해제 후) 는 다르다.
  // zstd/brotli 로 나가면 transferSize 가 0 으로 오고 encoded 도 decoded 와
  // 같아져서, 셋을 합쳐 쓰면 압축 전 크기를 "전송량"으로 오해한다
  // (2026-09-13: /dashboard/directives 를 1.55MB 전송이라고 적었는데 실제
  //  전송은 126KB 였다). 따로 담고 판정에서 구분해 쓴다.
  const res = performance.getEntriesByType('resource').map(r => ({
    name: r.name,
    kind: r.initiatorType,
    start: r.startTime,
    end: r.responseEnd,
    dur: r.duration,
    wire: r.transferSize || 0,
    payload: r.decodedBodySize || r.encodedBodySize || 0,
  }));
  const bodyText = (document.body && document.body.innerText || '').slice(0, 4000);
  // 조작 대상인데 보이지 않는 요소 — 눌러도 반응이 없다.
  const hidden = [];
  const clickable = document.querySelectorAll('button, a[href], [role="button"], input, select');
  for (const el of clickable) {
    const rect = el.getBoundingClientRect();
    const st = getComputedStyle(el);
    // display:none 은 거르지 않는다. 반응형 메뉴(햄버거·닫기 버튼)가 폭에 따라
    // 꺼지는 정상 패턴이고, 이걸 잡으면 매 페이지가 오탐으로 덮인다.
    // 문제는 "자리는 차지하는데 눌러도 반응이 없는" 쪽이다.
    if (st.display === 'none') continue;
    if (rect.width === 0 || rect.height === 0 ||
        st.visibility === 'hidden' || st.opacity === '0') {
      hidden.push((el.tagName + (el.id ? '#' + el.id : '') + ':' +
                   (el.innerText || el.getAttribute('aria-label') || '').trim().slice(0, 30)));
    }
  }
  return {
    ttfb: nav.responseStart || 0,
    dcl: nav.domContentLoadedEventEnd || 0,
    load: nav.loadEventEnd || 0,
    docWire: nav.transferSize || 0,
    docPayload: nav.decodedBodySize || nav.encodedBodySize || 0,
    fcp: paints['first-contentful-paint'] || 0,
    lcp: window.__lcp || 0,
    resources: res,
    bodyText: bodyText,
    clickableTotal: clickable.length,
    hiddenClickable: hidden.slice(0, 20),
    scrollW: document.documentElement.scrollWidth,
    clientW: document.documentElement.clientWidth,
  };
}
"""


def psql(sql: str) -> str:
    proc = subprocess.run(
        ["docker", "exec", "-i", "-e", f"PGPASSWORD={PG_PASSWORD}", PG_CONTAINER,
         "psql", "-U", "aads", "-d", "aads", "-At", "-f", "-"],
        input=sql, text=True, capture_output=True, timeout=120,
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or "")[:400])
    return (proc.stdout or "").strip()


def lit(value: Any) -> str:
    if value is None:
        return "NULL"
    return "'" + str(value).replace("'", "''") + "'"


def num(value: Any) -> str:
    if value is None:
        return "NULL"
    try:
        return str(int(round(float(value))))
    except (TypeError, ValueError):
        return "NULL"


# 쿼리에 실려 올 수 있는 민감 값. 지우되 나머지는 남긴다.
_SENSITIVE_PARAMS = ("token", "access_token", "key", "secret", "password", "auth")


def normalize_url(url: str) -> str:
    """집계용 경로. 쿼리를 버린다."""
    return str(url or "").split("?", 1)[0].split("#", 1)[0]


def dedupe_key(url: str) -> str:
    """중복 판정용 키. 경로만 보면 다른 요청을 같은 것으로 센다.

    2026-09-14: /chat/messages 를 3회 호출한 것으로 보고했으나 응답이 각각
    393,848B · 4,350B · 907,598B 였다. limit·cursor·fields 가 다른 **서로 다른
    요청**인데 쿼리를 지워 같은 URL 로 셌다. 있지도 않은 중복을 잡으라고
    보고하게 된다.

    민감 파라미터만 지우고 나머지는 키에 남긴다.
    """
    base, _, query = str(url or "").split("#", 1)[0].partition("?")
    if not query:
        return base
    kept = []
    for part in query.split("&"):
        name = part.split("=", 1)[0].lower()
        if any(s in name for s in _SENSITIVE_PARAMS):
            continue
        kept.append(part)
    kept.sort()  # 파라미터 순서가 달라도 같은 요청이다
    return base + ("?" + "&".join(kept) if kept else "")


def longest_serial_chain(calls: list[dict]) -> int:
    """겹치지 않고 이어지는 호출들의 최대 합.

    API 를 병렬로 부르면 전체 소요는 가장 느린 하나에 가깝다. 직렬로 부르면
    합이 된다. 이 값이 크다는 건 **병렬화로 줄일 수 있는 시간**이 그만큼
    있다는 뜻이다. 각 호출이 빨라도 여기서 몇 초가 나올 수 있다.
    """
    if not calls:
        return 0
    items = sorted(((c["start"], c["end"], c["end"] - c["start"]) for c in calls),
                   key=lambda x: x[1])
    best: list[float] = []
    for i, (s_i, e_i, d_i) in enumerate(items):
        cur = d_i
        for j in range(i):
            s_j, e_j, _ = items[j]
            if e_j <= s_i:  # j 가 끝난 뒤에 i 가 시작 — 이어붙일 수 있다
                cur = max(cur, best[j] + d_i)
        best.append(cur)
    return int(round(max(best)))


def decode_react_error(text: str) -> str | None:
    marker = "Minified React error #"
    if marker not in text:
        return None
    tail = text.split(marker, 1)[1]
    code = "".join(ch for ch in tail[:4] if ch.isdigit())
    if not code:
        return None
    return f"React #{code}: {REACT_ERRORS.get(code, '알려지지 않은 코드')}"


def lookup_error_book(texts: list[str]) -> list[dict]:
    """발견한 오류에 알려진 원인을 붙인다.

    "React #418 hydration 불일치" 까지는 진단기가 말해주지만, 그게 무엇 때문에
    생기는지는 매번 사람이 다시 추적했다. 한 번 원인을 밝힌 것은 사전에 넣고
    다음부터는 여기서 이어 붙인다.

    조회 실패는 무시한다 — 사전이 없다고 진단이 멈추면 안 된다.
    """
    body = "\n".join(t for t in texts if t)
    if not body.strip():
        return []
    book = Path("/root/aads/aads-server/scripts/error_book.py")
    if not book.is_file():
        return []
    try:
        proc = subprocess.run(
            [sys.executable, str(book), "match", "-", "--bump", "--record",
             "--source", "frontend_diagnostics"],
            input=body, text=True, capture_output=True, timeout=40,
        )
    except Exception:
        return []
    # match 는 "알려진 오류 없음" 일 때 종료코드 1 을 준다. 그때도 후보 기록
    # 메시지가 출력에 있으므로 종료코드로 버리면 새 후보가 조용히 사라진다.
    out = proc.stdout or ""
    if "알려진 오류:" not in out and "후보로 기록:" not in out:
        return []
    return [{"text": line.strip()} for line in out.splitlines() if line.strip()]


def ensure_storage_state(refresh: bool) -> bool:
    """인증 상태를 준비한다.

    인증 없이 열면 /·/chat·/ops 가 전부 307 이다. 로그인 화면을 진단 결과로
    저장하면 2026-09-13 의 시각 QA 와 똑같이 된다 — 로그인 UI 를 세 번 채점하고
    통과 판정을 냈다.
    """
    if refresh and str(REFRESHER) and REFRESHER.is_file():
        proc = subprocess.run([sys.executable, str(REFRESHER)],
                              capture_output=True, text=True, timeout=180)
        if proc.returncode != 0:
            print(f"  [경고] storage_state 갱신 실패 — 기존 파일을 쓴다: "
                  f"{(proc.stderr or '').strip()[:160]}")
    return bool(str(STORAGE_STATE)) and STORAGE_STATE.is_file()


def diagnose_page(ctx, route: str, timeout_ms: int) -> dict:
    """한 라우트를 진단한다. 계측을 먼저 붙이고 이동한다."""
    result: dict[str, Any] = {
        "route": route, "status": "ok",
        "console_errors": [], "page_errors": [], "failed_requests": [],
    }
    page = ctx.new_page()
    page.add_init_script(INIT_SCRIPT)

    def on_console(msg):
        if msg.type in ("error", "warning"):
            result["console_errors"].append({"type": msg.type, "text": str(msg.text)[:400]})

    def on_pageerror(err):
        result["page_errors"].append(str(err)[:600])

    def on_requestfailed(req):
        reason = str(req.failure)[:120] if req.failure else ""
        # 취소는 실패가 아니다. Next.js 가 링크를 프리페치했다가 페이지가
        # 넘어가면 전부 ERR_ABORTED 로 끝난다 — 첫 실행에서 20건 중 18건이
        # 이것이었고, 실제 결함(502 두 건)이 그 속에 묻혔다.
        if "ABORTED" in reason.upper():
            result["aborted_requests"] = result.get("aborted_requests", 0) + 1
            return
        result["failed_requests"].append({"url": normalize_url(req.url)[:300], "reason": reason})

    page.on("console", on_console)
    page.on("pageerror", on_pageerror)
    page.on("requestfailed", on_requestfailed)

    def on_response(resp):
        try:
            if resp.status >= 400:
                result["failed_requests"].append(
                    {"url": normalize_url(resp.url)[:300], "reason": f"HTTP {resp.status}"})
        except Exception:
            pass

    page.on("response", on_response)

    url = BASE_URL.rstrip("/") + route
    started = time.monotonic()
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        # networkidle 은 SSE·폴링이 있으면 영원히 안 온다. 상한을 두고 넘어간다.
        try:
            page.wait_for_load_state("networkidle", timeout=min(timeout_ms, 15000))
        except Exception:
            pass
        page.wait_for_timeout(1200)  # 지연 렌더·LCP 확정 여유
        raw = page.evaluate(COLLECT_SCRIPT)
    except Exception as exc:
        result["status"] = "timeout" if "Timeout" in str(exc) else "error"
        result["error"] = str(exc)[:300]
        page.close()
        return result

    landed = page.url
    body = str(raw.get("bodyText") or "")
    page.close()
    result["wall_ms"] = int((time.monotonic() - started) * 1000)
    result["landed_url"] = normalize_url(landed)

    # 로그인 화면이면 지표를 남기지 않는다. 남기면 "빠르고 오류 없는 페이지"로
    # 집계되어 기준선까지 오염시킨다.
    if any(m in landed for m in LOGIN_MARKERS):
        result["status"] = "auth_failed"
        return result

    # ── 페이지 로딩 ────────────────────────────────────────────────
    result["ttfb_ms"] = raw.get("ttfb") or 0
    result["dcl_ms"] = raw.get("dcl") or 0
    result["load_ms"] = raw.get("load") or 0
    result["fcp_ms"] = raw.get("fcp") or 0
    result["lcp_ms"] = raw.get("lcp") or 0

    resources = raw.get("resources") or []
    # payload_bytes — 압축 해제 후 크기. 대역폭이 아니라 **파싱·메모리·렌더 비용**이다.
    # wire_bytes — 실제 전송량. 압축 방식에 따라 0 으로 오므로 없을 수 있다.
    result["payload_bytes"] = int(raw.get("docPayload") or 0) + sum(
        int(r.get("payload") or 0) for r in resources)
    wire = int(raw.get("docWire") or 0) + sum(int(r.get("wire") or 0) for r in resources)
    result["wire_bytes"] = wire or None

    # ── 데이터 로딩 ────────────────────────────────────────────────
    api_calls = []
    for r in resources:
        name = str(r.get("name") or "")
        if "/api/" not in name:
            continue
        api_calls.append({
            "url": normalize_url(name),
            "key": dedupe_key(name),
            "start": float(r.get("start") or 0),
            "end": float(r.get("end") or 0),
            "dur": float(r.get("dur") or 0),
            "bytes": int(r.get("payload") or 0),
            "wire": int(r.get("wire") or 0),
        })
    api_calls.sort(key=lambda c: c["start"])
    result["api_calls"] = api_calls

    if api_calls:
        slowest = max(api_calls, key=lambda c: c["dur"])
        result["api_slowest_ms"] = slowest["dur"]
        result["api_slowest_url"] = slowest["url"]
        result["api_total_ms"] = sum(c["dur"] for c in api_calls)
        result["serial_chain_ms"] = longest_serial_chain(api_calls)
        # 데이터 대기 — 페이지는 떴는데 내용이 안 채워진 시간. 체감에 가장 가깝다.
        last_end = max(c["end"] for c in api_calls)
        result["data_wait_ms"] = max(0.0, last_end - (result["fcp_ms"] or 0))
        seen: dict[str, int] = {}
        for c in api_calls:
            seen[c["key"]] = seen.get(c["key"], 0) + 1
        result["duplicate_api"] = {u: n for u, n in seen.items() if n > 1}
    else:
        result["api_slowest_ms"] = 0
        result["api_total_ms"] = 0
        result["serial_chain_ms"] = 0
        result["data_wait_ms"] = 0
        result["duplicate_api"] = {}

    # ── 결함 ──────────────────────────────────────────────────────
    boundary = [m for m in ERROR_BOUNDARY_MARKERS if m in body]
    result["error_boundary"] = boundary
    decoded = []
    for text in result["page_errors"] + [c["text"] for c in result["console_errors"]] + [body]:
        hit = decode_react_error(str(text))
        if hit and hit not in decoded:
            decoded.append(hit)
    result["react_errors"] = decoded
    result["hidden_clickable"] = raw.get("hiddenClickable") or []
    result["clickable_total"] = raw.get("clickableTotal") or 0
    result["h_scroll"] = bool((raw.get("scrollW") or 0) > (raw.get("clientW") or 0) + 2)
    return result


def evaluate_page(page: dict, baseline: dict | None) -> tuple[str, list[dict]]:
    """예산·회귀와 대조해 판정과 지적사항을 만든다.

    지적사항은 **다음 행동이 바로 나오는 형태**여야 한다. "느립니다" 가 아니라
    "어느 API 가 몇 ms" 다.
    """
    route = page["route"]
    findings: list[dict] = []
    verdict = "pass"

    def bump(level: str) -> None:
        nonlocal verdict
        order = {"pass": 0, "warn": 1, "fail": 2}
        if order[level] > order[verdict]:
            verdict = level

    if page["status"] == "auth_failed":
        findings.append({"severity": "critical", "category": "auth",
                         "title": "로그인 화면으로 밀렸다 — 진단 불가",
                         "evidence": {"landed": page.get("landed_url")}})
        return "fail", findings
    if page["status"] in ("error", "timeout"):
        findings.append({"severity": "critical", "category": "load",
                         "title": f"페이지를 열지 못했다 ({page['status']})",
                         "evidence": {"error": page.get("error")}})
        return "fail", findings

    # 런타임 오류 — 경고 단계 없음
    for err in page["page_errors"]:
        findings.append({"severity": "critical", "category": "runtime",
                         "title": "잡히지 않은 예외", "evidence": {"error": err}})
        bump("fail")
    for hit in page["react_errors"]:
        findings.append({"severity": "critical", "category": "runtime",
                         "title": hit, "evidence": {}})
        bump("fail")
    if page["error_boundary"]:
        findings.append({"severity": "critical", "category": "runtime",
                         "title": "에러 바운더리가 화면을 대체했다",
                         "evidence": {"markers": page["error_boundary"]}})
        bump("fail")

    n_console = len(page["console_errors"])
    if n_console:
        findings.append({"severity": "minor", "category": "console",
                         "title": f"콘솔 오류/경고 {n_console}건",
                         "evidence": {"first": page["console_errors"][:3]}})
        bump("warn")

    failed = page["failed_requests"]
    # API 가 5xx 를 내면 화면에는 빈 칸으로 나타난다. 건수로 경중을 가르면
    # 502 한 건이 "경미"가 된다 — 첫 실행에서 실제로 그랬다.
    api_5xx = [f for f in failed
               if "/api/" in f.get("url", "") and f.get("reason", "").startswith("HTTP 5")]
    other = [f for f in failed if f not in api_5xx]
    if api_5xx:
        findings.append({"severity": "critical", "category": "network",
                         "title": f"API 서버 오류 {len(api_5xx)}건 — 화면에 데이터가 비어 보인다",
                         "evidence": {"calls": api_5xx[:5]}})
        bump("fail")
    if other:
        findings.append({"severity": "major" if len(other) >= 3 else "minor",
                         "category": "network",
                         "title": f"실패한 요청 {len(other)}건",
                         "evidence": {"first": other[:5]}})
        bump("fail" if len(other) >= 3 else "warn")

    # 예산
    for key, (warn, fail) in BUDGET.items():
        value = page.get(key) or 0
        if value >= fail:
            findings.append({"severity": "major", "category": "budget",
                             "title": f"{key} {int(value):,} — 예산 초과 (한도 {fail:,})",
                             "evidence": {"value": value, "limit": fail}})
            bump("fail")
        elif value >= warn:
            findings.append({"severity": "minor", "category": "budget",
                             "title": f"{key} {int(value):,} — 경고 (기준 {warn:,})",
                             "evidence": {"value": value, "limit": warn}})
            bump("warn")

    # 원인 지목 — 직렬 호출
    chain = page.get("serial_chain_ms") or 0
    slowest = page.get("api_slowest_ms") or 0
    if chain > 0 and chain > slowest * 1.5 and chain >= 800:
        findings.append({
            "severity": "major", "category": "data",
            "title": (f"API 직렬 호출 — 이어붙은 구간 {int(chain):,}ms, "
                      f"최장 단일 호출 {int(slowest):,}ms. "
                      f"병렬화 시 약 {int(chain - slowest):,}ms 단축 여지"),
            "evidence": {"serial_chain_ms": chain, "slowest_ms": slowest,
                         "calls": page.get("api_calls", [])[:12]},
        })
        bump("warn")

    dup = page.get("duplicate_api") or {}
    if dup:
        findings.append({"severity": "minor", "category": "data",
                         "title": f"같은 API 를 중복 호출 {len(dup)}종",
                         "evidence": dup})
        bump("warn")

    for call in page.get("api_calls", []):
        if call["bytes"] >= API_RESPONSE_FAIL:
            findings.append({"severity": "major", "category": "payload",
                             "title": f"과대 응답 {call['bytes']:,}B(압축 전) — {call['url']}",
                             "evidence": call})
            bump("fail")
        elif call["bytes"] >= API_RESPONSE_WARN:
            findings.append({"severity": "minor", "category": "payload",
                             "title": f"큰 응답 {call['bytes']:,}B(압축 전) — {call['url']}",
                             "evidence": call})
            bump("warn")

    if page.get("hidden_clickable"):
        findings.append({"severity": "minor", "category": "ui",
                         "title": f"보이지 않는 조작 요소 {len(page['hidden_clickable'])}개 "
                                  f"(전체 {page.get('clickable_total', 0)}개 중)",
                         "evidence": {"elements": page["hidden_clickable"][:10]}})
        bump("warn")
    if page.get("h_scroll"):
        findings.append({"severity": "minor", "category": "ui",
                         "title": "가로 스크롤이 생긴다", "evidence": {}})
        bump("warn")

    # 회귀 — 예산 안이어도 나빠졌으면 잡는다
    if baseline:
        for key, label in (("lcp_ms", "LCP"), ("data_wait_ms", "데이터 대기")):
            now = float(page.get(key) or 0)
            before = float(baseline.get(key) or 0)
            if before <= 0 or now <= before:
                continue
            delta = now - before
            if delta >= REGRESSION_MIN_MS and delta / before >= REGRESSION_PCT:
                findings.append({
                    "severity": "major", "category": "regression",
                    "title": (f"{label} 회귀 {int(before):,}ms → {int(now):,}ms "
                              f"(+{delta / before * 100:.0f}%)"),
                    "evidence": {"before": before, "after": now,
                                 "slowest_api": page.get("api_slowest_url")},
                })
                bump("fail")
    return verdict, findings


def load_baselines(routes: list[str], site: str = "aads") -> dict[str, dict]:
    """라우트별 직전 성공 측정값. 회귀 비교의 기준이다."""
    if not routes:
        return {}
    in_list = ", ".join(lit(r) for r in routes)
    try:
        out = psql(
            "SELECT DISTINCT ON (route) route, coalesce(lcp_ms,0), coalesce(data_wait_ms,0), "
            "coalesce(fcp_ms,0), coalesce(api_slowest_ms,0) "
            "FROM frontend_diagnostic_pages "
            # 부하가 섞인 측정은 기준선에서 제외한다. 오염된 값을 기준으로 삼으면
            # 다음 회귀 판정이 통째로 어긋난다.
            f"WHERE status='ok' AND site={lit(site)} AND route IN ({in_list}) "
            "  AND run_id IN (SELECT id FROM frontend_diagnostic_runs WHERE measurement_noisy = '') "
            "ORDER BY route, created_at DESC;"
        )
    except Exception:
        return {}
    baselines = {}
    for line in out.splitlines():
        parts = line.split("|")
        if len(parts) < 5:
            continue
        baselines[parts[0]] = {
            "lcp_ms": float(parts[1] or 0), "data_wait_ms": float(parts[2] or 0),
            "fcp_ms": float(parts[3] or 0), "api_slowest_ms": float(parts[4] or 0),
        }
    return baselines


def store(run_id: str, mode: str, release: str, pages: list[dict],
          verdicts: dict[str, str], findings: list[dict], verdict: str,
          *, site: str = "aads", load_info: dict | None = None, noisy: str = "") -> None:
    ok = sum(1 for p in pages if p["status"] == "ok")
    stmts = ["BEGIN;",
             "INSERT INTO frontend_diagnostic_runs "
             "(id, finished_at, mode, release_sha, base_url, site, load_info, measurement_noisy, "
             " pages_total, pages_ok, pages_failed, verdict, summary) "
             f"VALUES ({lit(run_id)}::uuid, now(), {lit(mode)}, {lit(release)}, {lit(BASE_URL)}, "
             f"{lit(site)}, {lit(json.dumps(load_info or {}, ensure_ascii=False))}::jsonb, {lit(noisy)}, "
             f"{len(pages)}, {ok}, {len(pages) - ok}, {lit(verdict)}, "
             f"{lit(json.dumps({'verdicts': verdicts}, ensure_ascii=False))}::jsonb);"]
    for p in pages:
        detail = {
            "api_calls": p.get("api_calls", [])[:40],
            "duplicate_api": p.get("duplicate_api", {}),
            "react_errors": p.get("react_errors", []),
            "error_boundary": p.get("error_boundary", []),
            "hidden_clickable": p.get("hidden_clickable", [])[:20],
            "console_errors": p.get("console_errors", [])[:10],
            "failed_requests": p.get("failed_requests", [])[:10],
            "landed_url": p.get("landed_url", ""),
            "error": p.get("error", ""),
        }
        stmts.append(
            "INSERT INTO frontend_diagnostic_pages (run_id, site, route, status, ttfb_ms, dcl_ms, fcp_ms, "
            "lcp_ms, load_ms, payload_bytes, wire_bytes, api_calls, api_total_ms, api_slowest_ms, api_slowest_url, "
            "data_wait_ms, serial_chain_ms, console_errors, page_errors, failed_requests, detail) VALUES ("
            f"{lit(run_id)}::uuid, {lit(site)}, {lit(p['route'])}, {lit(p['status'])}, {num(p.get('ttfb_ms'))}, "
            f"{num(p.get('dcl_ms'))}, {num(p.get('fcp_ms'))}, {num(p.get('lcp_ms'))}, {num(p.get('load_ms'))}, "
            f"{num(p.get('payload_bytes'))}, {num(p.get('wire_bytes'))}, {len(p.get('api_calls', []))}, {num(p.get('api_total_ms'))}, "
            f"{num(p.get('api_slowest_ms'))}, {lit(p.get('api_slowest_url'))}, {num(p.get('data_wait_ms'))}, "
            f"{num(p.get('serial_chain_ms'))}, {len(p.get('console_errors', []))}, "
            f"{len(p.get('page_errors', []))}, {len(p.get('failed_requests', []))}, "
            f"{lit(json.dumps(detail, ensure_ascii=False))}::jsonb);"
        )
    for f in findings:
        stmts.append(
            "INSERT INTO frontend_diagnostic_findings (run_id, route, severity, category, title, evidence) "
            f"VALUES ({lit(run_id)}::uuid, {lit(f['route'])}, {lit(f['severity'])}, {lit(f['category'])}, "
            f"{lit(f['title'])}, {lit(json.dumps(f.get('evidence', {}), ensure_ascii=False, default=str))}::jsonb);"
        )
    stmts.append("COMMIT;")
    psql("\n".join(stmts))


_report_load: dict = {}
_report_noisy: str = ""


def ms(value: Any) -> str:
    try:
        return f"{int(round(float(value))):,}"
    except (TypeError, ValueError):
        return "-"


def report(pages: list[dict], verdicts: dict[str, str], findings: list[dict],
           baselines: dict[str, dict]) -> None:
    print()
    print("━" * 78)
    print(f"  프론트 진단  ·  {BASE_URL}  ·  {time.strftime('%F %T')}")
    if _report_load:
        _l = _report_load
        print(f"  부하 load1={_l.get('load1','?')} / cpu={_l.get('cpus','?')}"
              + (f"  ⚠ {_report_noisy}" if _report_noisy else ""))
    print("━" * 78)
    print(f"{'라우트':<18} {'판정':<6} {'TTFB':>7} {'FCP':>7} {'LCP':>8} "
          f"{'데이터대기':>10} {'API':>4} {'최장API':>8}")
    print("─" * 78)
    for p in pages:
        v = verdicts.get(p["route"], "-")
        mark = {"pass": "정상", "warn": "경고", "fail": "실패"}.get(v, v)
        if p["status"] != "ok":
            print(f"{p['route']:<18} {mark:<6} {p['status']}")
            continue
        print(f"{p['route']:<18} {mark:<6} {ms(p.get('ttfb_ms')):>7} {ms(p.get('fcp_ms')):>7} "
              f"{ms(p.get('lcp_ms')):>8} {ms(p.get('data_wait_ms')):>10} "
              f"{len(p.get('api_calls', [])):>4} {ms(p.get('api_slowest_ms')):>8}")
        base = baselines.get(p["route"])
        if base and base.get("lcp_ms"):
            before, after = base["lcp_ms"], float(p.get("lcp_ms") or 0)
            if after and abs(after - before) >= 100:
                arrow = "나빠짐" if after > before else "좋아짐"
                print(f"{'':<25} 직전 LCP {ms(before)} → {ms(after)}  {arrow}")

    if findings:
        print()
        print("지적사항")
        print("─" * 78)
        order = {"critical": 0, "major": 1, "minor": 2}
        for f in sorted(findings, key=lambda x: (order.get(x["severity"], 9), x["route"])):
            tag = {"critical": "치명", "major": "주요", "minor": "경미"}[f["severity"]]
            print(f"  [{tag}] {f['route']}  {f['title']}")
    else:
        print("\n지적사항 없음")

    # 가장 느린 API 상위 — 개선 대상 1순위
    all_calls: list[dict] = []
    for p in pages:
        for c in p.get("api_calls", []):
            all_calls.append({**c, "route": p["route"]})
    if all_calls:
        print()
        print("가장 느린 API (개선 대상)")
        print("─" * 78)
        for c in sorted(all_calls, key=lambda x: -x["dur"])[:8]:
            print(f"  {ms(c['dur']):>7}ms  {c['bytes']:>9,}B  {c['route']:<16} {c['url'][:60]}")
    print()


def main() -> int:
    ap = argparse.ArgumentParser(description="프론트 진단기")
    ap.add_argument("--quick", action="store_true", help="핵심 라우트")
    ap.add_argument("--full", action="store_true", help="전체 라우트")
    ap.add_argument("--url", action="append", default=[], help="지정 경로 (반복 가능)")
    ap.add_argument("--no-db", action="store_true", help="DB 적재 생략")
    ap.add_argument("--no-refresh", action="store_true", help="storage_state 갱신 생략")
    ap.add_argument("--timeout", type=int, default=45000)
    ap.add_argument("--json", default="", help="결과 JSON 저장 경로")
    ap.add_argument("--site", default=os.getenv("AADS_FRONTEND_SITE", "aads"),
                    help="진단 대상 사이트 (config/frontend_diagnostics_sites.json)")
    ap.add_argument("--list-sites", action="store_true", help="등록된 사이트 목록")
    args = ap.parse_args()

    global BASE_URL, STORAGE_STATE, REFRESHER, CORE_ROUTES, MAJOR_ROUTES
    sites = load_sites()
    if args.list_sites:
        for key, cfg in sites.items():
            print(f"  {key:<10} {cfg.get('label','')}  {cfg.get('base_url','')}")
        return 0
    site = sites.get(args.site)
    if not site:
        print(f"[중단] 사이트 '{args.site}' 정의 없음. --list-sites 로 확인하라.", file=sys.stderr)
        return 2
    BASE_URL = os.getenv("AADS_FRONTEND_BASE_URL") or site.get("base_url", "")
    CORE_ROUTES = list(site.get("core_routes") or [])
    MAJOR_ROUTES = list(site.get("major_routes") or [])
    _auth = site.get("auth") or {}
    STORAGE_STATE = Path(os.getenv("AADS_QA_STORAGE_STATE") or _auth.get("storage_state") or "")
    REFRESHER = Path(_auth.get("refresh") or "")

    if args.url:
        routes = list(args.url)
    elif args.full:
        routes = CORE_ROUTES + MAJOR_ROUTES
    else:
        routes = list(CORE_ROUTES)
    if CHAT_SESSION and "/chat" in routes:
        routes[routes.index("/chat")] = f"/chat#{CHAT_SESSION}"

    load_before = capture_load()
    noisy = load_is_noisy(load_before)
    print(f"[진단] {args.site} · {len(routes)}개 라우트 · {BASE_URL}")
    if noisy:
        print(f"[경고] {noisy} — 시간 지표를 회귀 근거로 쓰지 마라")
    if not ensure_storage_state(not args.no_refresh):
        print("[중단] storage_state 가 없다. 인증 없이 열면 로그인 화면만 진단하게 된다.",
              file=sys.stderr)
        return 2

    release = ""
    try:
        release = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                                 capture_output=True, text=True, timeout=10,
                                 cwd="/root/aads/aads-server").stdout.strip()
    except Exception:
        pass

    baselines = {} if args.no_db else load_baselines([r.split("#")[0] for r in routes], args.site)
    if noisy:
        # 부하 중 측정은 회귀 판정에 쓰지 않는다. 예산·런타임 오류는 그대로 본다.
        baselines = {}

    from playwright.sync_api import sync_playwright

    pages: list[dict] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        ctx = browser.new_context(viewport={"width": 1440, "height": 900},
                                  storage_state=str(STORAGE_STATE))
        for route in routes:
            print(f"  · {route}", flush=True)
            try:
                pages.append(diagnose_page(ctx, route, args.timeout))
            except Exception as exc:
                pages.append({"route": route, "status": "error", "error": str(exc)[:300],
                              "console_errors": [], "page_errors": [], "failed_requests": []})
        browser.close()

    verdicts: dict[str, str] = {}
    findings: list[dict] = []
    overall = "pass"
    order = {"pass": 0, "warn": 1, "fail": 2}
    for page in pages:
        base_route = page["route"].split("#")[0]
        v, fs = evaluate_page(page, baselines.get(base_route))
        verdicts[page["route"]] = v
        for f in fs:
            findings.append({**f, "route": page["route"]})
        if order[v] > order[overall]:
            overall = v

    global _report_load, _report_noisy
    _report_load, _report_noisy = load_before, noisy
    # 런타임 오류에 알려진 원인을 붙인다.
    _err_texts: list[str] = []
    for _p in pages:
        _err_texts.extend(_p.get("page_errors", []))
        _err_texts.extend(c.get("text", "") for c in _p.get("console_errors", []))
        _err_texts.extend(_p.get("react_errors", []))
        if _p.get("error"):
            _err_texts.append(str(_p["error"]))
    for _line in lookup_error_book(_err_texts):
        findings.append({
            "severity": "minor", "category": "error_book",
            "title": _line["text"], "evidence": {}, "route": "-",
        })

    report(pages, verdicts, findings, baselines)

    run_id = str(uuid.uuid4())
    if not args.no_db:
        try:
            store(run_id, "full" if args.full else "quick", release, pages, verdicts, findings, overall,
                  site=args.site, load_info=load_before, noisy=noisy)
            print(f"[저장] run_id={run_id}")
        except Exception as exc:
            print(f"[경고] DB 적재 실패: {str(exc)[:200]}", file=sys.stderr)

    if args.json:
        Path(args.json).write_text(json.dumps(
            {"run_id": run_id, "verdict": overall, "pages": pages, "findings": findings},
            ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        print(f"[저장] {args.json}")

    print(f"[판정] {overall.upper()}")
    return {"pass": 0, "warn": 0, "fail": 1}[overall]


if __name__ == "__main__":
    sys.exit(main())
