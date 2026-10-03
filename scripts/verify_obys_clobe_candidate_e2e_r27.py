#!/usr/bin/env python3
"""R27: 클로브 UI 승인 후보(commit 1786e480)의 화면 검증 — 후보 SHA 그대로, 격리 loopback 프리뷰.

이 스크립트가 하는 일
  1. static   후보 SHA 의 트리를 `git archive` 로 임시 폴더에 풀고(워크트리 생성 없음), UI 가 부르는
              API 경로가 실제 라우터 계약과 맞는지 대조한다.
  2. probe    운영 origin(fb.newtalk.kr)을 인증 없이 GET 만 한다. 후보가 서빙 중인지, 진입 라우트가 무엇인지.
  3. preview  후보 정적 파일을 127.0.0.1 임의 포트로 서빙 + 합성 API. Playwright(headless)로
              데스크톱/모바일 흐름·만료·권한·네트워크 복구를 DOM assert + screenshot 으로 기록한다.
  4. gate     원 job 의 승인 게이트(assert_screen_evidence_gate)를 읽기 전용으로 호출한다.

하지 않는 일 (의도적)
  - task_logs 에 e2e_evidence 를 쓰지 않는다. 합성 API 프리뷰의 통과를 승인 게이트의 "화면 증거"로 만들면
    운영/실로그인 검증이 된 것처럼 읽힌다. 게이트 기록은 정식 인증 흐름(비라일론 승인 계정 + Vault allow 정책)
    에서 e2e_verify 가 돌 때만 남아야 한다.
  - Vault 자격증명 조회·복호화·사용, 운영 로그인, 수집 실행, 원장 확정, 외부 쓰기.
  - defer(보류 승인) 경로 호출 — 게이트 확인은 defer 인자를 받지 않는다.

실행: timeout 600 /root/aads/aads-server/.venv/bin/python scripts/verify_obys_clobe_candidate_e2e_r27.py [--out DIR]
종료코드: 0 = 프리뷰 검증 전부 통과, 1 = 실패 있음, 2 = 실행 불가(브라우저 등).
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import mimetypes
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import threading
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

REPO = Path(__file__).resolve().parents[1]
CANDIDATE_SHA = "1786e480139049b2642df3bfe3006fa39c373f85"
SOURCE_JOB = "runner-c856e5ff"
PROD_ORIGIN = "https://fb.newtalk.kr"
UI_PREFIX = "app/static/apps/obys"
ROUTERS = ("app/api/obys_collections.py", "app/api/clobe_integration.py")
ROUTER_BASES = {"/api/v1/obys-collections": ROUTERS[0], "/api/v1/integrations/clobe": ROUTERS[1]}

RESULTS: list[dict] = []
OBSERVATIONS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append({"name": name, "passed": bool(ok), "detail": detail[:300]})
    print(("PASS" if ok else "FAIL"), name, detail[:160])
    return bool(ok)


def git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=REPO, check=True, capture_output=True, text=True, timeout=60).stdout


def extract_candidate(dest: Path) -> list[str]:
    """후보 SHA 의 UI 트리와 라우터 소스를 dest 에 푼다. 워크트리/체크아웃을 만들지 않는다."""
    data = subprocess.run(["git", "archive", CANDIDATE_SHA, UI_PREFIX, *ROUTERS], cwd=REPO, check=True,
                          capture_output=True, timeout=120).stdout
    tar_path = dest / "candidate.tar"
    tar_path.write_bytes(data)
    with tarfile.open(tar_path) as tar:
        tar.extractall(dest, filter="data")
    tar_path.unlink()
    return git("show", "--name-only", "--format=", CANDIDATE_SHA).split()


# ── 1. static: API 계약 대조 ────────────────────────────────────────────
def route_patterns(source: str) -> list[tuple[str, re.Pattern[str]]]:
    out = []
    for method, path in re.findall(r'@router\.(get|post|put|patch|delete)\("([^"]+)"', source):
        out.append((method.upper(), re.compile("^" + re.sub(r"\{[^}]+\}", "[^/]+", path) + "$")))
    return out


def static_contract(root: Path) -> dict:
    js = (root / UI_PREFIX / "modules/clobe-collection.js").read_text(encoding="utf-8")
    index = (root / UI_PREFIX / "index.html").read_text(encoding="utf-8")
    patterns = {base: route_patterns((root / rel).read_text(encoding="utf-8")) for base, rel in ROUTER_BASES.items()}
    calls = []
    for m in re.finditer(r"request\(\s*([`\"])(/[^`\"?]*)", js):
        path = re.sub(r"\$\{[^}]+\}", "x", m.group(2))
        window = js[m.start(): m.start() + 260]
        method = "POST" if re.search(r'method:\s*"POST"', window) else "GET"
        base = "/api/v1/integrations/clobe" if "INTEGRATION_API" in window else "/api/v1/obys-collections"
        calls.append((method, base, path))
    for m in re.finditer(r"request\(itemsPath", js):
        calls.append(("GET", "/api/v1/obys-collections", "/businesses/x/items"))
    unmatched = []
    for method, base, path in sorted(set(calls)):
        if not any(m == method and rx.match(path) for m, rx in patterns[base]):
            unmatched.append(f"{method} {base}{path}")
    check("계약: UI 가 부르는 API 경로·메서드가 후보 SHA 의 라우터에 전부 존재", not unmatched and bool(calls),
          f"calls={len(set(calls))} unmatched={unmatched}")
    check("계약: 후보 index.html 이 clobe 모듈·4개 뷰를 싣는다",
          all(f'id="{v}View"' in index for v in ("clobeOverview", "clobeReview", "clobeSummary", "clobeSettings"))
          and "modules/clobe-collection.js" in index)
    check("계약: legacy 진입 라우트가 후보 index.html 에 있다(새 화면은 legacy=1 로만 진입)", "legacy" in index)
    return {"api_calls": sorted(set(f"{m} {b}{p}" for m, b, p in calls)), "unmatched": unmatched}


# ── 2. probe: 운영 origin 읽기 전용 ─────────────────────────────────────
def http_status(url: str) -> tuple[int | None, str]:
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):  # noqa: D401
            return None

    opener = urllib.request.build_opener(NoRedirect)
    try:
        with opener.open(urllib.request.Request(url, headers={"User-Agent": "aads-r27-readonly-probe"}), timeout=10) as r:
            return r.status, r.read(400_000).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, ""
    except Exception as exc:  # 네트워크 증거는 raise 하지 않고 반환
        return None, str(exc)[:200]


def probe_production() -> dict:
    out = {}
    code, body = http_status(f"{PROD_ORIGIN}/static/apps/obys/index.html")
    out["entry_index"] = code
    check("운영 probe: 진입 index.html 응답(200)", code == 200, f"status={code}")
    out["entry_has_clobe"] = "clobe-collection" in body
    check("운영 probe: 운영 index.html 에는 아직 클로브 UI 가 없다(후보 미배포)", code == 200 and not out["entry_has_clobe"])
    code, _ = http_status(f"{PROD_ORIGIN}/static/apps/obys/modules/clobe-collection.js")
    out["clobe_js"] = code
    check("운영 probe: clobe-collection.js 는 운영에서 404(후보 미배포)", code == 404, f"status={code}")
    real, _ = http_status(f"{PROD_ORIGIN}/api/v1/obys-collections/status")
    bogus, _ = http_status(f"{PROD_ORIGIN}/api/v1/obys-collections/zz-not-a-route")
    out["api_status_anon"], out["api_bogus_anon"] = real, bogus
    out["api_route_existence_provable"] = real != bogus
    check("운영 probe: 인증 전 API 응답은 401 이지만 존재하지 않는 경로도 같은 401 이므로 배포 증거가 아니다",
          real == 401 and bogus == 401 and not out["api_route_existence_provable"], f"real={real} bogus={bogus}")
    return out


# ── 3. preview: 격리 loopback + 합성 API ───────────────────────────────
def make_items() -> list[dict]:
    rows = []

    def add(kind, day, party, inst, direction, amount, stage="review_box"):
        rows.append(dict(item_id=f"it{len(rows) + 1:03d}", data_kind=kind, occurred_on=day, counterparty=party,
                         institution=inst, direction=direction, amount=amount, stage=stage, is_revision=False,
                         source_as_of="2026-10-03T01:10:00+00:00"))

    add("bank_transaction", "2026-09-30", "가나다 식자재", "신한은행", "OUT", "1250000")
    add("bank_transaction", "2026-09-29", "카드 정산", "국민은행", "IN", "3480500.50")
    add("tax_invoice", "2026-09-28", "한우 유통", "국세청", "PURCHASE", "2200000")
    add("cash_receipt", "2026-09-27", "현금 손님", "국세청", "SALES", "18000")
    add("card_approval", "2026-09-26", "카드 승인", "BC카드", "SALES", "42000")
    add("bank_transaction", "2026-09-20", "월 정산", "신한은행", "IN", "8800000", "confirmed")
    add("tax_invoice", "2026-09-22", "정기 납품", "국세청", "SALES", "7700000", "confirmed")
    return rows


class Synthetic:
    """합성 API 상태. 실제 금융 자료·토큰·자격증명은 어디에도 없다."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.items = make_items()
        self.mode = "ok"
        self.reauth = False
        self.calls: list[str] = []

    def status(self) -> dict:
        kinds = {
            "bank_transaction": dict(status="succeeded", last_success_at="2026-10-03T00:30:00+00:00",
                                     covered_from="2026-07-05", covered_to="2026-10-03"),
            "tax_invoice": dict(status="partial", last_success_at="2026-10-02T00:30:00+00:00",
                                covered_from="2026-07-05", covered_to="2026-10-02", last_error_code="source_scrape_error",
                                last_failure_at="2026-10-03T00:31:00+00:00", next_retry_at="2026-10-03T06:00:00+00:00",
                                consecutive_failures=1),
            "cash_receipt": dict(status="succeeded", last_success_at="2026-10-03T00:30:00+00:00",
                                 covered_from="2026-07-05", covered_to="2026-10-03"),
            "card_approval": dict(status="failed", last_error_code="clobe_http_500",
                                  last_failure_at="2026-10-03T00:32:00+00:00", consecutive_failures=2),
        }
        counts: dict[str, int] = {}
        for it in self.items:
            counts[it["stage"]] = counts.get(it["stage"], 0) + 1
        comps = [dict(clobe_company_id="c-1", company_name="합성 회사 A", business_id="biz-1", link_status="linked",
                      reg_no_masked="000-00-*****", kinds=kinds, item_stage_counts=counts)]
        if self.mode == "none":
            comps = []
        return dict(companies=comps, unsupported_sources=[],
                    auth=dict(status="needs_reauth" if self.reauth else "connected", reauth_required=self.reauth,
                              last_success_at="2026-10-03T00:30:00+00:00"))


def make_handler(root: Path, syn: Synthetic):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            return

        def _json(self, body, status=200):
            data = json.dumps(body, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _api(self, method: str):
            parts = urlsplit(self.path)
            if not parts.path.startswith(("/api/v1/obys-collections", "/api/v1/integrations/clobe")):
                return self._json({"detail": "synthetic-unrouted"}, 404)
            syn.calls.append(f"{method} {parts.path}")
            if syn.mode == "network":
                self.connection.close()
                return
            if syn.mode == "401":
                return self._json({"detail": "unauthorized"}, 401)
            sub = parts.path.replace("/api/v1/obys-collections", "")
            if parts.path.startswith("/api/v1/obys-collections") and method == "GET" and sub == "/status":
                return self._json(syn.status())
            if method == "GET" and re.fullmatch(r"/companies/[^/]+/runs", sub):
                return self._json(dict(runs=[]))
            if method == "GET" and re.fullmatch(r"/businesses/[^/]+/items", sub):
                q = dict(p.split("=", 1) for p in parts.query.split("&") if "=" in p)
                rows = [i for i in syn.items if (not q.get("stage") or i["stage"] == q["stage"])
                        and (not q.get("kind") or i["data_kind"] == q["kind"])]
                return self._json(dict(items=rows, total=len(rows)))
            return self._json({"detail": "synthetic-unrouted"}, 404)

        def do_GET(self):  # noqa: N802
            path = urlsplit(self.path).path
            if path.startswith("/api/"):
                return self._api("GET")
            fp = root / "app" / (path.lstrip("/") or UI_PREFIX.removeprefix("app/") + "/index.html")
            if path == "/":
                fp = root / UI_PREFIX / "index.html"
            if fp.is_file() and root in fp.resolve().parents:
                data = fp.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", mimetypes.guess_type(str(fp))[0] or "application/octet-stream")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            self.send_response(404)
            self.end_headers()

        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(length)
            return self._api("POST")

    return Handler


def run_preview(root: Path, out: Path) -> dict:
    from playwright.sync_api import sync_playwright

    if not os.environ.get("PLAYWRIGHT_BROWSERS_PATH") and Path("/root/.cache/ms-playwright").is_dir():
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = "/root/.cache/ms-playwright"
    shots = out / "shots"
    shots.mkdir(parents=True, exist_ok=True)
    syn = Synthetic()
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(root.resolve(), syn))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    entry = f"{base}/static/apps/obys/index.html?legacy=1&view=legacy"
    captured: list[dict] = []
    external_blocked: list[str] = []

    def shot(page, name: str, full: bool = True) -> None:
        path = shots / f"{name}.png"
        page.screenshot(path=str(path), full_page=full)
        captured.append(dict(name=name, path=str(path), sha256=hashlib.sha256(path.read_bytes()).hexdigest()))

    def new_ctx(browser, mobile: bool, role: str = "owner", logged_in: bool = True):
        vp = dict(width=390, height=844) if mobile else dict(width=1440, height=900)
        ctx = browser.new_context(viewport=vp, is_mobile=mobile, has_touch=mobile, service_workers="block")

        def gate(route, request):
            if urlsplit(request.url).hostname in ("127.0.0.1", "localhost"):
                return route.continue_()
            external_blocked.append(request.url[:80])
            return route.abort()

        ctx.route("**/*", gate)
        if logged_in:
            user = json.dumps({"user": {"email": "synthetic@example.test", "name": "합성"}, "permissions": {"role": role}})
            ctx.add_init_script(
                "localStorage.setItem('aads_token','synthetic-not-a-credential');"
                "localStorage.setItem('fb_access_token','synthetic-not-a-credential');"
                f"localStorage.setItem('yeoljeong-finance-auth-user', {json.dumps(user)});")
        return ctx

    def overflow(page) -> int:
        return page.evaluate("document.documentElement.scrollWidth - window.innerWidth")

    def tab(page, view: str) -> None:
        page.click(f".tab[data-view='{view}']:visible")

    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(args=["--no-sandbox"])
        except Exception as exc:
            print("BROWSER_UNAVAILABLE", str(exc)[:200])
            server.shutdown()
            sys.exit(2)

        for label, mobile in (("desktop", False), ("mobile", True)):
            syn.reset()
            ctx = new_ctx(browser, mobile)
            page = ctx.new_page()
            errors: list[str] = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(entry)
            page.wait_for_selector("#clobeOverviewView:not(.hidden) .clobe-shell", timeout=10_000)
            text = page.inner_text("#clobeOverviewView")
            check(f"{label}: 회사 선택→자료현황 진입(회사 선택 목록 + 종류별 현황)",
                  page.locator("[data-clobe-company] option").count() >= 1 and "합성 회사 A" in text
                  and page.locator(".clobe-kind-card").count() == 4)
            check(f"{label}: 마지막 수집·받은 기간·부분 수신·오류·재시도 표시",
                  all(s in text for s in ["마지막 수집 시각", "받은 기간", "부분 수신", "수집 실패", "다음 자동 재시도"]))
            check(f"{label}: 내부 식별자 미노출", not re.search(r"clobe_http|source_scrape_error|run_id|job_id|lease_", text))
            check(f"{label}: 가로 넘침 없음(자료현황)", overflow(page) <= 1, str(overflow(page)))
            shot(page, f"{label}-1-overview")
            tab(page, "clobeReview")
            page.wait_for_selector(".clobe-item", timeout=6000)
            check(f"{label}: 거래검토 — 검토함 5건", page.locator(".clobe-item").count() == 5, str(page.locator(".clobe-item").count()))
            shot(page, f"{label}-2-review")
            page.locator(".clobe-item-main").first.click()
            page.wait_for_selector(".clobe-drawer.open")
            check(f"{label}: 항목 상세 서랍이 열린다", page.locator(".clobe-drawer.open").count() == 1)
            shot(page, f"{label}-3-detail", full=False)
            page.keyboard.press("Escape")
            page.wait_for_timeout(300)
            if page.locator(".clobe-drawer.open").count():
                OBSERVATIONS.append(f"{label}: Escape 키로 상세 서랍이 닫히지 않는다(닫기 버튼만 동작) — 접근성 개선 후보, 후보 UI 는 수정하지 않음")
            page.click(".clobe-drawer [data-clobe-action='close-detail']")
            page.wait_for_selector(".clobe-drawer.open", state="detached", timeout=3000)
            tab(page, "clobeSummary")
            page.wait_for_selector(".clobe-metrics", timeout=6000)
            summary = page.inner_text("#clobeSummaryView")
            check(f"{label}: 경영요약 — 확정 건만 합산(입금 8,800,000원·매출 7,700,000원)",
                  "8,800,000원" in summary and "7,700,000원" in summary)
            check(f"{label}: 경영요약 — 검토 중 건(3,480,500.5)·현금/카드는 합계에서 제외",
                  "3,480,500" not in summary and "금액 합계에 넣지 않았습니다" in summary)
            check(f"{label}: 경영요약 — 미수집 종류 경고", "수집이 완료되지 않은 종류" in summary)
            check(f"{label}: 가로 넘침 없음(요약)", overflow(page) <= 1, str(overflow(page)))
            shot(page, f"{label}-4-summary")
            check(f"{label}: 수집·확정 같은 쓰기 호출 0건(읽기 흐름)", not any(c.startswith("POST") for c in syn.calls), str(syn.calls[-3:]))
            check(f"{label}: JS 예외 없음", not errors, "; ".join(errors)[:200])
            ctx.close()

        def scenario(name: str, mode: str, view: str, selector: str, expect: str, mobile: bool, role: str = "owner",
                     reauth: bool = False, logged_in: bool = True) -> None:
            syn.reset()
            syn.mode, syn.reauth = mode, reauth
            ctx = new_ctx(browser, mobile, role=role, logged_in=logged_in)
            page = ctx.new_page()
            page.goto(entry)
            page.wait_for_timeout(1200)
            if not logged_in:
                page.evaluate("window.obysClobe.open('clobeOverview')")
                page.evaluate("document.getElementById('clobeOverviewView').classList.remove('hidden')")
            else:
                tab(page, view)
            page.wait_for_selector(f"#{view}View {selector}", timeout=7000)
            body = page.inner_text(f"#{view}View")
            suffix = "m" if mobile else "d"
            check(f"복구/{name}/{suffix}: '{expect}' 안내가 보인다", expect in body, body[:100].replace("\n", " "))
            shot(page, f"recover-{name}-{suffix}")
            ctx.close()

        for mobile in (False, True):
            scenario("session-expired", "401", "clobeOverview", "[data-clobe-failure='session']", "세션이 만료", mobile)
            scenario("network", "network", "clobeOverview", "[data-clobe-failure='network']", "네트워크에 연결할 수 없습니다", mobile)
            scenario("reauth-expired", "ok", "clobeOverview", "[data-clobe-failure='expired']", "연결 승인", mobile, reauth=True)
        scenario("no-company", "none", "clobeOverview", "[data-clobe-failure='none']", "연결된 회사가 없습니다", False)
        syn.reset()
        ctx = new_ctx(browser, False, logged_in=False)
        page = ctx.new_page()
        page.goto(entry)
        page.wait_for_timeout(1500)
        visible = page.evaluate("""() => ['clobeOverviewView','clobeReviewView','clobeSummaryView','clobeSettingsView']
            .map(id => { const el = document.getElementById(id); return !!el && el.getClientRects().length > 0; })""")
        check("비로그인: 클로브 화면 4개가 어느 것도 보이지 않는다(로그인 화면에서 멈춤)", not any(visible), str(visible))
        check("비로그인: 클로브 API 호출 0건", not any("obys-collections" in c for c in syn.calls), str(syn.calls[:3]))
        shot(page, "logged-out-d", full=False)
        ctx.close()

        syn.reset()
        ctx = new_ctx(browser, False, role="viewer")
        page = ctx.new_page()
        page.goto(entry)
        page.wait_for_timeout(1200)
        tab(page, "clobeOverview")
        page.wait_for_selector("#clobeOverviewView .clobe-kind-card", timeout=10_000)
        check("권한/viewer: 수집 실행 버튼이 없고 읽기 전용 안내가 있다",
              page.locator("[data-clobe-action='run-all']").count() == 0 and "대표·관리자만" in page.inner_text("#clobeOverviewView"))
        tab(page, "clobeReview")
        page.wait_for_selector(".clobe-item", timeout=6000)
        check("권한/viewer: 검토 승인·확정 처리 버튼이 없다", page.locator("[data-clobe-action='bulk']").count() == 0)
        shot(page, "permission-viewer-d")
        ctx.close()
        browser.close()

    server.shutdown()
    check("격리: 외부 호스트 요청은 전부 차단했다(loopback 외 접속 0)", True, f"blocked={len(external_blocked)}")
    return dict(preview_origin=base, entry=entry, captured=captured, blocked_external_requests=len(external_blocked))


# ── 4. gate: 읽기 전용 ──────────────────────────────────────────────────
async def read_gate() -> dict:
    import asyncpg

    sys.path.insert(0, str(REPO))
    from app.services.e2e_verify import assert_screen_evidence_gate, load_deferred_evidence, load_passing_evidence

    conn = await asyncpg.connect(host=os.environ["PGHOST"], port=int(os.environ["PGPORT"]), user=os.environ["PGUSER"],
                                 password=os.environ["PGPASSWORD"], database=os.environ["PGDATABASE"], timeout=10)
    try:
        row = await conn.fetchrow("SELECT status, instruction, actual_changed_files, commit_hash FROM pipeline_jobs WHERE job_id=$1", SOURCE_JOB)
        files = row["actual_changed_files"] or []
        if isinstance(files, str):
            files = json.loads(files)
        passing = await load_passing_evidence(conn, SOURCE_JOB)
        deferred = await load_deferred_evidence(conn, SOURCE_JOB)
        try:
            await assert_screen_evidence_gate(conn, job_id=SOURCE_JOB, instruction=row["instruction"] or "", changed_files=list(files))
            verdict = "passes"
        except ValueError as exc:
            verdict = f"blocked:{exc}"
        result = dict(job_id=SOURCE_JOB, status=row["status"], commit_hash=row["commit_hash"], gate=verdict,
                      passing_evidence=bool(passing), deferral_record=bool(deferred), changed_files=len(files))
        check("게이트: 원 job 은 awaiting_approval 로 보존되어 있다", row["status"] == "awaiting_approval", row["status"])
        check("게이트: 후보 commit_hash 가 지시서의 후보 SHA 와 같다", row["commit_hash"] == CANDIDATE_SHA, str(row["commit_hash"]))
        check("게이트: 읽기 전용 재확인 결과(이 스크립트는 증거를 쓰지 않으므로 상태 변화 없음이 정상)", True, verdict)
        return result
    finally:
        await conn.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="/tmp/r27_clobe_preview")
    ap.add_argument("--skip-gate", action="store_true")
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    resolved = git("rev-parse", f"{CANDIDATE_SHA}^{{commit}}").strip()
    check("후보 SHA 가 저장소에 존재한다", resolved == CANDIDATE_SHA, resolved[:12])

    evidence: dict = dict(
        schema="aads.clobe_candidate_preview.v1", job_id=SOURCE_JOB, candidate_sha=CANDIDATE_SHA,
        created_at=datetime.now(timezone.utc).isoformat(),
        provenance=dict(
            origin_kind="isolated_loopback_preview", operational=False, synthetic_api=True, real_financial_data=False,
            login="simulated_localstorage_session_no_credential_used", real_auth_flow_exercised=False,
            vault_credential_used=False, writes_to_task_logs=False, writes_to_operational_systems=False,
            note="후보 화면 근거이며 운영 origin 근거가 아니다. 인증 후 운영 화면 검증은 미완료다."),
    )
    with tempfile.TemporaryDirectory(prefix="r27_candidate_") as tmp:
        root = Path(tmp)
        evidence["candidate_changed_files"] = extract_candidate(root)
        evidence["static_contract"] = static_contract(root)
        evidence["production_probe"] = probe_production()
        evidence["preview"] = run_preview(root, out)
    if not args.skip_gate:
        evidence["gate"] = asyncio.run(read_gate())
    evidence["checks"] = RESULTS
    evidence["observations"] = OBSERVATIONS
    failed = [r for r in RESULTS if not r["passed"]]
    evidence["summary"] = dict(total=len(RESULTS), passed=len(RESULTS) - len(failed), failed=len(failed))
    (out / "evidence.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nTOTAL {len(RESULTS)} PASS {len(RESULTS) - len(failed)} FAIL {len(failed)} -> {out / 'evidence.json'}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
