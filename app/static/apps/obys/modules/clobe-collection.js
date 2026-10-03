(function () {
  "use strict";

  const API = "/api/v1/obys-collections";
  const INTEGRATION_API = "/api/v1/integrations/clobe";
  const UI_KEY = "obys_clobe_ui_v1";
  const AUTH_USER_KEY = "yeoljeong-finance-auth-user";
  const ITEM_PAGE = 500;
  const ITEM_CAP = 5000;
  const STALE_HOURS = 48;

  const VIEWS = {
    clobeOverview: { eyebrow: "DATA STATUS", title: "자료현황", description: "은행·세금계산서·현금영수증·카드 자료의 수집 상태와 처리할 일을 한곳에서 확인합니다." },
    clobeReview: { eyebrow: "REVIEW BOX", title: "거래 검토함", description: "수집된 거래·세금계산서·증빙을 상세 확인하고 대사한 뒤 검토·확정합니다." },
    clobeSummary: { eyebrow: "MANAGEMENT SUMMARY", title: "경영요약", description: "확정된 원장 자료만 집계한 자금·매출·매입 요약입니다." },
    clobeSettings: { eyebrow: "CONNECTION", title: "자료 연결 설정", description: "외부 자료 연결 상태, 회사 연결 승인과 권한 확인을 관리합니다." }
  };
  const KINDS = {
    bank_transaction: "은행 거래",
    tax_invoice: "세금계산서",
    cash_receipt: "현금영수증",
    card_approval: "카드 승인"
  };
  const STAGES = {
    review_box: "검토 대기",
    reviewed: "검토 완료",
    confirmed: "확정",
    rejected: "반려",
    superseded: "수정본으로 대체"
  };
  const DIRECTIONS = { IN: "입금", OUT: "출금", SALES: "매출", PURCHASE: "매입" };
  const RUN_STATUS = {
    succeeded: "수집 완료", partial: "부분 수신", failed: "수집 실패", needs_reauth: "재연결 필요",
    lease_lost: "중단됨", running: "수집 중", never_run: "수집 전"
  };
  const READ_ROLES = ["owner", "admin", "member", "viewer"];
  const REVIEW_ROLES = ["owner", "admin", "member"];
  const MANAGE_ROLES = ["owner", "admin"];

  const state = {
    view: "",
    status: null,
    statusError: null,
    statusLoading: false,
    runs: {},
    runsError: null,
    runBusy: "",
    runResult: null,
    items: [],
    itemsKey: "",
    itemsTotal: 0,
    itemsLoading: false,
    itemsError: null,
    selected: new Set(),
    detailId: "",
    pending: null,
    ackMismatch: false,
    notice: null,
    summaryItems: [],
    summaryKey: "",
    summaryTotal: 0,
    summaryLoading: false,
    summaryError: null,
    admin: { status: null, error: null, busy: "", message: "", loaded: false },
    internalAdmin: false,
    navigated: false,
    ui: loadUi()
  };

  function loadUi() {
    try {
      const saved = JSON.parse(sessionStorage.getItem(UI_KEY) || "{}") || {};
      return Object.assign({ companyId: "", kind: "", stage: "review_box", from: "", to: "", institution: "", direction: "", q: "", summaryFrom: "", summaryTo: "", runFrom: "", runTo: "" }, saved);
    } catch (_) {
      return { companyId: "", kind: "", stage: "review_box", from: "", to: "", institution: "", direction: "", q: "", summaryFrom: "", summaryTo: "", runFrom: "", runTo: "" };
    }
  }

  function saveUi() {
    try { sessionStorage.setItem(UI_KEY, JSON.stringify(state.ui)); } catch (_) { /* 저장 불가 환경은 화면 상태만 유지 */ }
  }

  function esc(value) {
    return String(value ?? "").replace(/[&<>"']/g, ch => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[ch]);
  }

  // ── 인증 · 요청 ─────────────────────────────────────────
  function readCookie(name) {
    return document.cookie.split(";").map(item => item.trim()).find(item => item.startsWith(`${name}=`))?.slice(name.length + 1) || "";
  }

  function token() {
    return localStorage.getItem("aads_token") || localStorage.getItem("fb_access_token") || readCookie("aads_token") || readCookie("fb_access_token");
  }

  function sessionUser() {
    try { return JSON.parse(localStorage.getItem(AUTH_USER_KEY) || "null") || {}; } catch (_) { return {}; }
  }

  function role() {
    return String(sessionUser().permissions?.role || "").trim().toLowerCase();
  }

  function can(level) {
    if (!token()) return false;
    if (level === "manage") return state.internalAdmin || MANAGE_ROLES.includes(role());
    if (level === "review") return state.internalAdmin || REVIEW_ROLES.includes(role());
    return state.internalAdmin || READ_ROLES.includes(role());
  }

  async function request(path, options = {}) {
    const { method = "GET", body, timeout = 20000, base = API } = options;
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeout);
    try {
      const response = await fetch(`${base}${path}`, {
        method,
        headers: { "Content-Type": "application/json", ...(token() ? { Authorization: `Bearer ${token()}` } : {}) },
        body: body === undefined ? undefined : JSON.stringify(body),
        signal: controller.signal
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) {
        const detail = payload.detail;
        const error = new Error("http");
        error.status = response.status;
        error.code = typeof detail === "string" ? detail : String(detail?.code || detail?.message || "");
        throw error;
      }
      return payload;
    } catch (error) {
      if (error.status) throw error;
      const wrapped = new Error("network");
      wrapped.network = true;
      wrapped.timeout = error.name === "AbortError";
      throw wrapped;
    } finally {
      clearTimeout(timer);
    }
  }

  // ── 오류 설명: 원인 → 복구 행동 ─────────────────────────
  function explain(error) {
    const code = String(error?.code || "");
    const status = Number(error?.status || 0);
    if (error?.network) {
      return error.timeout
        ? { kind: "network", title: "서버 응답이 지연되고 있습니다", body: "요청은 서버에서 계속 처리 중일 수 있습니다. 잠시 후 새로고침해 결과를 확인하십시오.", actions: ["retry"] }
        : { kind: "network", title: "네트워크에 연결할 수 없습니다", body: "인터넷 연결을 확인한 뒤 다시 시도하십시오. 입력한 조건은 그대로 유지됩니다.", actions: ["retry"] };
    }
    if (status === 401) return { kind: "session", title: "로그인 세션이 만료되었습니다", body: "다시 로그인하면 보던 회사와 조회 조건 그대로 이어서 볼 수 있습니다.", actions: ["login"] };
    if (status === 409 && code === "reauth_required") {
      return { kind: "expired", title: "자료 연결 승인이 만료되었습니다", body: state.internalAdmin ? "연결 설정에서 승인을 다시 받아야 수집할 수 있습니다. 이미 받은 자료는 그대로 볼 수 있습니다." : "관리자가 연결 승인을 다시 받아야 합니다. 이미 받은 자료는 그대로 볼 수 있습니다. 관리자에게 재승인을 요청하십시오.", actions: state.internalAdmin ? ["settings"] : [] };
    }
    if (status === 409 && code === "collection_in_progress") return { kind: "busy", title: "이미 수집이 진행 중입니다", body: "같은 회사의 다른 수집이 끝난 뒤 다시 실행할 수 있습니다. 잠시 후 현황을 새로고침하십시오.", actions: ["retry"] };
    if (status === 409 && code === "page_limit") return { kind: "period", title: "기간이 길어 한 번에 받을 수 없습니다", body: "수집 기간을 나누어 다시 실행하십시오.", actions: [] };
    if (status === 400 && (code === "invalid_period" || code === "period_too_long")) return { kind: "period", title: code === "period_too_long" ? "수집 기간이 너무 깁니다" : "수집 기간이 올바르지 않습니다", body: "기간은 시작일이 종료일보다 앞서야 하며 최대 1년입니다.", actions: [] };
    if (status === 403 && code === "role_not_allowed") return { kind: "role", title: "이 작업을 할 권한이 없습니다", body: "현재 계정의 역할로는 사용할 수 없는 기능입니다. 필요하면 대표·관리자에게 권한을 요청하십시오.", actions: [] };
    if (status === 403 && code === "tenant_not_in_collection_scope") return { kind: "scope", title: "이 사업장은 자료 수집 대상이 아닙니다", body: "자료 수집은 승인된 사업장에서만 쓸 수 있습니다. 대상 확대는 관리자에게 요청하십시오.", actions: [] };
    if (status === 403 && (code === "tool_denied" || code === "permission_denied")) return { kind: "permission", title: "외부 자료 조회 권한이 없습니다", body: "이 회사의 자료 조회가 허용되지 않았습니다. 관리자가 연결 설정에서 권한을 다시 확인해야 합니다.", actions: state.internalAdmin ? ["settings"] : [] };
    if (status === 403 && code === "company_blocked") return { kind: "permission", title: "수집 대상에서 제외된 회사입니다", body: "이 회사는 자료 수집이 차단되어 있습니다.", actions: [] };
    if (status === 403) return { kind: "role", title: "접근 권한이 없습니다", body: "관리자 전용 기능이거나 현재 계정에 권한이 없습니다.", actions: [] };
    if (status === 404 || (status === 409 && code === "company_not_linked")) return { kind: "none", title: "연결된 회사 또는 사업자가 없습니다", body: "회사 연결이 아직 승인되지 않았거나 선택한 회사를 찾을 수 없습니다. 관리자에게 연결 승인을 요청하십시오.", actions: state.internalAdmin ? ["settings"] : [] };
    if (status === 503) return { kind: "network", title: "외부 자료 서비스가 일시적으로 응답하지 않습니다", body: "잠시 후 다시 시도하십시오. 이미 받은 자료에는 영향이 없습니다.", actions: ["retry"] };
    return { kind: "error", title: "요청을 처리하지 못했습니다", body: "잠시 후 다시 시도하십시오. 계속되면 관리자에게 문의하십시오.", actions: ["retry"] };
  }

  function codeText(code) {
    const text = String(code || "");
    if (!text) return "";
    if (text === "reauth_required") return "자료 연결 승인이 만료되었습니다.";
    if (text === "source_scrape_error") return "원천 기관 중 일부 자료를 아직 가져오지 못했습니다.";
    if (text === "count_mismatch") return "받은 건수가 원천 보고 건수와 달라 다시 확인이 필요합니다.";
    if (text.startsWith("sum_mismatch")) return "받은 합계가 원천 보고 합계와 달라 다시 확인이 필요합니다.";
    if (text.startsWith("transient")) return "일시적인 연결 문제로 일부만 받았습니다.";
    if (text.startsWith("clobe")) return "외부 자료 서비스 응답 오류로 수집하지 못했습니다.";
    if (text === "lease_lost") return "다른 수집과 겹쳐 중단되었습니다.";
    if (text === "permission_denied") return "외부 자료 조회 권한이 없습니다.";
    if (text === "page_limit") return "기간이 길어 한 번에 받지 못했습니다.";
    return "수집 중 문제가 발생했습니다. 원인을 확인하고 있습니다.";
  }

  function failureHtml(error, retryAction) {
    const info = explain(error);
    const buttons = info.actions.map(action => {
      if (action === "retry") return `<button type="button" class="primary" data-clobe-action="${esc(retryAction || "refresh")}">다시 시도</button>`;
      if (action === "login") return `<button type="button" class="primary" data-clobe-action="login">다시 로그인</button>`;
      if (action === "settings") return `<button type="button" class="primary" data-clobe-action="go" data-view="clobeSettings">연결 설정으로 이동</button>`;
      return "";
    }).join("");
    return `<div class="clobe-state error" role="alert" data-clobe-failure="${esc(info.kind)}"><strong>${esc(info.title)}</strong><span>${esc(info.body)}</span>${buttons ? `<div class="clobe-state-actions">${buttons}</div>` : ""}</div>`;
  }

  // ── 형식 ────────────────────────────────────────────────
  function fmtDateTime(value) {
    if (!value) return "";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return String(value).slice(0, 16).replace("T", " ");
    return new Intl.DateTimeFormat("sv-SE", { timeZone: "Asia/Seoul", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false }).format(date);
  }

  function fmtDate(value) {
    return value ? String(value).slice(0, 10) : "";
  }

  function cents(value) {
    const number = Number(value);
    return Number.isFinite(number) ? Math.round(number * 100) : null;
  }

  function won(centsValue) {
    return `${(centsValue / 100).toLocaleString("ko-KR", { maximumFractionDigits: 2 })}원`;
  }

  function amountText(value) {
    const c = cents(value);
    return c === null ? "-" : won(c);
  }

  function todayText() {
    return new Intl.DateTimeFormat("sv-SE", { timeZone: "Asia/Seoul" }).format(new Date());
  }

  function shiftDays(dateText, days) {
    const date = new Date(`${dateText}T00:00:00Z`);
    date.setUTCDate(date.getUTCDate() + days);
    return date.toISOString().slice(0, 10);
  }

  // ── 상태 로딩 ───────────────────────────────────────────
  function companies() {
    return (state.status?.companies || []).filter(item => item.link_status === "linked" && item.business_id);
  }

  function selectedCompany() {
    const list = companies();
    return list.find(item => item.clobe_company_id === state.ui.companyId) || list[0] || null;
  }

  function applyAuthInfo() {
    state.internalAdmin = Boolean(state.status?.auth?.reauth_endpoint);
  }

  async function loadStatus() {
    state.statusLoading = true;
    state.statusError = null;
    renderCurrent();
    try {
      state.status = await request("/status");
      applyAuthInfo();
      const company = selectedCompany();
      if (company && company.clobe_company_id !== state.ui.companyId) {
        state.ui.companyId = company.clobe_company_id;
        saveUi();
      }
    } catch (error) {
      state.statusError = error;
    } finally {
      state.statusLoading = false;
    }
    renderCurrent();
  }

  async function loadRuns() {
    const company = selectedCompany();
    if (!company) return;
    try {
      const payload = await request(`/companies/${encodeURIComponent(company.clobe_company_id)}/runs?limit=20`);
      state.runs[company.clobe_company_id] = payload.runs || [];
      state.runsError = null;
    } catch (error) {
      state.runsError = error;
    }
    renderCurrent();
  }

  function itemsPath(business, stage, kind, offset) {
    const params = new URLSearchParams({ limit: String(ITEM_PAGE), offset: String(offset) });
    if (stage) params.set("stage", stage);
    if (kind) params.set("kind", kind);
    return `/businesses/${encodeURIComponent(business)}/items?${params}`;
  }

  async function fetchAllItems(business, stage, kind) {
    let offset = 0;
    let total = 0;
    const all = [];
    while (offset < ITEM_CAP) {
      const payload = await request(itemsPath(business, stage, kind, offset));
      total = Number(payload.total || 0);
      all.push(...(payload.items || []));
      offset += ITEM_PAGE;
      if (all.length >= total || !(payload.items || []).length) break;
    }
    return { items: all, total };
  }

  async function loadItems(force) {
    const company = selectedCompany();
    if (!company) return;
    const key = `${company.business_id}|${state.ui.stage}|${state.ui.kind}`;
    if (!force && key === state.itemsKey && !state.itemsError) return;
    state.itemsLoading = true;
    state.itemsError = null;
    state.selected = new Set();
    renderReviewBody();
    try {
      const out = await fetchAllItems(company.business_id, state.ui.stage, state.ui.kind);
      state.items = out.items;
      state.itemsTotal = out.total;
      state.itemsKey = key;
    } catch (error) {
      state.itemsError = error;
      state.items = [];
      state.itemsTotal = 0;
      state.itemsKey = "";
    } finally {
      state.itemsLoading = false;
    }
    renderReviewBody();
  }

  async function loadSummary(force) {
    const company = selectedCompany();
    if (!company) return;
    const key = company.business_id;
    if (!force && key === state.summaryKey && !state.summaryError) return;
    state.summaryLoading = true;
    state.summaryError = null;
    renderCurrent();
    try {
      const out = await fetchAllItems(company.business_id, "confirmed", "");
      state.summaryItems = out.items;
      state.summaryTotal = out.total;
      state.summaryKey = key;
    } catch (error) {
      state.summaryError = error;
      state.summaryItems = [];
      state.summaryTotal = 0;
      state.summaryKey = "";
    } finally {
      state.summaryLoading = false;
    }
    renderCurrent();
  }

  // ── 공통 화면 조각 ──────────────────────────────────────
  function rootFor(view) {
    return document.getElementById(`${view}View`);
  }

  function frame(view, bodyHtml) {
    const meta = VIEWS[view];
    return `<div class="clobe-shell" data-clobe-view="${esc(view)}">
      <header class="clobe-hero"><div><span class="clobe-eyebrow">${esc(meta.eyebrow)}</span><h1>${esc(meta.title)}</h1><p>${esc(meta.description)}</p></div></header>
      ${state.notice ? `<div class="clobe-notice ${esc(state.notice.tone)}" role="status" aria-live="polite">${esc(state.notice.text)}<button type="button" data-clobe-action="dismiss-notice" aria-label="알림 닫기">닫기</button></div>` : ""}
      ${bodyHtml}
    </div>`;
  }

  function companyBar() {
    const list = companies();
    if (!list.length) return "";
    const current = selectedCompany();
    return `<div class="clobe-toolbar"><label>회사<select data-clobe-company aria-label="회사 선택">${list.map(item => `<option value="${esc(item.clobe_company_id)}"${item.clobe_company_id === current.clobe_company_id ? " selected" : ""}>${esc(item.company_name)}</option>`).join("")}</select></label>
      <span class="clobe-company-meta">${current.reg_no_masked ? `사업자번호 ${esc(current.reg_no_masked)}` : ""}</span>
      <button type="button" data-clobe-action="refresh">새로고침</button></div>`;
  }

  function gateHtml(retryAction) {
    if (!token()) return `<div class="clobe-state error" role="alert" data-clobe-failure="session"><strong>로그인이 필요합니다</strong><span>로그인하면 회사별 자료 현황을 볼 수 있습니다.</span><div class="clobe-state-actions"><button type="button" class="primary" data-clobe-action="login">로그인</button></div></div>`;
    if (state.statusLoading && !state.status) return `<div class="clobe-state loading"><strong>자료 현황을 불러오는 중입니다</strong><span>회사 연결과 권한을 확인하고 있습니다.</span></div>`;
    if (state.statusError) return failureHtml(state.statusError, retryAction);
    if (!companies().length) {
      return `<div class="clobe-state" data-clobe-failure="none"><strong>연결된 회사가 없습니다</strong><span>승인된 회사 연결이 있어야 자료를 볼 수 있습니다. ${state.internalAdmin ? "자료 연결 설정에서 회사 연결을 승인하십시오." : "관리자에게 회사 연결 승인을 요청하십시오."}</span>${state.internalAdmin ? `<div class="clobe-state-actions"><button type="button" class="primary" data-clobe-action="go" data-view="clobeSettings">연결 설정으로 이동</button></div>` : ""}</div>`;
    }
    return "";
  }

  function kindStates(company) {
    return Object.keys(KINDS).map(kind => ({ kind, ...(company.kinds?.[kind] || { status: "never_run" }) }));
  }

  function freshness(company) {
    const kinds = kindStates(company);
    const successTimes = kinds.map(k => k.last_success_at).filter(Boolean).sort();
    const from = kinds.map(k => k.covered_from).filter(Boolean).sort()[0] || "";
    const to = kinds.map(k => k.covered_to).filter(Boolean).sort().slice(-1)[0] || "";
    const counts = kinds.reduce((acc, k) => { acc[k.status] = (acc[k.status] || 0) + 1; return acc; }, {});
    const lastSuccess = successTimes.slice(-1)[0] || "";
    const oldestSuccess = successTimes[0] || "";
    const stale = Boolean(oldestSuccess) && (Date.now() - new Date(oldestSuccess).getTime()) > STALE_HOURS * 3600 * 1000;
    let overall = "mixed";
    if (counts.needs_reauth) overall = "needs_reauth";
    else if (counts.failed) overall = "failed";
    else if (counts.partial) overall = "partial";
    else if (counts.never_run === kinds.length) overall = "never_run";
    else if (counts.succeeded === kinds.length) overall = stale ? "stale" : "succeeded";
    return { overall, lastSuccess, from, to, stale, kinds };
  }

  const OVERALL_LABEL = {
    succeeded: "전 종류 수집 완료", stale: "수집 완료 · 최신 아님", partial: "부분 수신", failed: "수집 오류", needs_reauth: "재연결 필요",
    never_run: "수집 전", mixed: "일부 종류 미수집"
  };

  function badge(status, label) {
    return `<span class="clobe-badge ${esc(status)}">${esc(label || RUN_STATUS[status] || status)}</span>`;
  }

  function stageCounts(company) {
    const counts = company.item_stage_counts || {};
    return { review_box: counts.review_box || 0, reviewed: counts.reviewed || 0, confirmed: counts.confirmed || 0, rejected: counts.rejected || 0 };
  }

  // ── 자료현황 ────────────────────────────────────────────
  function authBanner() {
    const auth = state.status?.auth;
    if (!auth) return "";
    if (auth.reauth_required) {
      return `<div class="clobe-state error" role="alert" data-clobe-failure="expired"><strong>자료 연결 승인이 필요합니다</strong><span>${state.internalAdmin ? "연결 설정에서 승인을 다시 받으면 수집을 재개할 수 있습니다." : "관리자가 연결 승인을 다시 받아야 수집할 수 있습니다. 이미 받은 자료는 그대로 볼 수 있습니다."}</span>${state.internalAdmin ? `<div class="clobe-state-actions"><button type="button" class="primary" data-clobe-action="go" data-view="clobeSettings">연결 설정으로 이동</button></div>` : ""}</div>`;
    }
    return "";
  }

  function todoList(company, fresh) {
    const counts = stageCounts(company);
    const todos = [];
    const manage = can("manage");
    if (state.status?.auth?.reauth_required) {
      todos.push(state.internalAdmin
        ? { tone: "error", text: "연결 승인이 만료되었습니다. 다시 승인하세요.", action: `<button type="button" data-clobe-action="go" data-view="clobeSettings">연결 설정</button>` }
        : { tone: "error", text: "연결 승인이 만료되었습니다. 관리자에게 재승인을 요청하세요." });
    }
    const failed = fresh.kinds.filter(k => ["failed", "partial", "needs_reauth"].includes(k.status));
    failed.forEach(k => todos.push({
      tone: "warn",
      text: `${KINDS[k.kind]} ${RUN_STATUS[k.status]}${k.last_error_code ? ` — ${codeText(k.last_error_code)}` : ""}`,
      action: manage && k.status !== "needs_reauth" ? `<button type="button" data-clobe-action="run-kind" data-kind="${esc(k.kind)}"${state.runBusy ? " disabled" : ""}>재시도</button>` : ""
    }));
    if (counts.review_box && can("review")) todos.push({ tone: "info", text: `검토 대기 ${counts.review_box}건이 있습니다.`, action: `<button type="button" class="primary" data-clobe-action="open-review" data-stage="review_box">검토하러 가기</button>` });
    if (counts.reviewed && manage) todos.push({ tone: "info", text: `확정 대기(검토 완료) ${counts.reviewed}건이 있습니다.`, action: `<button type="button" class="primary" data-clobe-action="open-review" data-stage="reviewed">확정하러 가기</button>` });
    if (fresh.overall === "never_run" && manage) todos.push({ tone: "info", text: "아직 수집한 자료가 없습니다. 아래에서 수집을 실행하세요." });
    if (!todos.length) return `<div class="clobe-todo empty">지금 처리할 일이 없습니다.</div>`;
    return `<ul class="clobe-todo-list">${todos.map(t => `<li class="${esc(t.tone)}"><span>${esc(t.text)}</span>${t.action || ""}</li>`).join("")}</ul>`;
  }

  function kindCard(k, manage) {
    const covered = k.covered_from && k.covered_to ? `${fmtDate(k.covered_from)} ~ ${fmtDate(k.covered_to)}` : "";
    return `<article class="clobe-kind-card" data-kind="${esc(k.kind)}">
      <header><b>${esc(KINDS[k.kind])}</b>${badge(k.status)}</header>
      <dl>
        <div><dt>마지막 수집</dt><dd>${k.last_success_at ? esc(fmtDateTime(k.last_success_at)) : "수집 기록 없음"}</dd></div>
        <div><dt>받은 기간</dt><dd>${covered ? esc(covered) : "-"}</dd></div>
        ${k.source_as_of ? `<div><dt>원천 기준 시각</dt><dd>${esc(fmtDateTime(k.source_as_of))}</dd></div>` : ""}
      </dl>
      ${k.last_error_code && k.status !== "succeeded" ? `<p class="clobe-kind-error">${esc(codeText(k.last_error_code))}${k.last_failure_at ? ` (${esc(fmtDateTime(k.last_failure_at))})` : ""}</p>` : ""}
      ${k.next_retry_at && k.status !== "succeeded" && k.status !== "needs_reauth" ? `<p class="clobe-kind-retry">다음 자동 재시도 ${esc(fmtDateTime(k.next_retry_at))}${k.consecutive_failures ? ` · 연속 실패 ${Number(k.consecutive_failures)}회` : ""}</p>` : ""}
      ${manage && k.status !== "succeeded" && k.status !== "never_run" && k.status !== "needs_reauth" ? `<button type="button" data-clobe-action="run-kind" data-kind="${esc(k.kind)}"${state.runBusy ? " disabled" : ""}>이 종류 다시 수집</button>` : ""}
    </article>`;
  }

  function runPanel(company) {
    if (!can("manage")) return `<section class="clobe-panel"><h2>수집 실행</h2><p class="clobe-muted">수집 실행은 대표·관리자만 할 수 있습니다. 현재 계정은 자료 확인과 검토만 가능합니다.</p></section>`;
    const to = state.ui.runTo || todayText();
    const from = state.ui.runFrom || shiftDays(to, -90);
    return `<section class="clobe-panel"><h2>수집 실행</h2>
      <p class="clobe-muted">기간을 정해 4종류 자료를 한 번에 받습니다. 중단된 수집은 이어서 받고, 같은 자료는 중복으로 쌓이지 않습니다.</p>
      <div class="clobe-run-form">
        <label>시작일<input type="date" data-clobe-run-from value="${esc(from)}"></label>
        <label>종료일<input type="date" data-clobe-run-to value="${esc(to)}"></label>
        <button type="button" class="primary" data-clobe-action="run-all"${state.runBusy ? " disabled" : ""}>${state.runBusy ? "수집 중…" : "수집 실행"}</button>
      </div>
      <div class="clobe-run-result" aria-live="polite">${runResultHtml(company)}</div>
    </section>`;
  }

  function runResultHtml() {
    if (state.runBusy) return `<div class="clobe-state loading"><strong>수집 중입니다</strong><span>자료 양에 따라 수 분이 걸릴 수 있습니다. 이 화면을 닫아도 서버에서 계속 수집합니다.</span></div>`;
    const result = state.runResult;
    if (!result) return "";
    if (result.error) return failureHtml(result.error, "refresh");
    const lines = (result.runs || []).map(run => {
      const c = run.counts || {};
      const extra = run.status === "succeeded"
        ? `수신 ${Number(c.received || 0)}건 · 신규 ${Number(c.inserted || 0)} · 변경 ${Number(c.revised || 0)} · 동일 ${Number(c.unchanged || 0)}`
        : codeText(run.error_code);
      return `<li class="${esc(run.status)}"><b>${esc(KINDS[run.data_kind] || run.data_kind)}</b>${badge(run.status)}<span>${esc(extra)}</span></li>`;
    }).join("");
    const head = result.status === "succeeded" ? "모든 종류를 정상 수집했습니다." : result.status === "needs_reauth" ? "연결 승인이 만료되어 수집을 중단했습니다." : result.status === "lease_lost" ? "다른 수집과 겹쳐 중단되었습니다. 잠시 후 다시 실행하십시오." : result.status === "failed" ? "수집에 실패했습니다." : "일부만 수집했습니다. 실패한 종류는 다시 수집하십시오.";
    return `<div class="clobe-run-summary ${esc(result.status)}"><strong>${esc(head)}</strong>${result.status === "partial" && result.error_code && !lines ? `<span>${esc(codeText(result.error_code))}</span>` : ""}<ul>${lines}</ul></div>`;
  }

  function recentRuns(company) {
    const runs = state.runs[company.clobe_company_id];
    if (state.runsError) return `<section class="clobe-panel"><h2>최근 수집 이력</h2>${failureHtml(state.runsError, "refresh")}</section>`;
    if (!runs) return `<section class="clobe-panel"><h2>최근 수집 이력</h2><div class="clobe-state loading"><strong>이력을 불러오는 중입니다</strong></div></section>`;
    if (!runs.length) return `<section class="clobe-panel"><h2>최근 수집 이력</h2><div class="clobe-state"><strong>수집 이력이 없습니다</strong><span>수집을 실행하면 여기에 기록됩니다.</span></div></section>`;
    return `<section class="clobe-panel"><h2>최근 수집 이력</h2><div class="clobe-run-list">${runs.slice(0, 12).map(run => `<div class="clobe-run-row"><span class="when">${esc(fmtDateTime(run.started_at))}</span><span>${esc(KINDS[run.data_kind] || run.data_kind)}${run.mode === "shadow" ? " (시험)" : ""}</span><span>${esc(fmtDate(run.period?.[0]))} ~ ${esc(fmtDate(run.period?.[1]))}</span>${badge(run.status)}<span class="detail">${run.status === "succeeded" ? `수신 ${Number(run.counts?.received || 0)}건` : esc(codeText(run.error_code))}${run.attempt > 1 ? ` · ${Number(run.attempt)}차 시도` : ""}</span></div>`).join("")}</div></section>`;
  }

  function unsupportedPanel() {
    const list = state.status?.unsupported_sources || [];
    if (!list.length) return "";
    return `<section class="clobe-panel"><h2>파일로 받아야 하는 자료</h2><ul class="clobe-unsupported">${list.map(item => `<li><b>${item.kind === "wetax_tax_payment_certificate" ? "위택스 납부확인서" : esc(item.kind)}</b><span>자동 수집을 지원하지 않습니다. 파일을 직접 올려야 하며, 올리기 전까지는 수집된 것으로 표시하지 않습니다.</span><button type="button" data-clobe-action="go" data-view="documents">경영자료·보관에서 올리기</button></li>`).join("")}</ul></section>`;
  }

  function renderOverview() {
    const gate = gateHtml("refresh");
    if (gate) return frame("clobeOverview", `${gate}`);
    const company = selectedCompany();
    const fresh = freshness(company);
    const counts = stageCounts(company);
    const noStatus = fresh.overall === "never_run";
    return frame("clobeOverview", `
      ${companyBar()}
      ${authBanner()}
      <section class="clobe-panel clobe-todo"><h2>지금 할 일</h2>${todoList(company, fresh)}</section>
      <section class="clobe-freshness ${esc(fresh.overall)}" aria-label="수집 현황">
        <div><small>수집 상태</small><strong>${badge(fresh.overall, OVERALL_LABEL[fresh.overall])}</strong></div>
        <div><small>마지막 수집 시각</small><strong>${fresh.lastSuccess ? esc(fmtDateTime(fresh.lastSuccess)) : "수집 기록 없음"}</strong></div>
        <div><small>받은 기간</small><strong>${fresh.from && fresh.to ? `${esc(fmtDate(fresh.from))} ~ ${esc(fmtDate(fresh.to))}` : "-"}</strong></div>
        ${fresh.stale ? `<p class="clobe-stale">마지막 수집이 ${STALE_HOURS}시간 이상 지났습니다. 최신 자료가 아닐 수 있습니다.</p>` : ""}
        ${fresh.overall === "partial" ? `<p class="clobe-stale">일부 자료만 받았습니다. 합계와 건수가 실제보다 적을 수 있습니다.</p>` : ""}
      </section>
      <div class="clobe-kind-grid">${fresh.kinds.map(k => kindCard(k, can("manage"))).join("")}</div>
      <section class="clobe-panel"><h2>처리 단계별 건수</h2>
        ${noStatus && !counts.review_box && !counts.reviewed && !counts.confirmed ? `<p class="clobe-muted">받은 자료가 아직 없습니다.</p>` : `<div class="clobe-stage-row">
          ${["review_box", "reviewed", "confirmed", "rejected"].map(stage => `<button type="button" class="clobe-stage-chip" data-clobe-action="open-review" data-stage="${stage}"><small>${esc(STAGES[stage])}</small><strong>${Number(counts[stage]).toLocaleString("ko-KR")}건</strong></button>`).join("")}
        </div>`}
      </section>
      ${runPanel(company)}
      ${recentRuns(company)}
      ${unsupportedPanel()}`);
  }

  // ── 거래 검토함 ─────────────────────────────────────────
  function itemDirectionLabel(item) {
    return DIRECTIONS[item.direction] || "구분 없음";
  }

  function visibleItems() {
    const q = state.ui.q.trim().toLowerCase();
    return state.items.filter(item => {
      const day = fmtDate(item.occurred_on);
      if (state.ui.from && (!day || day < state.ui.from)) return false;
      if (state.ui.to && (!day || day > state.ui.to)) return false;
      if (state.ui.institution && item.institution !== state.ui.institution) return false;
      if (state.ui.direction && item.direction !== state.ui.direction) return false;
      if (q && ![item.counterparty, item.institution, item.amount, KINDS[item.data_kind]].some(v => String(v || "").toLowerCase().includes(q))) return false;
      return true;
    });
  }

  function reviewFilters() {
    const institutions = [...new Set(state.items.map(item => item.institution).filter(Boolean))].sort();
    const dirs = [...new Set(state.items.map(item => item.direction).filter(Boolean))];
    const opt = (map, value) => Object.entries(map).map(([k, label]) => `<option value="${esc(k)}"${value === k ? " selected" : ""}>${esc(label)}</option>`).join("");
    return `<form class="clobe-filters" data-clobe-filters onsubmit="return false">
      <label>처리 단계<select data-clobe-filter="stage">${opt(STAGES, state.ui.stage)}</select></label>
      <label>자료 종류<select data-clobe-filter="kind"><option value="">전체</option>${opt(KINDS, state.ui.kind)}</select></label>
      <label>시작일<input type="date" data-clobe-filter="from" value="${esc(state.ui.from)}"></label>
      <label>종료일<input type="date" data-clobe-filter="to" value="${esc(state.ui.to)}"></label>
      <label>기관<select data-clobe-filter="institution"><option value="">전체</option>${institutions.map(name => `<option value="${esc(name)}"${state.ui.institution === name ? " selected" : ""}>${esc(name)}</option>`).join("")}</select></label>
      <label>구분<select data-clobe-filter="direction"><option value="">전체</option>${dirs.map(d => `<option value="${esc(d)}"${state.ui.direction === d ? " selected" : ""}>${esc(DIRECTIONS[d] || d)}</option>`).join("")}</select></label>
      <label class="wide">검색<input type="search" data-clobe-filter="q" value="${esc(state.ui.q)}" placeholder="거래처·기관·금액"></label>
      <button type="button" data-clobe-action="reset-filters">조건 초기화</button>
    </form>`;
  }

  function pendingHtml() {
    const pending = state.pending;
    const label = { approve: "검토 승인", reject: "반려", confirm: "원장 확정" }[pending.kind];
    const warn = pending.kind === "confirm" ? "확정하면 원장에 올라가며 되돌릴 수 없습니다. 이후 수정은 새로 수집된 수정본으로만 반영됩니다." : pending.kind === "reject" ? "반려한 항목은 경영요약에 포함되지 않습니다." : "승인한 항목은 확정 대기로 넘어갑니다.";
    return `<div class="clobe-actionbar confirm" role="alertdialog" aria-label="${esc(label)} 확인"><div><strong>${esc(label)} ${pending.ids.length}건을 진행할까요?</strong><span>${esc(warn)}</span></div>
      <div class="clobe-state-actions"><button type="button" class="primary" data-clobe-action="pending-go"${state.itemsLoading ? " disabled" : ""}>${esc(label)} 진행</button><button type="button" data-clobe-action="pending-cancel">취소</button></div></div>`;
  }

  function actionBar(rows) {
    const selected = rows.filter(item => state.selected.has(item.item_id));
    const stage = state.ui.stage;
    const review = can("review");
    const manage = can("manage");
    const n = state.selected.size;
    if (state.pending?.scope === "bulk") return pendingHtml();
    if (!review) return "";
    const approveOk = stage === "review_box";
    const rejectOk = stage === "review_box" || stage === "reviewed";
    const confirmOk = stage === "reviewed" && manage;
    return `<div class="clobe-actionbar"><span>${n ? `${n}건 선택됨` : "항목을 선택하면 일괄 처리할 수 있습니다."}${selected.length < n ? " (필터에 가려진 선택 포함)" : ""}</span>
      <div class="clobe-state-actions">
        ${approveOk ? `<button type="button" data-clobe-action="bulk" data-kind="approve"${n ? "" : " disabled"}>검토 승인</button>` : ""}
        ${rejectOk ? `<button type="button" data-clobe-action="bulk" data-kind="reject"${n ? "" : " disabled"}>반려</button>` : ""}
        ${confirmOk ? `<button type="button" class="primary" data-clobe-action="bulk" data-kind="confirm"${n ? "" : " disabled"}>원장 확정</button>` : ""}
        ${stage === "reviewed" && !manage ? `<span class="clobe-muted">확정은 대표·관리자만 할 수 있습니다.</span>` : ""}
      </div></div>`;
  }

  function itemRow(item) {
    const checked = state.selected.has(item.item_id);
    return `<div class="clobe-item${item.item_id === state.detailId ? " active" : ""}" role="row" data-item="${esc(item.item_id)}">
      <label class="clobe-check"><input type="checkbox" data-clobe-select="${esc(item.item_id)}"${checked ? " checked" : ""} aria-label="항목 선택"></label>
      <button type="button" class="clobe-item-main" data-clobe-action="detail" data-item="${esc(item.item_id)}">
        <span class="when">${esc(fmtDate(item.occurred_on) || "일자 없음")}</span>
        <span class="kind">${esc(KINDS[item.data_kind] || item.data_kind)}</span>
        <span class="party">${esc(item.counterparty || "거래처 없음")}<small>${esc(item.institution || "")}</small></span>
        <span class="dir">${esc(itemDirectionLabel(item))}</span>
        <span class="amt">${esc(amountText(item.amount))}</span>
        <span class="stage">${badge(item.stage, STAGES[item.stage] || item.stage)}${item.is_revision ? `<em class="clobe-rev">수정본</em>` : ""}</span>
      </button>
    </div>`;
  }

  function reviewListHtml() {
    if (state.itemsLoading) return `<div class="clobe-state loading"><strong>거래를 불러오는 중입니다</strong><span>선택한 회사의 자료를 확인하고 있습니다.</span></div>`;
    if (state.itemsError) return failureHtml(state.itemsError, "reload-items");
    const rows = visibleItems();
    if (!state.items.length) {
      return `<div class="clobe-state" data-clobe-failure="nodata"><strong>${esc(STAGES[state.ui.stage])} 단계의 거래가 없습니다</strong><span>${state.ui.stage === "review_box" ? "새로 수집된 자료가 없거나 모두 처리되었습니다. 자료현황에서 수집 상태를 확인하십시오." : "다른 처리 단계를 선택해 보십시오."}</span><div class="clobe-state-actions"><button type="button" data-clobe-action="go" data-view="clobeOverview">자료현황 보기</button></div></div>`;
    }
    if (!rows.length) return `<div class="clobe-state" data-clobe-failure="nomatch"><strong>조건에 맞는 거래가 없습니다</strong><span>기간·기관·검색 조건을 바꿔 보십시오.</span><div class="clobe-state-actions"><button type="button" data-clobe-action="reset-filters">조건 초기화</button></div></div>`;
    const partial = state.itemsTotal > state.items.length;
    return `${partial ? `<div class="clobe-notice warn">전체 ${state.itemsTotal.toLocaleString("ko-KR")}건 중 ${state.items.length.toLocaleString("ko-KR")}건만 불러왔습니다. 기간·종류 조건으로 범위를 좁혀 확인하십시오.</div>` : ""}
      ${actionBar(rows)}
      <div class="clobe-list-head"><label class="clobe-check"><input type="checkbox" data-clobe-select-all${rows.length && rows.every(r => state.selected.has(r.item_id)) ? " checked" : ""} aria-label="표시된 항목 모두 선택"></label><b>${rows.length.toLocaleString("ko-KR")}건 표시</b><span class="clobe-muted">행을 누르면 상세와 대사를 볼 수 있습니다.</span></div>
      <div class="clobe-list" role="table" aria-label="거래 목록">${rows.map(itemRow).join("")}</div>`;
  }

  function renderReview() {
    const gate = gateHtml("refresh");
    if (gate) return frame("clobeReview", gate);
    return frame("clobeReview", `${companyBar()}${authBanner()}${reviewFilters()}<div data-clobe-review-body>${reviewListHtml()}</div><aside class="clobe-drawer${state.detailId ? " open" : ""}" data-clobe-drawer aria-label="거래 상세">${drawerHtml()}</aside>`);
  }

  function renderReviewBody() {
    if (state.view !== "clobeReview") return;
    const root = rootFor("clobeReview");
    const body = root?.querySelector("[data-clobe-review-body]");
    if (!body) { renderCurrent(); return; }
    body.innerHTML = reviewListHtml();
    const drawer = root.querySelector("[data-clobe-drawer]");
    if (drawer) { drawer.innerHTML = drawerHtml(); drawer.classList.toggle("open", Boolean(state.detailId)); }
    const filters = root.querySelector("[data-clobe-filters]");
    if (filters) {
      const inst = filters.querySelector('[data-clobe-filter="institution"]');
      const dir = filters.querySelector('[data-clobe-filter="direction"]');
      if (inst && document.activeElement !== inst) inst.innerHTML = `<option value="">전체</option>${[...new Set(state.items.map(i => i.institution).filter(Boolean))].sort().map(n => `<option value="${esc(n)}"${state.ui.institution === n ? " selected" : ""}>${esc(n)}</option>`).join("")}`;
      if (dir && document.activeElement !== dir) dir.innerHTML = `<option value="">전체</option>${[...new Set(state.items.map(i => i.direction).filter(Boolean))].map(d => `<option value="${esc(d)}"${state.ui.direction === d ? " selected" : ""}>${esc(DIRECTIONS[d] || d)}</option>`).join("")}`;
    }
  }

  function latestReconcile(kind) {
    const company = selectedCompany();
    const runs = (company && state.runs[company.clobe_company_id]) || [];
    const run = runs.find(r => r.data_kind === kind && r.mode === "live" && ["succeeded", "partial"].includes(r.status) && Array.isArray(r.counts?.reconcile) && r.counts.reconcile.length);
    if (!run) return null;
    const rows = run.counts.reconcile;
    return { run, ok: rows.every(r => r.ok), rows };
  }

  function duplicateCandidates(item) {
    return state.items.filter(other => other.item_id !== item.item_id && other.data_kind === item.data_kind
      && other.occurred_on === item.occurred_on && other.amount === item.amount && (other.counterparty || "") === (item.counterparty || ""));
  }

  function stepper(item) {
    const stage = item.stage;
    const steps = ["검토함 접수", "대사 확인", "검토 승인", "원장 확정"];
    const activeIndex = stage === "rejected" || stage === "superseded" ? -1 : stage === "review_box" ? 1 : stage === "reviewed" ? 3 : 4;
    return `<ol class="clobe-stepper" aria-label="처리 순서">${steps.map((label, i) => `<li class="${i < activeIndex ? "done" : i === activeIndex ? "active" : ""}"><b>${i + 1}</b><span>${esc(label)}</span></li>`).join("")}</ol>`;
  }

  function reconcileHtml(item) {
    const rec = latestReconcile(item.data_kind);
    const dups = duplicateCandidates(item);
    let body;
    if (!rec) {
      body = `<p class="clobe-muted">이 종류의 대사 기록이 없습니다. 자료현황에서 수집을 다시 실행하면 대사 결과가 생깁니다.</p>`;
    } else {
      body = rec.rows.map(r => `<p class="${r.ok ? "ok" : "bad"}">${r.ok ? "일치" : "불일치"} — 받은 건수 ${Number(r.collected_total ?? 0).toLocaleString("ko-KR")}건 / 원천 보고 ${r.reported_total == null ? "알 수 없음" : `${Number(r.reported_total).toLocaleString("ko-KR")}건`}${(r.sum_checks || []).some(s => s.status === "mismatch") ? " · 합계 불일치" : (r.sum_checks || []).some(s => s.status === "ok") ? " · 합계 일치" : " · 합계 대조 불가"}</p>`).join("")
        + `<p class="clobe-muted">대사 기준 수집: ${esc(fmtDate(rec.run.period?.[0]))} ~ ${esc(fmtDate(rec.run.period?.[1]))} (${esc(fmtDateTime(rec.run.started_at))})</p>`;
    }
    return `<section class="clobe-recon"><h3>대사</h3>${body}
      <p class="${dups.length ? "bad" : "ok"}">${dups.length ? `같은 날짜·금액·거래처의 다른 거래가 ${dups.length}건 있습니다. 중복 여부를 확인하십시오.` : "같은 날짜·금액·거래처의 중복 후보가 없습니다."}</p>
      ${item.is_revision ? `<p class="bad">원천에서 내용이 바뀌어 다시 수집된 수정본입니다. 확정 시 기존 확정분을 대체합니다.</p>` : ""}
      ${rec && !rec.ok ? `<label class="clobe-ack"><input type="checkbox" data-clobe-ack${state.ackMismatch ? " checked" : ""}> 대사 불일치를 확인했고, 그래도 진행합니다.</label>` : ""}
    </section>`;
  }

  function drawerHtml() {
    const item = state.items.find(row => row.item_id === state.detailId);
    if (!item) return "";
    const rec = latestReconcile(item.data_kind);
    const blocked = rec && !rec.ok && !state.ackMismatch;
    const review = can("review");
    const manage = can("manage");
    const actions = [];
    if (review && item.stage === "review_box") actions.push(`<button type="button" class="primary" data-clobe-action="one" data-kind="approve"${blocked ? " disabled" : ""}>검토 승인</button>`);
    if (review && ["review_box", "reviewed"].includes(item.stage)) actions.push(`<button type="button" data-clobe-action="one" data-kind="reject">반려</button>`);
    if (manage && item.stage === "reviewed") actions.push(`<button type="button" class="primary" data-clobe-action="one" data-kind="confirm"${blocked ? " disabled" : ""}>원장 확정</button>`);
    return `<div class="clobe-drawer-head"><h2>거래 상세</h2><button type="button" data-clobe-action="close-detail" aria-label="상세 닫기">닫기</button></div>
      ${stepper(item)}
      <dl class="clobe-detail">
        <div><dt>자료 종류</dt><dd>${esc(KINDS[item.data_kind] || item.data_kind)}</dd></div>
        <div><dt>거래일</dt><dd>${esc(fmtDate(item.occurred_on) || "-")}</dd></div>
        <div><dt>기관</dt><dd>${esc(item.institution || "-")}</dd></div>
        <div><dt>거래처</dt><dd>${esc(item.counterparty || "-")}</dd></div>
        <div><dt>구분</dt><dd>${esc(itemDirectionLabel(item))}</dd></div>
        <div><dt>금액</dt><dd><b>${esc(amountText(item.amount))}</b></dd></div>
        <div><dt>처리 단계</dt><dd>${badge(item.stage, STAGES[item.stage] || item.stage)}</dd></div>
        <div><dt>원천 기준 시각</dt><dd>${esc(fmtDateTime(item.source_as_of) || "-")}</dd></div>
      </dl>
      ${reconcileHtml(item)}
      ${state.pending?.scope === "one" ? pendingHtml() : `<div class="clobe-drawer-actions">${actions.join("") || `<span class="clobe-muted">${item.stage === "confirmed" ? "원장에 확정된 거래입니다." : item.stage === "rejected" ? "반려된 거래입니다." : item.stage === "superseded" ? "수정본으로 대체된 거래입니다." : "이 계정에는 처리 권한이 없습니다."}</span>`}</div>`}`;
  }

  async function applyAction(kind, ids) {
    const company = selectedCompany();
    if (!company || !ids.length) return;
    state.itemsLoading = true;
    renderReviewBody();
    try {
      const business = encodeURIComponent(company.business_id);
      let text;
      if (kind === "confirm") {
        const out = await request(`/businesses/${business}/items/confirm`, { method: "POST", body: { item_ids: ids } });
        text = `${out.confirmed}건을 원장에 확정했습니다.${out.skipped ? ` ${out.skipped}건은 검토 완료 상태가 아니거나 이미 처리되어 건너뛰었습니다.` : ""}`;
        state.notice = { tone: out.confirmed ? "ok" : "warn", text };
      } else {
        const out = await request(`/businesses/${business}/items/review`, { method: "POST", body: { item_ids: ids, decision: kind } });
        text = `${out.updated}건을 ${kind === "approve" ? "검토 승인" : "반려"}했습니다.${out.skipped ? ` ${out.skipped}건은 처리할 수 없는 단계여서 건너뛰었습니다.` : ""}`;
        state.notice = { tone: out.updated ? "ok" : "warn", text };
      }
    } catch (error) {
      state.itemsLoading = false;
      state.notice = { tone: "error", text: `${explain(error).title}. ${explain(error).body}` };
      state.pending = null;
      renderReviewBody();
      renderCurrent();
      return;
    }
    state.pending = null;
    state.detailId = "";
    state.ackMismatch = false;
    state.summaryKey = "";
    await Promise.all([loadItems(true), refreshStatusQuiet()]);
    renderCurrent();
  }

  async function refreshStatusQuiet() {
    try {
      state.status = await request("/status");
      applyAuthInfo();
    } catch (error) {
      state.statusError = error;
    }
  }

  // ── 경영요약 ────────────────────────────────────────────
  function summarize(items, from, to) {
    const total = { bankIn: 0, bankOut: 0, sales: 0, purchase: 0, cash: 0, card: 0, unclassified: 0, months: {}, count: 0 };
    items.forEach(item => {
      const day = fmtDate(item.occurred_on);
      if (from && (!day || day < from)) return;
      if (to && (!day || day > to)) return;
      total.count += 1;
      const c = cents(item.amount);
      const month = day.slice(0, 7) || "일자 없음";
      const m = total.months[month] || (total.months[month] = { bankIn: 0, bankOut: 0, sales: 0, purchase: 0 });
      if (item.data_kind === "cash_receipt") { total.cash += 1; return; }
      if (item.data_kind === "card_approval") { total.card += 1; return; }
      if (c === null) { total.unclassified += 1; return; }
      if (item.data_kind === "bank_transaction" && item.direction === "IN") { total.bankIn += c; m.bankIn += c; }
      else if (item.data_kind === "bank_transaction" && item.direction === "OUT") { total.bankOut += c; m.bankOut += c; }
      else if (item.data_kind === "tax_invoice" && item.direction === "SALES") { total.sales += c; m.sales += c; }
      else if (item.data_kind === "tax_invoice" && item.direction === "PURCHASE") { total.purchase += c; m.purchase += c; }
      else total.unclassified += 1;
    });
    return total;
  }

  function summaryCard(label, value, note, tone) {
    return `<article class="clobe-metric ${esc(tone || "")}"><small>${esc(label)}</small><strong>${value}</strong><span>${esc(note)}</span></article>`;
  }

  function renderSummary() {
    const gate = gateHtml("refresh");
    if (gate) return frame("clobeSummary", gate);
    const company = selectedCompany();
    const fresh = freshness(company);
    const counts = stageCounts(company);
    let body;
    if (state.summaryLoading) body = `<div class="clobe-state loading"><strong>확정 자료를 집계하는 중입니다</strong><span>원장에 확정된 거래만 합산합니다.</span></div>`;
    else if (state.summaryError) body = failureHtml(state.summaryError, "reload-summary");
    else if (!state.summaryItems.length) {
      body = `<div class="clobe-state" data-clobe-failure="nodata"><strong>확정된 자료가 없어 요약할 수 없습니다</strong><span>수집된 자료를 검토·확정하면 여기에 집계됩니다. 확정 전에는 금액을 표시하지 않습니다.</span><div class="clobe-state-actions"><button type="button" class="primary" data-clobe-action="open-review" data-stage="${counts.reviewed ? "reviewed" : "review_box"}">거래 검토함으로 이동</button></div></div>`;
    } else {
      const t = summarize(state.summaryItems, state.ui.summaryFrom, state.ui.summaryTo);
      const partialLoad = state.summaryTotal > state.summaryItems.length;
      const months = Object.keys(t.months).sort().reverse();
      const kindWarn = fresh.kinds.filter(k => k.status !== "succeeded").map(k => `${KINDS[k.kind]}(${RUN_STATUS[k.status]})`);
      body = `
        ${partialLoad ? `<div class="clobe-notice warn">확정 ${state.summaryTotal.toLocaleString("ko-KR")}건 중 ${state.summaryItems.length.toLocaleString("ko-KR")}건만 불러와 집계했습니다. 기간을 좁혀 확인하십시오.</div>` : ""}
        ${kindWarn.length ? `<div class="clobe-notice warn">수집이 완료되지 않은 종류가 있어 실제보다 적게 보일 수 있습니다: ${esc(kindWarn.join(", "))}</div>` : ""}
        <div class="clobe-metrics">
          ${summaryCard("자금 · 입금", won(t.bankIn), "확정된 은행 입금 합계")}
          ${summaryCard("자금 · 출금", won(t.bankOut), "확정된 은행 출금 합계")}
          ${summaryCard("자금 · 순증감", won(t.bankIn - t.bankOut), "입금 − 출금", t.bankIn - t.bankOut < 0 ? "neg" : "")}
          ${summaryCard("매출", won(t.sales), "세금계산서 기준 확정 합계")}
          ${summaryCard("매입", won(t.purchase), "세금계산서 기준 확정 합계")}
        </div>
        <p class="clobe-muted">현금영수증 ${t.cash.toLocaleString("ko-KR")}건, 카드 승인 ${t.card.toLocaleString("ko-KR")}건은 취소·정정 구분이 목록 자료에 없어 금액 합계에 넣지 않았습니다. 건별 금액은 거래 검토함에서 확인하십시오.${t.unclassified ? ` 구분을 알 수 없는 ${t.unclassified}건도 합계에서 제외했습니다.` : ""}</p>
        <section class="clobe-panel"><h2>월별 추이</h2>
          ${months.length ? `<div class="clobe-month-table" role="table"><div class="head" role="row"><span>월</span><span>입금</span><span>출금</span><span>매출</span><span>매입</span></div>${months.map(month => { const m = t.months[month]; return `<div role="row"><span>${esc(month)}</span><span>${esc(won(m.bankIn))}</span><span>${esc(won(m.bankOut))}</span><span>${esc(won(m.sales))}</span><span>${esc(won(m.purchase))}</span></div>`; }).join("")}</div>` : `<p class="clobe-muted">선택한 기간에 확정된 자료가 없습니다.</p>`}
        </section>
        <section class="clobe-panel"><h2>원장 바로가기</h2><div class="clobe-links">
          <button type="button" data-clobe-action="go" data-view="salesLedger">매출 상세</button>
          <button type="button" data-clobe-action="go" data-view="purchaseLedger">매입 상세</button>
          <button type="button" data-clobe-action="go" data-view="bankLedger">은행 거래내역</button>
          <button type="button" data-clobe-action="go" data-view="cardLedger">카드 사용내역</button>
        </div></section>`;
    }
    const pendingText = counts.review_box || counts.reviewed ? `아직 확정되지 않은 자료: 검토 대기 ${counts.review_box}건 · 확정 대기 ${counts.reviewed}건 (이 합계에는 포함되지 않음)` : "";
    return frame("clobeSummary", `${companyBar()}
      <form class="clobe-filters" data-clobe-summary-filters onsubmit="return false">
        <label>시작일<input type="date" data-clobe-sfilter="summaryFrom" value="${esc(state.ui.summaryFrom)}"></label>
        <label>종료일<input type="date" data-clobe-sfilter="summaryTo" value="${esc(state.ui.summaryTo)}"></label>
        <button type="button" data-clobe-action="reset-summary">기간 초기화</button>
      </form>
      <div class="clobe-basis"><span>마지막 수집 ${fresh.lastSuccess ? esc(fmtDateTime(fresh.lastSuccess)) : "기록 없음"}</span><span>받은 기간 ${fresh.from && fresh.to ? `${esc(fmtDate(fresh.from))} ~ ${esc(fmtDate(fresh.to))}` : "-"}</span>${badge(fresh.overall, OVERALL_LABEL[fresh.overall])}</div>
      ${pendingText ? `<div class="clobe-notice info">${esc(pendingText)}</div>` : ""}
      ${body}`);
  }

  // ── 연결 설정 ───────────────────────────────────────────
  async function loadAdmin() {
    state.admin.error = null;
    state.admin.busy = "load";
    renderCurrent();
    try {
      state.admin.status = await request("/status?scope=all");
      state.admin.loaded = true;
      state.internalAdmin = true;
    } catch (error) {
      state.admin.error = error;
      state.admin.loaded = true;
    } finally {
      state.admin.busy = "";
    }
    renderCurrent();
  }

  const LINK_LABEL = { linked: "연결됨", review: "승인 대기", blocked: "차단", absent: "목록에서 사라짐" };

  function settingsAuthPanel() {
    const auth = state.admin.status?.auth || state.status?.auth;
    if (!auth) return "";
    const label = auth.status === "connected" ? "연결됨" : auth.reauth_required ? "승인 필요" : String(auth.status || "확인 중");
    return `<section class="clobe-panel"><h2>외부 자료 연결</h2>
      <div class="clobe-basis"><span>${auth.reauth_required ? badge("needs_reauth", label) : badge("succeeded", label)}</span><span>마지막 정상 조회 ${auth.last_success_at ? esc(fmtDateTime(auth.last_success_at)) : "기록 없음"}</span>${auth.token_expires_at ? `<span>승인 만료 예정 ${esc(fmtDateTime(auth.token_expires_at))}</span>` : ""}</div>
      ${auth.reauth_required
        ? (state.internalAdmin ? `<p>승인이 필요합니다. 아래 버튼으로 승인 화면을 열어 한 번만 승인하면 이어서 수집됩니다.</p><button type="button" class="primary" data-clobe-action="reauth"${state.admin.busy === "reauth" ? " disabled" : ""}>연결 승인 받기</button>` : `<p class="clobe-muted">관리자가 승인을 다시 받아야 합니다.</p>`)
        : `<p class="clobe-muted">연결이 정상입니다. 이미 승인된 연결이라 다시 승인할 필요가 없습니다.</p>`}
    </section>`;
  }

  function companyAdminRow(item) {
    const busy = state.admin.busy.endsWith(item.clobe_company_id);
    const linkedHint = item.permission_ok === true ? `<small class="ok">조회 권한 확인됨 ${item.permission_checked_at ? `(${esc(fmtDateTime(item.permission_checked_at))})` : ""}</small>` : item.permission_ok === false ? `<small class="bad">조회 권한 없음${item.permission_checked_at ? ` (${esc(fmtDateTime(item.permission_checked_at))})` : ""}</small>` : `<small class="clobe-muted">권한 미확인</small>`;
    const candidates = (item.candidate_business_ids || []);
    const approve = item.link_status === "review" && state.internalAdmin ? `<div class="clobe-link-form"><label>연결할 사업자<select data-clobe-link-select="${esc(item.clobe_company_id)}">${candidates.length ? candidates.map(id => `<option value="${esc(id)}">${esc(id)}</option>`).join("") : `<option value="">후보 없음</option>`}</select></label>
      <label class="clobe-ack"><input type="checkbox" data-clobe-link-evidence="${esc(item.clobe_company_id)}"> 회사명 일치를 직접 확인했습니다.</label>
      <button type="button" class="primary" data-clobe-action="link" data-company="${esc(item.clobe_company_id)}"${candidates.length && !busy ? "" : " disabled"}>연결 승인</button></div>` : "";
    return `<article class="clobe-company-card" data-company="${esc(item.clobe_company_id)}"><header><b>${esc(item.company_name)}</b><span>${esc(item.reg_no_masked || "사업자번호 없음")}</span>${badge(item.link_status === "linked" ? "succeeded" : item.link_status === "blocked" ? "failed" : "partial", LINK_LABEL[item.link_status] || item.link_status)}</header>
      <div>${linkedHint}${item.permission_error ? `<small class="bad">${esc(codeText(item.permission_error))}</small>` : ""}</div>
      ${state.internalAdmin ? `<div class="clobe-state-actions"><button type="button" data-clobe-action="perm" data-company="${esc(item.clobe_company_id)}"${busy ? " disabled" : ""}>권한 확인</button>${item.link_status !== "blocked" ? `<button type="button" class="clobe-danger" data-clobe-action="block" data-company="${esc(item.clobe_company_id)}"${busy ? " disabled" : ""}>수집 제외</button>` : ""}</div>` : ""}
      ${approve}</article>`;
  }

  function renderSettings() {
    if (!token()) return frame("clobeSettings", gateHtml("refresh"));
    const adminList = state.admin.status?.companies;
    const list = adminList || state.status?.companies || [];
    let listBody;
    if (state.admin.busy === "load" && !adminList) listBody = `<div class="clobe-state loading"><strong>회사 연결 목록을 불러오는 중입니다</strong></div>`;
    else if (state.admin.error && !state.status) listBody = failureHtml(state.admin.error, "refresh");
    else if (!list.length) listBody = `<div class="clobe-state" data-clobe-failure="none"><strong>연결된 회사가 없습니다</strong><span>${state.internalAdmin ? "‘회사 목록 새로 찾기’를 눌러 외부 서비스의 회사를 가져오십시오." : "관리자에게 회사 연결 승인을 요청하십시오."}</span></div>`;
    else listBody = `<div class="clobe-company-list">${list.map(companyAdminRow).join("")}</div>`;
    const denied = state.admin.error && Number(state.admin.error.status) === 403;
    return frame("clobeSettings", `
      ${state.admin.message ? `<div class="clobe-notice info" role="status" aria-live="polite">${esc(state.admin.message)}</div>` : ""}
      ${denied ? `<div class="clobe-notice info">회사 연결 승인·권한 확인은 관리자 전용입니다. 아래에는 현재 사업장에 연결된 회사만 표시됩니다.</div>` : ""}
      ${settingsAuthPanel()}
      <section class="clobe-panel"><div class="clobe-panel-head"><h2>회사 연결</h2>${state.internalAdmin ? `<button type="button" data-clobe-action="discover"${state.admin.busy === "discover" ? " disabled" : ""}>${state.admin.busy === "discover" ? "찾는 중…" : "회사 목록 새로 찾기"}</button>` : ""}</div>${listBody}</section>`);
  }

  async function adminCall(label, path, options, doneText) {
    state.admin.busy = label;
    state.admin.message = "";
    renderCurrent();
    try {
      const out = await request(path, options);
      state.admin.message = typeof doneText === "function" ? doneText(out) : doneText;
      state.admin.busy = "";
      await loadAdmin();
      await refreshStatusQuiet();
    } catch (error) {
      const info = explain(error);
      state.admin.message = `${info.title}. ${info.body}`;
      state.admin.busy = "";
    }
    renderCurrent();
  }

  // ── 렌더 · 이벤트 ───────────────────────────────────────
  const RENDER = { clobeOverview: renderOverview, clobeReview: renderReview, clobeSummary: renderSummary, clobeSettings: renderSettings };

  function renderCurrent() {
    const root = rootFor(state.view);
    if (!root || !RENDER[state.view]) return;
    const active = document.activeElement;
    const keep = active?.matches?.("[data-clobe-filter='q']") ? { selector: "[data-clobe-filter='q']", start: active.selectionStart } : null;
    root.innerHTML = RENDER[state.view]();
    if (keep) {
      const field = root.querySelector(keep.selector);
      if (field) { field.focus(); try { field.setSelectionRange(keep.start, keep.start); } catch (_) { /* 선택 범위 복원 불가 */ } }
    }
  }

  async function open(view) {
    if (!RENDER[view]) return;
    state.view = view;
    state.notice = null;
    state.pending = null;
    renderCurrent();
    if (!token()) return;
    if (!state.status || state.statusError) await loadStatus();
    if (state.view !== view) return;
    if (view === "clobeOverview") loadRuns();
    if (view === "clobeReview") { loadRuns(); loadItems(false); }
    if (view === "clobeSummary") loadSummary(false);
    if (view === "clobeSettings" && !state.admin.loaded) loadAdmin();
  }

  function goView(view) {
    const tab = document.querySelector(`.side-nav .tab[data-view="${view}"]`) || document.querySelector(`.tab[data-view="${view}"]`);
    if (tab) tab.click();
  }

  async function runCollection(kinds) {
    const company = selectedCompany();
    if (!company || state.runBusy) return;
    const to = state.ui.runTo || todayText();
    const from = state.ui.runFrom || shiftDays(to, -90);
    state.runBusy = kinds ? kinds[0] : "all";
    state.runResult = null;
    renderCurrent();
    try {
      const body = { period_start: from, period_end: to, mode: "live" };
      if (kinds) body.kinds = kinds;
      state.runResult = await request(`/companies/${encodeURIComponent(company.clobe_company_id)}/runs`, { method: "POST", body, timeout: 180000 });
    } catch (error) {
      state.runResult = { error };
    }
    state.runBusy = "";
    state.summaryKey = "";
    state.itemsKey = "";
    await refreshStatusQuiet();
    await loadRuns();
    renderCurrent();
  }

  async function handleAction(target) {
    const action = target.dataset.clobeAction;
    switch (action) {
      case "login": {
        const loginButton = document.getElementById("loginBtn");
        if (loginButton) loginButton.click();
        break;
      }
      case "refresh":
        state.admin.loaded = false;
        state.summaryKey = "";
        state.itemsKey = "";
        await loadStatus();
        open(state.view);
        break;
      case "reload-items": loadItems(true); break;
      case "reload-summary": loadSummary(true); break;
      case "go": goView(target.dataset.view); break;
      case "dismiss-notice": state.notice = null; renderCurrent(); break;
      case "open-review":
        state.ui.stage = target.dataset.stage || "review_box";
        saveUi();
        state.itemsKey = "";
        goView("clobeReview");
        break;
      case "run-all": runCollection(null); break;
      case "run-kind": runCollection([target.dataset.kind]); break;
      case "reset-filters":
        Object.assign(state.ui, { kind: "", from: "", to: "", institution: "", direction: "", q: "" });
        saveUi();
        loadItems(true);
        renderCurrent();
        break;
      case "reset-summary":
        Object.assign(state.ui, { summaryFrom: "", summaryTo: "" });
        saveUi();
        renderCurrent();
        break;
      case "detail":
        state.detailId = state.detailId === target.dataset.item ? "" : target.dataset.item;
        state.ackMismatch = false;
        renderReviewBody();
        break;
      case "close-detail": state.detailId = ""; renderReviewBody(); break;
      case "one": state.pending = { scope: "one", kind: target.dataset.kind, ids: [state.detailId] }; renderReviewBody(); break;
      case "bulk": state.detailId = ""; state.pending = { scope: "bulk", kind: target.dataset.kind, ids: [...state.selected] }; renderReviewBody(); break;
      case "pending-cancel": state.pending = null; renderReviewBody(); break;
      case "pending-go": if (state.pending) applyAction(state.pending.kind, state.pending.ids); break;
      case "reauth": {
        state.admin.busy = "reauth";
        renderCurrent();
        try {
          const out = await request("/reauth", { method: "POST", base: INTEGRATION_API });
          state.admin.message = "승인 화면을 새 창으로 열었습니다. 승인을 마친 뒤 이 화면에서 새로고침하십시오.";
          if (out.authorize_url) window.open(out.authorize_url, "_blank", "noopener");
        } catch (error) {
          state.admin.message = Number(error.status) === 409 ? "이미 승인된 연결이라 다시 승인할 필요가 없습니다." : `${explain(error).title}. ${explain(error).body}`;
        }
        state.admin.busy = "";
        renderCurrent();
        break;
      }
      case "discover": adminCall("discover", "/admin/discover", { method: "POST", timeout: 60000 }, out => `회사 ${Number(out.seen || 0)}곳을 확인했습니다. 새로 발견 ${Number(out.new || 0)}곳 · 승인 대기 ${Number(out.review || 0)}곳.`); break;
      case "perm": adminCall(`perm:${target.dataset.company}`, `/admin/companies/${encodeURIComponent(target.dataset.company)}/permission-check`, { method: "POST", timeout: 60000 }, out => out.permission_ok ? "조회 권한이 확인되었습니다." : "조회 권한이 없습니다. 외부 서비스에서 권한을 확인하십시오."); break;
      case "block": {
        if (!window.confirm("이 회사를 수집 대상에서 제외합니다. 계속할까요?")) break;
        adminCall(`block:${target.dataset.company}`, `/admin/companies/${encodeURIComponent(target.dataset.company)}/block`, { method: "POST" }, "수집 대상에서 제외했습니다.");
        break;
      }
      case "link": {
        const id = target.dataset.company;
        const root = rootFor("clobeSettings");
        const select = root.querySelector(`[data-clobe-link-select="${CSS.escape(id)}"]`);
        const evidence = root.querySelector(`[data-clobe-link-evidence="${CSS.escape(id)}"]`);
        if (!select?.value) break;
        adminCall(`link:${id}`, `/admin/companies/${encodeURIComponent(id)}/link`, { method: "POST", body: { business_id: select.value, name_evidence: Boolean(evidence?.checked) } }, "회사 연결을 승인했습니다.");
        break;
      }
      default: break;
    }
  }

  function bindRoot(view) {
    const root = rootFor(view);
    if (!root || root.dataset.clobeBound) return;
    root.dataset.clobeBound = "1";
    root.addEventListener("click", event => {
      const target = event.target.closest("[data-clobe-action]");
      if (target && !target.disabled) handleAction(target);
    });
    root.addEventListener("change", event => {
      const el = event.target;
      if (el.matches("[data-clobe-company]")) {
        state.ui.companyId = el.value;
        saveUi();
        state.itemsKey = "";
        state.summaryKey = "";
        state.detailId = "";
        state.selected = new Set();
        open(state.view);
      } else if (el.matches("[data-clobe-filter]")) {
        const key = el.dataset.clobeFilter;
        state.ui[key] = el.value;
        saveUi();
        state.detailId = "";
        if (key === "stage" || key === "kind") loadItems(true); else renderReviewBody();
      } else if (el.matches("[data-clobe-sfilter]")) {
        state.ui[el.dataset.clobeSfilter] = el.value;
        saveUi();
        renderCurrent();
      } else if (el.matches("[data-clobe-run-from]")) {
        state.ui.runFrom = el.value; saveUi();
      } else if (el.matches("[data-clobe-run-to]")) {
        state.ui.runTo = el.value; saveUi();
      } else if (el.matches("[data-clobe-select]")) {
        if (el.checked) state.selected.add(el.dataset.clobeSelect); else state.selected.delete(el.dataset.clobeSelect);
        state.pending = null;
        renderReviewBody();
      } else if (el.matches("[data-clobe-select-all]")) {
        visibleItems().forEach(item => { if (el.checked) state.selected.add(item.item_id); else state.selected.delete(item.item_id); });
        state.pending = null;
        renderReviewBody();
      } else if (el.matches("[data-clobe-ack]")) {
        state.ackMismatch = el.checked;
        renderReviewBody();
      }
    });
    let timer = 0;
    root.addEventListener("input", event => {
      const el = event.target;
      if (!el.matches("[data-clobe-filter='q']")) return;
      state.ui.q = el.value;
      saveUi();
      clearTimeout(timer);
      timer = setTimeout(renderReviewBody, 200);
    });
  }

  function landingView() {
    if (!token()) return "";
    try {
      const saved = JSON.parse(sessionStorage.getItem("obys_clobe_landing") || "null");
      return saved?.role === role() && saved.view ? saved.view : "";
    } catch (_) {
      return "";
    }
  }

  async function probeLanding() {
    if (!token() || landingView()) return;
    const current = role();
    if (!READ_ROLES.includes(current)) return;
    try {
      const status = await request("/status");
      if (!(status.companies || []).some(item => item.link_status === "linked")) return;
      const counts = (status.companies || []).reduce((sum, item) => sum + Number(item.item_stage_counts?.review_box || 0) + Number(item.item_stage_counts?.reviewed || 0), 0);
      const view = MANAGE_ROLES.includes(current) ? (counts ? "clobeOverview" : "clobeSummary") : current === "member" ? "clobeReview" : "clobeSummary";
      sessionStorage.setItem("obys_clobe_landing", JSON.stringify({ role: current, view }));
      if (!state.navigated) goView(view);
    } catch (_) {
      // 대상이 아니거나 일시 장애면 기존 첫 화면을 유지한다.
    }
  }

  function init() {
    Object.keys(VIEWS).forEach(bindRoot);
    document.addEventListener("click", event => {
      if (event.target.closest?.(".tab[data-view]") && event.isTrusted) state.navigated = true;
    }, true);
    window.addEventListener("online", () => { if (RENDER[state.view]) open(state.view); });
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "visible" && RENDER[state.view] && token() && (state.statusError?.status === 401 || !state.status)) {
        state.status = null;
        open(state.view);
      }
    });
    setTimeout(probeLanding, 600);
  }

  window.obysClobe = { open, landingView, explain, codeText, summarize };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
