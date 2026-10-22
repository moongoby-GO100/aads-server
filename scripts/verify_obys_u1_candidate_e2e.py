#!/usr/bin/env python3
"""U1: 오비서 admin.html(V4.1)·index.html(legacy) 후보 소스의 PC/390px 화면 검증 (격리 loopback + 합성 API).

이 스크립트는 '후보 소스' 검증이다. 운영(fb.newtalk.kr) 성공을 뜻하지 않는다.
  - 워크트리의 app/static 을 127.0.0.1 임의 포트로 서빙하고 /api/v1/** 는 Playwright route 로 합성 응답한다.
  - 외부 호스트는 모두 차단한다. Vault 자격증명·운영 로그인·운영 쓰기는 하지 않는다.
  - 합성 급여 금액은 가짜 값이다(개인급여 원문 아님).

검사
  A. admin.html: owner/admin/member/viewer 별 메뉴 노출 == 서버 ROUTE_READ_ROLES 와 일치, 보이는 메뉴의 API 가 403 이 아님,
     직접 #payroll 진입 시 member/viewer 는 home 으로 복귀, owner 급여표에서 총지급액이 기본급으로 표시되지 않음.
  B. index.html?legacy=1&view=legacy: 5지표가 200/403/404/500/미로그인/렌더예외에서 '불러오는 중'으로 멈추지 않음.

실행: timeout 300 python3 scripts/verify_obys_u1_candidate_e2e.py [--out DIR]
종료코드: 0 전부 통과, 1 실패 있음, 2 브라우저 실행 불가.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import mimetypes
import os
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "app"
RESULTS: list[dict] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append(dict(name=name, ok=bool(ok), detail=detail))
    print(("PASS " if ok else "FAIL ") + name + (f" — {detail}" if detail and not ok else ""))
    return bool(ok)


def server_manager_routes() -> set[str]:
    tree = ast.parse((ROOT / "app/api/obys_workspaces.py").read_text(encoding="utf-8"))
    node = next(n for n in tree.body if isinstance(n, ast.AnnAssign) and getattr(n.target, "id", "") == "ROUTE_READ_ROLES")
    return {ast.literal_eval(k) for k in node.value.keys}


def make_static_handler():
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            return

        def do_GET(self):  # noqa: N802
            path = urlsplit(self.path).path
            fp = (STATIC / path.lstrip("/")).resolve()
            if fp.is_file() and STATIC.resolve() in fp.parents:
                data = fp.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", mimetypes.guess_type(str(fp))[0] or "application/octet-stream")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            self.send_response(404)
            self.end_headers()

    return Handler


class Api:
    """Playwright route 로 붙는 합성 API. role/mode 로 상황을 바꾼다."""

    def __init__(self, manager_routes: set[str]) -> None:
        self.manager_routes = manager_routes
        self.role = "owner"
        self.home_mode: dict[str, int] = {}
        self.calls: list[str] = []
        self.denied: list[str] = []

    def respond(self, route, request) -> None:
        url = urlsplit(request.url)
        if url.hostname not in ("127.0.0.1", "localhost"):
            return route.abort()
        if not url.path.startswith("/api/"):
            return route.continue_()
        path = url.path
        self.calls.append(f"{request.method} {path}")

        def js(body, status=200):
            route.fulfill(status=status, content_type="application/json", body=json.dumps(body, ensure_ascii=False))

        if path == "/api/v1/auth/me":
            return js(dict(tenant_id="t-1", membership=dict(tenant_id="t-1", role=self.role, status="active"),
                           user=dict(email="synthetic@example.test", name="합성")))
        if path == "/api/v1/auth/tenants":
            return js(dict(tenants=[dict(tenant_id="t-1", name="합성 조직")], current_tenant_id="t-1"))
        if path == "/api/v1/workspaces/businesses":
            return js(dict(businesses=[dict(id="b-1", name="합성 사업자 A", entity_type="법인", tax_type="일반과세")]))
        m = re.fullmatch(r"/api/v1/workspaces/b-1/([a-z\-]+)/(summary|records)", path)
        if m:
            name, kind = m.groups()
            if name in self.manager_routes and self.role not in ("owner", "admin"):
                self.denied.append(name)
                return js(dict(detail="이 화면은 소유자·관리자만 조회할 수 있습니다"), 403)
            if name == "attendance" and self.role == "viewer":
                self.denied.append(name)
                return js(dict(detail="이 화면은 소유자·관리자만 조회할 수 있습니다"), 403)
            if kind == "summary":
                return js(dict(metrics=[], source=dict(live=True, name="synthetic")))
            if name == "payroll":
                raw = dict(gross_pay="3000000", tax_withholding="100000", insurance_deduction="200000",
                           other_deduction="0", net_pay="2700000")
                return js(dict(records=[dict(id="pay-1", status="확정", raw=raw, display={"직원": "합성직원", "기본급": "잘못된 값"})],
                               has_more=False, source=dict(live=True, name="synthetic")))
            return js(dict(records=[], has_more=False, source=dict(live=True, name="synthetic")))
        if path == "/api/v1/yeoljeong-finance/session":
            return js(dict(permissions=dict(can_manage_onboarding=self.role in ("owner", "admin"), role=self.role)))
        home = {"/sales": "sales", "/settlements": "sales", "/bank-summary": "bank", "/accounts": "bank", "/reviews": "review",
                "/payroll": "payroll", "/employees/join-requests": "approval", "/onboarding/documents": "approval",
                "/contracts/pending-signature": "approval", "/collection-status": "sales"}
        sub = path.replace("/api/v1/yeoljeong-finance", "", 1)
        if path.startswith("/api/v1/yeoljeong-finance") and sub in home:
            status = self.home_mode.get(sub, 200)
            if status != 200:
                return js(dict(detail=f"synthetic {status}"), status)
            bodies = {"/sales": dict(sales=[]), "/settlements": dict(settlements=[]),
                      "/bank-summary": dict(totals=dict(net=1000, total_in=2000, total_out=1000, account_count=1)),
                      "/accounts": dict(accounts=[]), "/reviews": dict(reviews=[]), "/payroll": dict(statements=[]),
                      "/employees/join-requests": dict(requests=[]), "/onboarding/documents": dict(documents=[]),
                      "/contracts/pending-signature": dict(count=0), "/collection-status": dict(statuses=[])}
            return js(bodies[sub])
        return js(dict(detail="synthetic-unrouted"), 404)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="/tmp/obys_u1_e2e")
    out = Path(ap.parse_args().out)
    shots = out / "shots"
    shots.mkdir(parents=True, exist_ok=True)
    if not os.environ.get("PLAYWRIGHT_BROWSERS_PATH") and Path("/root/.cache/ms-playwright").is_dir():
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = "/root/.cache/ms-playwright"
    from playwright.sync_api import sync_playwright

    api = Api(server_manager_routes())
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_static_handler())
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    captured: list[dict] = []
    console: list[str] = []

    def shot(page, name: str) -> None:
        path = shots / f"{name}.png"
        page.screenshot(path=str(path), full_page=False)
        captured.append(dict(name=name, sha256=hashlib.sha256(path.read_bytes()).hexdigest()[:16]))

    def ctx_for(browser, mobile: bool, logged_in: bool = True):
        vp = dict(width=390, height=844) if mobile else dict(width=1440, height=900)
        ctx = browser.new_context(viewport=vp, is_mobile=mobile, has_touch=mobile, service_workers="block")
        ctx.route("**/*", api.respond)
        if logged_in:
            user = json.dumps({"user": {"email": "synthetic@example.test", "name": "합성"}, "permissions": {"role": api.role}})
            ctx.add_init_script("localStorage.setItem('aads_token','synthetic-not-a-credential');"
                                "localStorage.setItem('fb_access_token','synthetic-not-a-credential');"
                                f"localStorage.setItem('yeoljeong-finance-auth-user', {json.dumps(user)});")
        return ctx

    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(args=["--no-sandbox"])
        except Exception as exc:  # noqa: BLE001
            print("BROWSER_UNAVAILABLE", str(exc)[:200])
            return 2

        # ── A. admin.html 역할별 메뉴 ──
        manager = api.manager_routes
        for mobile in (False, True):
            label = "390px" if mobile else "PC"
            for role in ("owner", "admin", "member", "viewer"):
                api.role = role
                api.denied.clear()
                api.calls.clear()
                ctx = ctx_for(browser, mobile)
                page = ctx.new_page()
                page.on("pageerror", lambda e: console.append(f"pageerror {e}"))
                page.goto(f"{base}/static/apps/obys/admin.html#home")
                page.wait_for_selector("[data-route='home']", state="attached", timeout=15_000)
                page.wait_for_timeout(600)
                visible = set(page.eval_on_selector_all("[data-route]", "els=>els.map(e=>e.dataset.route)"))
                expect_hidden = manager if role not in ("owner", "admin") else set()
                if role == "viewer":
                    expect_hidden = manager | {"attendance"}
                check(f"admin {label} {role}: 제한 메뉴 노출이 서버 표와 일치",
                      not (visible & expect_hidden) and ("payroll" in visible) == (role in ("owner", "admin")),
                      f"visible∩hidden={sorted(visible & expect_hidden)}")
                check(f"admin {label} {role}: 첫 진입에서 보이는 메뉴 API 가 403 이 아님", not api.denied, str(api.denied))
                if role in ("member", "viewer"):
                    page.goto(f"{base}/static/apps/obys/admin.html#payroll")
                    page.reload()
                    page.wait_for_selector("[data-route='home']", state="attached", timeout=15_000)
                    page.wait_for_timeout(600)
                    check(f"admin {label} {role}: #payroll 직접 진입은 home 으로 복귀",
                          page.evaluate("location.hash") == "#home", page.evaluate("location.hash"))
                if role == "owner":
                    page.goto(f"{base}/static/apps/obys/admin.html#payroll")
                    page.reload()
                    page.wait_for_selector("[data-route='payroll']", state="attached", timeout=15_000)
                    page.wait_for_function("document.body.innerText.includes('3,000,000원')", timeout=10_000)
                    body = page.inner_text("body")
                    row = next((r for r in page.eval_on_selector_all("table tbody tr", "rs=>rs.map(r=>[...r.cells].map(c=>c.innerText))") if "합성직원" in " ".join(r)), [])
                    heads = page.eval_on_selector_all("table thead th", "hs=>hs.map(h=>h.innerText.trim())")
                    cells = dict(zip(heads, row))
                    check(f"admin {label} owner: 총지급액=원천 gross_pay, 기본급·수당은 미산출",
                          cells.get("총지급액") == "3,000,000원" and cells.get("기본급") == "미산출" and "잘못된 값" not in body,
                          json.dumps(cells, ensure_ascii=False))
                    shot(page, f"admin_payroll_owner_{label}")
                if role == "member":
                    shot(page, f"admin_home_member_{label}")
                elif role == "owner":
                    page.goto(f"{base}/static/apps/obys/admin.html#home")
                    page.reload()
                    page.wait_for_timeout(800)
                    shot(page, f"admin_home_owner_{label}")
                ctx.close()

        # ── B. legacy 오늘 홈 ──
        api.role = "owner"
        entry = f"{base}/static/apps/obys/index.html?legacy=1&view=legacy"
        scenarios = {
            "all-200": {},
            "payroll-404": {"/payroll": 404},
            "reviews-403_sales-500": {"/reviews": 403, "/sales": 500},
            "all-404": {p: 404 for p in ("/sales", "/bank-summary", "/reviews", "/payroll", "/employees/join-requests")},
        }
        for mobile in (False, True):
            label = "390px" if mobile else "PC"
            for name, mode in scenarios.items():
                api.home_mode = mode
                ctx = ctx_for(browser, mobile)
                page = ctx.new_page()
                page.on("pageerror", lambda e: console.append(f"pageerror {e}"))
                page.goto(entry)
                try:
                    page.wait_for_selector("#homeKpiGrid", state="visible", timeout=15_000)
                    page.wait_for_function(
                        "[...document.querySelectorAll('#homeKpiGrid [data-kpi-hint]')].every(e=>e.textContent.trim()!=='불러오는 중')",
                        timeout=15_000)
                    stuck = False
                except Exception:  # noqa: BLE001
                    stuck = True
                visible_home = page.locator("#homeKpiGrid").is_visible()
                texts = page.eval_on_selector_all("#homeKpiGrid [data-home-kpi]", "cs=>cs.map(c=>c.innerText.replace(/\\s+/g,' '))") if visible_home else []
                failed = sum(1 for t in texts if "불러오지 못했습니다" in t)
                want_fail = 0 if name == "all-200" else {"payroll-404": 1, "reviews-403_sales-500": 2, "all-404": 4}[name]
                check(f"legacy {label} {name}: 5지표가 불러오는 중으로 멈추지 않음", visible_home and not stuck, str(texts)[:300])
                check(f"legacy {label} {name}: 실패 카드 {want_fail}개와 복구 버튼", failed == want_fail and
                      (want_fail == 0 or page.locator("#homeKpiGrid .state-actions button", has_text="다시 시도").count() == want_fail),
                      f"failed={failed}")
                if name == "all-404" and visible_home:
                    check(f"legacy {label} {name}: 404 카드에 허용 화면(관리자 화면) 이동 안내",
                          page.locator("#homeKpiGrid .state-actions button", has_text="관리자 화면 열기").count() == 4)
                    shot(page, f"legacy_home_404_{label}")
                if name == "all-200" and visible_home:
                    shot(page, f"legacy_home_ok_{label}")
                ctx.close()

        # 재시도: 404 -> 복구 후 다시 시도하면 카드가 채워진다
        api.home_mode = {"/payroll": 404}
        ctx = ctx_for(browser, False)
        page = ctx.new_page()
        page.goto(entry)
        page.wait_for_selector("#homeKpiGrid .state-actions button", timeout=15_000)
        api.home_mode = {}
        page.locator("#homeKpiGrid .state-actions button", has_text="다시 시도").first.click()
        page.wait_for_function("document.querySelectorAll('#homeKpiGrid .state.error').length===0", timeout=15_000)
        check("legacy PC: 장애 복구 후 '다시 시도'로 카드 정상화", True)
        ctx.close()

        # 미로그인
        api.home_mode = {}
        ctx = ctx_for(browser, False, logged_in=False)
        page = ctx.new_page()
        page.goto(entry)
        page.wait_for_timeout(2500)
        hints = page.eval_on_selector_all("#homeKpiGrid [data-kpi-hint]", "hs=>hs.map(h=>h.textContent.trim())")
        check("legacy PC 미로그인: 지표 힌트가 '불러오는 중'으로 남지 않음(또는 로그인 화면)",
              "불러오는 중" not in hints or not page.locator("#homeKpiGrid").is_visible(), str(hints))
        shot(page, "legacy_logged_out_PC")
        ctx.close()
        browser.close()

    srv.shutdown()
    summary = dict(candidate_only=True, production_claim=False, vault_credential_used=False,
                   passed=sum(r["ok"] for r in RESULTS), failed=[r for r in RESULTS if not r["ok"]],
                   total=len(RESULTS), screenshots=captured, page_errors=console[:20])
    (out / "result.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: summary[k] for k in ("passed", "total")}, ensure_ascii=False))
    return 0 if not summary["failed"] else 1


if __name__ == "__main__":
    sys.exit(main())
