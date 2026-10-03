#!/usr/bin/env python3
"""v2 목업 브라우저 검증. 로그인·네트워크 없음. 실행: PLAYWRIGHT_BROWSERS_PATH=... python3 verify_v2.py"""
import json
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
MOCK = (ROOT / "mockup-v2.html").as_uri()
FIXTURE = "fx-rdoc-chat-01"
checks, errors, requests_seen = [], [], []


def ok(name, cond, extra=""):
    assert cond, f"FAIL {name} {extra}"
    checks.append(name)


def shot(page, name):
    page.screenshot(path=str(HERE / name), full_page=True)


def new_page(browser, vp):
    page = browser.new_page(viewport=vp, device_scale_factor=1)
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.on("request", lambda r: requests_seen.append(r.url) if not r.url.startswith(("file:", "data:", "about:", "blob:")) else None)
    return page


def msgs_text(page):
    return page.locator("#msgs").inner_text()


def run_flow(browser, label, vp, mobile):
    page = new_page(browser, vp)
    page.goto(MOCK)
    shot(page, f"after-{label}-1-initial.png")
    ok(f"{label}:initial_card_has_ids", "A-0001" in msgs_text(page) and "rv-demo-0001" in msgs_text(page) and "sha256:" in msgs_text(page))
    page.locator('[data-act="open"][data-v="1"]').first.click()
    if mobile:
        ok(f"{label}:panel_fullscreen_on_mobile", page.locator("#panel").evaluate("e=>getComputedStyle(e).position")=="fixed" and page.locator("#panel").is_visible())
        ok(f"{label}:back_to_conversation_visible", page.locator("#back").is_visible())
    ok(f"{label}:preview_iframe_sandbox_empty", page.locator("#pbody iframe").get_attribute("sandbox") == "")
    shot(page, f"after-{label}-2-panel-v1.png")
    if mobile:
        page.locator("#back").click()
        ok(f"{label}:back_returns_to_chat", not page.locator("#panel").is_visible())
    page.locator("#send").click()
    ok(f"{label}:empty_request_rejected", "빈 요청은 접수하지 않습니다" in page.locator("#notice").inner_text())
    page.locator("#fillReq").click()
    ok(f"{label}:reply_target_chip", "A-0001" in page.locator("#replyChip").inner_text())
    shot(page, f"after-{label}-3-request-typed.png")
    page.locator("#send").click()
    t = msgs_text(page)
    ok(f"{label}:receipt_names_target_version", "선택한 대상: MR-01 v1" in t and "base_revision 1" in t and "CR-0001" in t)
    ok(f"{label}:request_not_approval_command", "승인·구현 명령이 아닙니다" in t)
    ok(f"{label}:original_preserved_note", "원본 v1 는 그대로 보존" in t)
    ok(f"{label}:editing_disables_approval", page.locator("#apprBtn").is_disabled())
    ok(f"{label}:editing_status_shown", "작성 중" in page.locator("#pstat").inner_text() or page.locator("#panel").is_hidden())
    shot(page, f"after-{label}-4-editing.png")
    page.wait_for_function("document.getElementById('msgs').innerText.includes('변경점 재보고')", timeout=5000)
    ok(f"{label}:rereport_per_request_result", "CR-0001 반영 / 미반영 0건" in msgs_text(page))
    ok(f"{label}:v2_unapproved_no_inheritance", "미승인" in page.locator('[data-v="2"]').first.locator("xpath=ancestor::div[contains(@class,'card')]").inner_text())
    ok(f"{label}:approval_reenabled_after_done", not page.locator("#apprBtn").is_disabled())
    shot(page, f"after-{label}-5-rereport.png")
    # 구버전 카드 승인 시도 → stale 거절
    page.locator('[data-act="open"][data-v="1"]').first.click()
    ok(f"{label}:new_version_banner_in_old_view", "새 버전 v2 있음" in page.locator("#pbody").inner_text())
    page.locator("#apprBtn").click()
    ok(f"{label}:confirm_dialog_names_exact_version", "v1" in page.locator("#dlgB").inner_text() and "A-0001" in page.locator("#dlgB").inner_text())
    page.get_by_role("button", name="확정(시연)").click()
    ok(f"{label}:stale_approval_rejected", "거절 409 stale_revision" in msgs_text(page) and "승인 기록은 만들지 않았습니다" in msgs_text(page))
    shot(page, f"after-{label}-6-stale-rejected.png")
    # 비교
    ok(f"{label}:stale_rejection_visible_in_panel", "stale_revision" in page.locator("#pnotice").inner_text())
    if mobile:
        page.locator('button[data-tab="compare"]').click()
    else:
        page.locator('[data-act="cmp"]').first.click()
    ok(f"{label}:compare_two_frames", page.locator("#pbody iframe").count() == 2)
    shot(page, f"after-{label}-7-compare.png")
    # v2 승인: 저장 실패 → 재시도 → 성공
    page.locator("#ver").select_option("2")
    page.locator('button[data-tab="preview"]').click()
    if mobile:
        page.locator("#back").click()
    page.locator("details.edge summary").click()
    page.locator("#fail").check()
    if mobile:
        page.locator('[data-act="open"][data-v="2"]').first.click()
    page.locator("#apprBtn").click()
    key1 = re.search(r"idempotency_key (ap-\S+)", page.locator("#dlgB").inner_text()).group(1)
    page.get_by_role("button", name="확정(시연)").click()
    ok(f"{label}:save_failure_reported", "저장 실패" in msgs_text(page))
    ok(f"{label}:save_failure_visible_in_panel", "저장 실패" in page.locator("#pnotice").inner_text())
    if mobile:
        page.locator("#back").click()
    page.locator("#fail").uncheck()
    if mobile:
        page.locator('[data-act="open"][data-v="2"]').first.click()
    page.locator("#apprBtn").click()
    key2 = re.search(r"idempotency_key (ap-\S+)", page.locator("#dlgB").inner_text()).group(1)
    ok(f"{label}:retry_reuses_idempotency_key", key1 == key2, f"{key1} {key2}")
    shot(page, f"after-{label}-8-approve-confirm.png")
    page.get_by_role("button", name="확정(시연)").click()
    if mobile:
        page.locator("#back").click()
    t = msgs_text(page)
    ok(f"{label}:demo_approval_disclaimer", "시연 승인 확인" in t and "운영 승인·DB 저장·작업 실행은 일어나지 않았습니다" in t)
    shot(page, f"after-{label}-9-approved-demo.png")
    # 중복 전송
    before = msgs_text(page).count("수정 요청 접수 CR-")
    if mobile:
        pass
    page.locator("#eDup").click()
    t = msgs_text(page)
    ok(f"{label}:duplicate_send_idempotent", "이미 접수된 요청" in t and t.count("수정 요청 접수 CR-") == before)
    # 늦게 온 구버전 결과
    page.locator("#eLate").click()
    ok(f"{label}:late_old_result_not_overwrite", "덮어쓰지 않았습니다" in msgs_text(page))
    # 복수 후보: 입력 보존
    page.locator("#ambig").check()
    page.locator("#draft").fill("다른 화면도 같이 고쳐줘")
    page.locator("#send").click()
    ok(f"{label}:target_select_dialog_once", page.locator("dialog[open]").count() == 1 and "다른 세션" in page.locator("#dlgA").inner_text())
    shot(page, f"after-{label}-10-target-select.png")
    page.get_by_role("button", name="취소").click()
    ok(f"{label}:cancel_preserves_input", page.locator("#draft").input_value() == "다른 화면도 같이 고쳐줘")
    # 재접속: 입력 보존
    page.locator("#eReconn").click()
    page.wait_for_timeout(1200)
    ok(f"{label}:reconnect_preserves_draft", page.locator("#draft").input_value() == "다른 화면도 같이 고쳐줘" and not page.locator("#off").is_visible())
    # overflow
    for w in (1440, 768, 390, 360):
        page.set_viewport_size({"width": w, "height": 900 if w > 600 else 844})
        ok(f"{label}:no_overflow_{w}", page.evaluate("document.documentElement.scrollWidth<=innerWidth"))
    page.close()


def artifact_wrapper(inner):
    esc = lambda v: v.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;").replace("'", "&#39;")
    csp = "default-src 'none'; img-src data: blob:; font-src data:; style-src 'unsafe-inline'; script-src 'none'; connect-src 'none'; form-action 'none'; base-uri 'none'"
    title = "HTML 미리보기"
    return (f'<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<meta http-equiv="Content-Security-Policy" content="{csp}"><title>{title}</title><style>html,body,iframe{{width:100%;height:100%;margin:0;border:0}}body{{background:#fff}}</style></head>'
            f'<body><iframe title="{title}" sandbox="" referrerpolicy="no-referrer" srcdoc="{esc(inner)}"></iframe></body></html>')


def run_artifact(browser):
    inner = (ROOT / "artifact-v2.html").read_text(encoding="utf-8")
    ok("artifact:no_script_tag", "<script" not in inner.lower())
    ok("artifact:under_50000_chars", len(inner) <= 50000, str(len(inner)))
    for label, vp in (("desktop", {"width": 1000, "height": 800}), ("mobile", {"width": 390, "height": 800})):
        page = new_page(browser, vp)
        page.set_content(artifact_wrapper(inner))
        fr = page.frame_locator("iframe")
        ok(f"artifact:{label}:renders_in_panel_wrapper", fr.locator("h2").first.is_visible())
        for i in range(1, 6):
            fr.locator(f'label[for="t{i}"]').click()
            ok(f"artifact:{label}:css_tab_{i}_visible", fr.locator(f".p{i}").is_visible() and all(not fr.locator(f".p{j}").is_visible() for j in range(1, 6) if j != i))
            if label == "desktop" or i == 1:
                page.screenshot(path=str(HERE / f"artifact-panel-{label}-tab{i}.png"))
        fr.locator('label[for="t1"]').click()
        w = page.frame_locator("iframe").locator("body").evaluate("b=>[b.ownerDocument.documentElement.scrollWidth,innerWidth]")
        ok(f"artifact:{label}:no_overflow", w[0] <= w[1], str(w))
        page.close()


def blocked_before(browser):
    out = {}
    for label, vp in (("desktop", {"width": 1440, "height": 900}), ("mobile", {"width": 390, "height": 844})):
        page = new_page(browser, vp)
        page.goto("https://aads.newtalk.kr/chat", wait_until="networkidle", timeout=30000)
        out[label] = {"final_url": page.url, "is_login_page": "/login" in page.url}
        page.screenshot(path=str(HERE / f"blocked-before-login-{label}.png"), full_page=True)
        page.close()
    ok("before:real_chat_requires_login_blocked_evidence", all(v["is_login_page"] for v in out.values()))
    return out


with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    run_flow(browser, "desktop", {"width": 1440, "height": 900}, False)
    run_flow(browser, "mobile", {"width": 390, "height": 844}, True)
    run_artifact(browser)
    ok("no_network_requests_from_mockup_or_artifact", not requests_seen, str(requests_seen[:5]))
    before = blocked_before(browser)
    browser.close()
real_errors = [e for e in errors if "newtalk.kr" not in e and "Failed to load resource" not in e]
ok("no_browser_page_or_console_errors", not real_errors, str(real_errors[:5]))
result = {
    "tested_at_kst": datetime.now(ZoneInfo("Asia/Seoul")).isoformat(),
    "source": "local Playwright Chromium; file URL for mockup; no login; no credentials; no production approval",
    "fixture_id": FIXTURE,
    "viewports": ["1440x900", "390x844"],
    "before_capture": {"status": "blocked_evidence", "reason": "real /chat requires login; credentials not used", "detail": before},
    "checks": checks,
    "console_page_errors": real_errors,
    "result": "pass",
}
(HERE / "verification-v2.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(json.dumps({"result": "pass", "n_checks": len(checks)}, ensure_ascii=False))
