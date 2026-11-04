"""오비서 클로브 자료현황·거래검토·경영요약 UI 배선 정적 검증.

브라우저 없이 확인할 수 있는 계약만 본다 — 화면 동작은 별도 Playwright 캡처로 검증한다.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2] / "app" / "static" / "apps" / "obys"
INDEX = (ROOT / "index.html").read_text(encoding="utf-8")
JS = (ROOT / "modules" / "clobe-collection.js").read_text(encoding="utf-8")
CSS = (ROOT / "modules" / "clobe-collection.css").read_text(encoding="utf-8")
SW = (ROOT / "sw.js").read_text(encoding="utf-8")

VIEWS = ["clobeOverview", "clobeReview", "clobeSummary", "clobeSettings"]


def test_views_are_wired_in_index():
    for view in VIEWS:
        assert f'id="{view}View"' in INDEX
        assert f'data-view="{view}"' in INDEX
        assert f'"{view}"' in INDEX.split("const appViewNames = [", 1)[1].split("];", 1)[0]
    assert "window.obysClobe?.open(currentView)" in INDEX


def test_assets_are_versioned_and_loaded():
    assert re.search(r"modules/clobe-collection\.css\?v=\d{8}-r\d+", INDEX)
    assert re.search(r"modules/clobe-collection\.js\?v=\d{8}-r\d+", INDEX)


def test_existing_ledger_views_are_untouched():
    for view in ("salesLedger", "purchaseLedger", "bankLedger", "cardLedger"):
        assert f'id="{view}View"' in INDEX
    assert "window.obysLedgerDetails?.open(currentView)" in INDEX
    assert "modules/ledger-details.js" in INDEX
    assert "modules/store-assistant-v2.js" in INDEX


def test_employee_experience_does_not_get_clobe_views():
    block = INDEX.split("const employeeViewNames", 1)[1].split("];", 1)[0]
    assert not any(view in block for view in VIEWS)


def test_service_worker_version_bumped_and_api_not_cached():
    assert 'CACHE_VERSION = "obys-clock-shell-20261015-r1"' in SW
    assert "obys-collections" not in SW
    assert "/api/" not in SW.split("APP_SHELL", 1)[1].split("]", 1)[0]


def test_module_uses_only_collection_endpoints_and_no_secrets():
    assert "/api/v1/obys-collections" in JS
    assert "/api/v1/integrations/clobe" in JS
    for forbidden in ("ANTHROPIC", "sk-ant", "api_key", "apikey", "mcp_token", "Bearer sk"):
        assert forbidden.lower() not in JS.lower()


def test_module_does_not_leak_internal_identifiers_into_copy():
    visible = re.findall(r'"([^"\n]*[가-힣][^"\n]*)"', JS)
    joined = "\n".join(visible)
    for token in ("run_id", "job_id", "pipeline", "runner-", "lease_", "model_"):
        assert token not in joined


def test_summary_never_adds_cash_card_and_uses_integer_cents():
    assert "Math.round(number * 100)" in JS
    assert "total.cash += 1; return;" in JS
    assert "total.card += 1; return;" in JS


def test_confirmation_flow_guards_confirm_and_mismatch():
    assert "되돌릴 수 없습니다" in JS
    assert "state.ackMismatch" in JS
    assert 'state.pending?.scope === "one"' in JS
    assert 'state.pending?.scope === "bulk"' in JS


def test_every_failure_class_has_recovery_copy():
    for marker in ("reauth_required", "collection_in_progress", "role_not_allowed",
                   "tenant_not_in_collection_scope", "company_not_linked", "network"):
        assert marker in JS
    for kind in ("session", "expired", "network", "none", "nodata", "permission"):
        assert f'data-clobe-failure="{kind}"' in JS or f'kind: "{kind}"' in JS


def test_drawer_is_above_mobile_dock_and_touch_targets_are_44px():
    assert "z-index: 1900" in CSS
    assert "min-height: 44px" in CSS
    assert "max-width: 820px" in CSS


def test_kind_card_renders_server_next_action_with_44px_buttons():
    assert "function nextActionHtml(k, manage)" in JS
    assert "nextActionHtml(k, manage)" in JS.split("function kindCard", 1)[1].split("function nextActionHtml", 1)[0]
    body = JS.split("function nextActionHtml", 1)[1].split("function runPanel", 1)[0]
    for marker in ("na.text", "na.buttons", "na.button", "na.view", "다시 시도", "다시 연결 보기", "원장 업로드로 이동", "upload_file", "data-next-action"):
        assert marker in body
    assert 'data-view="clobeSettings"' in body
    assert 'data-view="documents"' not in body  # 경영자료·보관 화면에는 CSV 업로드 입력란이 없다
    assert 'data-view="${esc(na.view)}"' in body
    assert 'data-clobe-action="go-upload"' in body and "data-business-id" in body
    block = CSS.split(".clobe-next-action button", 1)[1].split("}", 1)[0]
    assert "min-height: 44px" in block


def test_next_action_copy_does_not_show_internal_identifiers():
    body = JS.split("function nextActionHtml", 1)[1].split("function runPanel", 1)[0]
    visible = re.findall(r'"([^"\n]*[가-힣][^"\n]*)"', body)
    assert visible
    assert not any(re.search(r"[a-z]+_[a-z]+", text) for text in visible)
    assert "esc(na.text)" in body and "${na.text}" not in body


def test_upload_file_target_views_really_have_a_csv_upload_panel():
    from app.services import obys_collection_core as core

    for kind, view in core.UPLOAD_VIEW_BY_KIND.items():
        section = INDEX.split(f'<section id="{view}View"', 1)[1].split("</section>\n      <section id=", 1)[0]
        upload = section.split("data-ledger-upload=", 1)[1]
        assert "data-upload-file" in upload and ".csv" in upload.split("data-upload-file", 1)[0]
        assert f'data-view="{view}"' in INDEX


HOST_FUNCTIONS = ("ledgerUploadScope", "renderLedgerUploadTarget", "syncLedgerUploadTargets", "loadLedgerUploads", "submitLedgerUpload")


def _host_function(name: str) -> str:
    """index.html 의 4칸 들여쓴 함수 하나를 소스 그대로 잘라 낸다(끝은 첫 `    }` 줄)."""
    lines = INDEX.splitlines()
    start = next(i for i, line in enumerate(lines) if re.match(rf"    (async )?function {name}\(", line))
    end = next(i for i in range(start, len(lines)) if lines[i] == "    }")
    return "\n".join(lines[start:end + 1])


NODE_HARNESS = r"""
const toasts = [];
const requests = [];
const store = {};
const tabClicks = [];
const mk = () => ({ dataset: {}, disabled: false, textContent: "", innerHTML: "", classList: { toggle() {}, add() {}, remove() {} }, setAttribute() {} });
const roots = {};
["clobeOverview", "clobeReview", "clobeSummary", "clobeSettings"].forEach(v => {
  const r = mk(); r.handlers = {};
  r.addEventListener = (t, fn) => { r.handlers[t] = fn; };
  r.querySelector = () => null; r.querySelectorAll = () => [];
  roots[v + "View"] = r;
});
function makePanel(category, viewId) {
  const panel = mk();
  panel.dataset.ledgerUpload = category;
  const parts = { submit: mk(), file: mk(), rows: mk(), actions: mk(), state: mk() };
  let chosen = [new File(["a,b1,2"], category + ".csv")];
  const pick = () => { chosen = [new File(["a,b1,2"], category + ".csv")]; };
  panel.pick = pick;
  Object.defineProperty(parts.file, "files", { get: () => chosen });
  Object.defineProperty(parts.file, "value", { get: () => (chosen.length ? "C:\\fakepath\\x.csv" : ""), set: v => { if (v === "") chosen = []; } });
  parts.actions.insertAdjacentElement = (_where, node) => { panel.note = node; node.remove = () => { panel.note = null; }; };
  panel.note = null;
  panel.closest = () => ({ id: viewId });
  panel.querySelector = sel => ({
    "[data-upload-submit]": parts.submit, "[data-upload-file]": parts.file, "[data-upload-rows]": parts.rows,
    ".v2-actions": parts.actions, "[data-upload-state]": parts.state, "[data-upload-target]": panel.note
  }[sel] || null);
  panel.parts = parts;
  return panel;
}
const panels = { transaction: makePanel("transaction", "accountsView"), sales: makePanel("sales", "salesView") };
globalThis.window = { addEventListener() {}, open() {}, confirm() { return true; } };
globalThis.localStorage = { getItem: k => (k === "aads_token" ? "tok" : k === "yeoljeong-finance-auth-user" ? JSON.stringify({ permissions: { role: "owner" } }) : null), setItem() {} };
globalThis.sessionStorage = { getItem: k => store[k] || null, setItem: (k, v) => { store[k] = v; } };
let currentView = "clobeOverview";
globalThis.document = {
  readyState: "complete", cookie: "", activeElement: null,
  addEventListener() {},
  getElementById: id => roots[id] || null,
  createElement: () => mk(),
  querySelector: sel => {
    const m = sel.match(/data-view="(\w+)"/);
    if (m) return { click() { tabClicks.push(m[1]); currentView = m[1]; syncLedgerUploadTargets(); } };
    const c = sel.match(/data-ledger-upload="(\w+)"/);
    return c ? panels[c[1]] || null : null;
  },
  querySelectorAll: sel => (sel === "[data-ledger-upload]" ? Object.values(panels) : [])
};
globalThis.setTimeout = () => 0;
const STATUS = { auth: {}, companies: [
  { clobe_company_id: "c-A", business_id: "biz-A", company_name: "가게A", link_status: "linked", kinds: {} },
  { clobe_company_id: "c-B", business_id: "biz-B", company_name: "가게B", link_status: "linked",
    kinds: { bank_transaction: { status: "failed", last_error_code: "source_scrape_error", consecutive_failures: 2,
      next_action: { key: "upload_file", text: "CSV를 올리세요", button: "upload_file", buttons: ["upload_file"], view: "accounts" } } } }
] };
globalThis.fetch = async (url, opts = {}) => {
  requests.push({ url: String(url), method: opts.method || "GET", body: opts.body });
  const payload = String(url).endsWith("/status") ? STATUS : { runs: [], uploads: [], rows: [] };
  return { ok: true, status: 200, json: async () => payload };
};
sessionStorage.setItem("obys_clobe_ui_v1", JSON.stringify({ companyId: "c-B" }));
const SETTINGS_SELECTED = "biz-A";
const selectedSettingsBusinessId = () => SETTINGS_SELECTED;
const businessName = id => ({ "biz-A": "가게A", "biz-B": "가게B" }[id] || "미지정");
const ownerBusinessApisAllowed = () => true;
const financeApi = async path => { requests.push({ url: "finance" + path, method: "GET" }); return globalThis.financeHook ? globalThis.financeHook(path) : { uploads: [], rows: [] }; };
const fillViewState = () => {};
const escapeHtml = v => String(v);
const apiAuthOnlyHeaders = () => ({});
const showToast = msg => toasts.push(msg);
//CLOBE_JS
//HOST_JS
const posts = () => requests.filter(r => r.method === "POST" && r.url.endsWith("/uploads")).map(r => r.body.get("business_id"));
const listed = () => requests.filter(r => r.url.startsWith("finance/uploads?")).map(r => decodeURIComponent(r.url.match(/business_id=([^&]+)/)[1]));
const click = async (dataset) => { await roots.clobeOverviewView.handlers.click({ target: { closest: () => ({ dataset, disabled: false }) } }); };
(async () => {
  const out = {};
  // 일반 경로: 수집 화면을 거치지 않으면 설정 선택값(A)으로 올린다.
  await submitLedgerUpload(panels.transaction);
  out.plainPost = posts().slice();
  // 설정 A · 수집 B 에서 실제 렌더된 버튼을 눌렀다.
  await window.obysClobe.open("clobeOverview");
  const html = roots.clobeOverviewView.innerHTML;
  const m = html.match(/<button[^>]*data-clobe-action="go-upload"[^>]*>/);
  out.buttonTag = m ? m[0] : "";
  const attr = n => (out.buttonTag.match(new RegExp(n + '="([^"]*)"')) || [])[1] || "";
  await click({ clobeAction: "go-upload", view: attr("data-view"), businessId: attr("data-business-id"), companyName: attr("data-company-name") });
  out.tabClicks = tabClicks.slice();
  out.note = panels.transaction.note && panels.transaction.note.textContent;
  out.salesNote = panels.sales.note && panels.sales.note.textContent;
  await new Promise(r => setImmediate(r));
  out.listedWhilePinned = listed().slice();
  out.fileClearedOnPin = panels.transaction.parts.file.files.length;
  panels.transaction.pick();
  await submitLedgerUpload(panels.transaction);
  out.pinnedPost = posts().slice(1);
  // 다른 화면으로 가면 해제되고 일반 경로로 돌아온다.
  currentView = "dashboard"; syncLedgerUploadTargets();
  out.noteAfterLeave = panels.transaction.note;
  // 고정 중 고른 파일이 해제 뒤에 남아 설정 회사(A) 원장으로 올라가면 안 된다.
  out.fileKeptAfterLeave = panels.transaction.parts.file.files.length;
  const beforeStale = posts().length;
  toasts.length = 0;
  await submitLedgerUpload(panels.transaction);
  out.staleFilePosts = posts().length - beforeStale;
  out.staleFileToast = toasts.slice();
  panels.transaction.pick();
  await submitLedgerUpload(panels.transaction);
  out.afterLeavePost = posts().slice(2);
  // 대상 미확정(business_id 없음): 막고 안내한다.
  const before = posts().length;
  await click({ clobeAction: "go-upload", view: "accounts", businessId: "", companyName: "" });
  out.blockedNote = panels.transaction.note && panels.transaction.note.textContent;
  out.blockedDisabled = [panels.transaction.parts.submit.disabled, panels.transaction.parts.file.disabled];
  toasts.length = 0;
  await submitLedgerUpload(panels.transaction);
  out.blockedPosts = posts().length - before;
  out.blockedToast = toasts.slice();
  console.log(JSON.stringify(out));
})().catch(e => { console.error(e); process.exit(1); });
"""


RACE_SCENARIO = r"""(async () => {
  const out = {};
  const deferred = {};
  const files = name => ({ uploads: [{ id: "id-" + name, original_filename: name + ".csv", status: "ok" }], rows: [] });
  const bizOf = path => decodeURIComponent(path.match(/business_id=([^&]+)/)[1]);
  globalThis.financeHook = path => {
    const biz = bizOf(path);
    if (deferred[biz]) return new Promise(resolve => { deferred[biz].push(() => resolve(files(biz))); });
    return files(biz);
  };
  const flush = () => new Promise(r => setImmediate(r));
  const rows = () => panels.transaction.parts.rows.innerHTML;
  // 1) 고정 해제 중 시작된 설정 회사(A) 조회가 B 고정 뒤에 늦게 끝난다.
  deferred["biz-A"] = [];
  const lateA = loadLedgerUploads("transaction");
  await flush();
  await window.obysClobe.open("clobeOverview");
  const m = roots.clobeOverviewView.innerHTML.match(/<button[^>]*data-clobe-action="go-upload"[^>]*>/)[0];
  const attr = n => (m.match(new RegExp(n + '="([^"]*)"')) || [])[1] || "";
  await roots.clobeOverviewView.handlers.click({ target: { closest: () => ({ dataset: { clobeAction: "go-upload", view: attr("data-view"), businessId: attr("data-business-id"), companyName: attr("data-company-name") }, disabled: false }) } });
  await flush();
  out.pinnedRows = rows();
  deferred["biz-A"].forEach(fn => fn());
  await lateA;
  out.afterLateA = rows();
  // 2) B 고정 중 시작된 조회가 고정 해제 뒤에 늦게 끝난다.
  delete deferred["biz-A"];
  deferred["biz-B"] = [];
  const lateB = loadLedgerUploads("transaction");
  await flush();
  currentView = "dashboard"; syncLedgerUploadTargets();
  await flush();
  out.unpinnedRows = rows();
  deferred["biz-B"].forEach(fn => fn());
  await lateB;
  out.afterLateB = rows();
  console.log(JSON.stringify(out));
})().catch(e => { console.error(e); process.exit(1); });
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node 필요")
def test_stale_upload_list_never_renders_under_another_target_company(tmp_path):
    prelude = NODE_HARNESS.split("(async () => {\n  const out = {};", 1)[0]
    script = (prelude + RACE_SCENARIO).replace("//CLOBE_JS", JS).replace("//HOST_JS", "\n".join(_host_function(n) for n in HOST_FUNCTIONS))
    path = tmp_path / "race.js"
    path.write_text(script, encoding="utf-8")
    run = subprocess.run(["node", str(path)], capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, run.stderr
    out = json.loads(run.stdout.strip().splitlines()[-1])
    assert "biz-B.csv" in out["pinnedRows"] and "biz-A.csv" not in out["pinnedRows"]
    assert out["afterLateA"] == out["pinnedRows"]  # 늦게 도착한 A 응답은 버린다
    assert "biz-A.csv" in out["unpinnedRows"] and "biz-B.csv" not in out["unpinnedRows"]
    assert out["afterLateB"] == out["unpinnedRows"]  # 늦게 도착한 B 응답은 버린다


@pytest.mark.skipif(shutil.which("node") is None, reason="node 필요")
def test_collection_failure_upload_goes_only_to_the_company_being_viewed(tmp_path):
    script = NODE_HARNESS.replace("//CLOBE_JS", JS).replace("//HOST_JS", "\n".join(_host_function(n) for n in HOST_FUNCTIONS))
    path = tmp_path / "harness.js"
    path.write_text(script, encoding="utf-8")
    run = subprocess.run(["node", str(path)], capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, run.stderr
    out = json.loads(run.stdout.strip().splitlines()[-1])
    assert out["plainPost"] == ["biz-A"]  # 일반 업로드 경로는 그대로 설정 선택값
    assert 'data-business-id="biz-B"' in out["buttonTag"]
    assert out["tabClicks"] == ["accounts"]
    assert out["note"] == "대상 회사: 가게B"
    assert out["salesNote"] is None  # 다른 업로드 패널은 건드리지 않는다
    assert out["listedWhilePinned"][-1] == "biz-B"
    assert out["fileClearedOnPin"] == 0  # 고정 시점에 이전에 고른 파일은 비운다
    assert out["pinnedPost"] == ["biz-B"]  # 설정은 A 지만 B 로 전송
    assert out["noteAfterLeave"] is None
    assert out["fileKeptAfterLeave"] == 0  # 고정 해제 시 파일 선택도 비운다
    assert out["staleFilePosts"] == 0 and out["staleFileToast"] == ["사업자와 원장 파일을 선택하십시오."]
    assert out["afterLeavePost"] == ["biz-A"]  # 해제 뒤 새로 고른 파일은 일반 경로(설정 선택값)
    assert out["blockedNote"] == "대상 회사를 먼저 선택하세요"
    assert out["blockedDisabled"] == [True, True]
    assert out["blockedPosts"] == 0
    assert out["blockedToast"] == ["대상 회사를 먼저 선택하세요."]


def test_host_upload_panel_uses_pinned_target_and_releases_on_view_change():
    assert "window.obysClobe?.uploadTargetFor?.(view)" in INDEX
    assert "syncLedgerUploadTargets();" in INDEX.split("function setView(view)", 1)[1].split("saveUiState();", 1)[0]
    submit = _host_function("submitLedgerUpload")
    assert "ledgerUploadScope(panel)" in submit and "selectedSettingsBusinessId()" not in submit
    assert "uploadTargetFor" in JS and "releaseUploadTarget" in JS
