(() => {
  "use strict";

  const API = {
    dashboard: "/api/v1/yeoljeong-dashboard",
    inventory: "/api/v1/yeoljeong-inventory",
    accounting: "/api/v1/yeoljeong-accounting",
    ops: "/api/v1/yeoljeong-ops"
  };
  const TOKEN_KEYS = ["aads_token", "fb_access_token"];
  const DRAFT_KEY = "obys_inventory_order_draft";
  const DRAFT_FIELDS = ["supplier", "item_id", "quantity", "total_amount", "memo"];
  const connectedViews = new Set(["dashboard", "tasks", "inventory", "tax", "approvals", "alerts", "audit"]);
  let activeController = null;
  let lastFocus = null;

  // 화면 호스트. 기존 관리화면(index.html)은 그 화면의 지점 필터·사업자 선택·탭·로그인 창을 쓴다.
  // 새 관리화면(admin.html)은 스크립트에 data-obys-host 를 달고 mount() 로 사업자와 세션 만료 처리를 넘긴다 —
  // 이때는 기존 관리화면 전역(branchByName·selectedSettingsBusinessId)과 화면 요소를 읽지 않는다.
  const HOST_MODE = String(document.currentScript?.dataset?.obysHost || "");
  let mounted = null;
  const MOUNTED_VIEW = "assistant";

  const byId = id => (mounted?.element?.querySelector(`#${id}`)) || (HOST_MODE ? null : document.getElementById(id));
  const escapeHtml = value => String(value ?? "").replace(/[&<>'"]/g, char => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;"
  })[char]);
  const money = value => `${Number(value || 0).toLocaleString("ko-KR")}원`;
  const number = value => Number(value || 0).toLocaleString("ko-KR", { maximumFractionDigits: 3 });
  const text = (row, ...keys) => keys.map(key => row?.[key]).find(value => value !== undefined && value !== null && value !== "") ?? "-";
  const token = () => TOKEN_KEYS.map(key => localStorage.getItem(key)).find(Boolean) || "";

  function scope() {
    if (mounted || HOST_MODE) return { business_id: mounted?.businessId || "", branch_id: "" };
    const branchName = byId("branchFilter")?.value || "";
    const branch = typeof window.branchByName === "function" ? window.branchByName(branchName) : null;
    const businessId = branch?.businessId || window.selectedSettingsBusinessId?.() || "";
    return { business_id: businessId, branch_id: branch?.id || "" };
  }

  function query(extra = {}) {
    const params = new URLSearchParams({ ...scope(), ...extra });
    [...params].forEach(([key, value]) => { if (!value) params.delete(key); });
    return params.toString();
  }

  function authError(status) {
    const labels = { 401: "로그인이 만료되었습니다.", 403: "이 화면을 볼 권한이 없습니다." };
    return Object.assign(new Error(labels[status] || `서버 응답 오류 (${status})`), { status });
  }

  async function request(path, extra = {}) {
    const authToken = token();
    if (!authToken) throw Object.assign(new Error("로그인이 필요합니다."), { status: 401 });
    const response = await fetch(`${path}?${query(extra)}`, {
      headers: { Authorization: `Bearer ${authToken}`, Accept: "application/json" },
      signal: activeController?.signal
    });
    if (!response.ok) throw authError(response.status);
    return response.json();
  }

  async function send(path, body = {}, method = "POST") {
    const authToken = token();
    if (!authToken) throw Object.assign(new Error("로그인이 필요합니다."), { status: 401 });
    const response = await fetch(path, {
      method,
      headers: {
        Authorization: `Bearer ${authToken}`,
        Accept: "application/json",
        "Content-Type": "application/json"
      },
      body: JSON.stringify(body)
    });
    if (!response.ok) {
      const detail = await response.json().catch(() => null);
      const error = authError(response.status);
      if (detail?.detail) error.message = String(detail.detail);
      throw error;
    }
    return response.json();
  }

  function livebar() {
    if (mounted) return mounted.element.querySelector(".v2-livebar");
    let bar = document.querySelector(".v2-livebar");
    if (bar) return bar;
    bar = document.createElement("div");
    bar.className = "v2-livebar";
    bar.setAttribute("role", "status");
    bar.setAttribute("aria-live", "polite");
    bar.innerHTML = '<strong>실데이터</strong><span data-v2-state="loading">연동 확인 중</span><span data-v2-sync>마지막 동기화: -</span><button type="button" data-v2-retry>재시도</button>';
    byId("appMain")?.before(bar);
    bar.querySelector("[data-v2-retry]").addEventListener("click", () => refresh(currentView()));
    return bar;
  }

  function setStatus(kind, message) {
    const bar = livebar();
    if (!bar) return;
    const state = bar.querySelector("[data-v2-state]");
    state.dataset.v2State = kind;
    state.textContent = message;
    if (kind === "ready") bar.querySelector("[data-v2-sync]").textContent = `마지막 동기화: ${new Date().toLocaleString("ko-KR")}`;
    bar.querySelector("[data-v2-retry]").hidden = kind !== "error";
  }

  function showLivebarFor(view) {
    if (mounted) return view === MOUNTED_VIEW && mounted.element.isConnected;
    if (HOST_MODE) return false;
    const visible = connectedViews.has(view) && !document.body.classList.contains("signed-out");
    livebar().hidden = !visible;
    if (!visible) {
      activeController?.abort();
      activeController = null;
    }
    return visible;
  }

  const emptyRow = (colspan, message = "연결된 데이터가 없습니다.") => `<tr><td colspan="${colspan}"><div class="v2-empty">${escapeHtml(message)}</div></td></tr>`;
  const progress = (label, value) => `<div class="progress-item"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></div>`;

  // -------------------------------------------------------------------------
  // 작성 중 발주 자동 보관 + 세션 만료 복구
  //
  // 현장에서 한 손으로 발주를 적다가 세션이 끊기면 처음부터 다시 적게 된다.
  // 입력할 때마다 보관해 두고, 다시 로그인하면 그대로 되살린다.
  // -------------------------------------------------------------------------

  function orderForm() {
    return byId("inventoryOrderForm");
  }

  function saveDraft() {
    const form = orderForm();
    if (!form) return;
    const draft = { saved_at: new Date().toISOString() };
    DRAFT_FIELDS.forEach(name => { draft[name] = form.elements[name]?.value || ""; });
    if (!DRAFT_FIELDS.some(name => draft[name])) return clearDraft();
    try {
      localStorage.setItem(DRAFT_KEY, JSON.stringify(draft));
      const state = byId("inventoryDraftState");
      if (state) state.textContent = `임시 보관됨 ${new Date().toLocaleTimeString("ko-KR")}`;
    } catch (error) {
      // 저장 공간이 없으면 보관을 포기하되 입력은 막지 않는다.
    }
  }

  function readDraft() {
    try {
      return JSON.parse(localStorage.getItem(DRAFT_KEY) || "null");
    } catch (error) {
      return null;
    }
  }

  function clearDraft() {
    localStorage.removeItem(DRAFT_KEY);
    const state = byId("inventoryDraftState");
    if (state) state.textContent = "작성 중인 발주는 자동 보관됩니다.";
  }

  function restoreDraft() {
    const form = orderForm();
    const draft = readDraft();
    if (!form || !draft) return;
    DRAFT_FIELDS.forEach(name => {
      const field = form.elements[name];
      if (field && !field.value && draft[name]) field.value = draft[name];
    });
    const state = byId("inventoryDraftState");
    if (state) state.textContent = "작성 중이던 발주를 되살렸습니다.";
  }

  function recoverExpiredSession(message) {
    saveDraft();
    if (mounted || HOST_MODE) {
      setStatus("error", message || "로그인이 만료되었습니다. 다시 로그인하면 작성 중이던 발주가 복구됩니다.");
      if (typeof mounted?.onExpired === "function") mounted.onExpired();
      return;
    }
    TOKEN_KEYS.forEach(key => localStorage.removeItem(key));
    document.body.classList.add("signed-out");
    byId("authGate")?.classList.remove("hidden");
    document.querySelectorAll(".app-only").forEach(element => element.classList.add("hidden"));
    document.querySelector(".v2-bottom-nav")?.classList.add("hidden");
    setStatus("error", message || "로그인이 만료되었습니다. 다시 로그인하면 작성 중이던 발주가 복구됩니다.");
    (byId("loginBtn") || byId("openLoginFromGateBtn"))?.focus();
  }

  async function renderDashboard() {
    const [{ kpis = {} }, { tasks = [] }] = await Promise.all([
      request(`${API.dashboard}/kpis`), request(`${API.dashboard}/tasks`)
    ]);
    byId("kpiGrid").innerHTML = [
      ["오늘 매출", money(kpis.today_sales), kpis.date || "조회일 기준"],
      ["이번 달 매출", money(kpis.month_sales), "실제 수집 원장"],
      ["미정산", money(kpis.pending_settlement_amount), `${Number(kpis.pending_settlement_count || 0)}건`],
      ["처리 대기", `${Number(kpis.pending_tasks || 0)}건`, `가입 ${Number(kpis.pending_joins || 0)} · 서류 ${Number(kpis.pending_documents || 0)} · 계약 ${Number(kpis.pending_contracts || 0)}`]
    ].map(([label, value, note]) => `<article class="kpi v2-api-kpi"><small>${escapeHtml(label)}</small><strong>${escapeHtml(value)}</strong><span>${escapeHtml(note)}</span></article>`).join("");
    if (byId("taskQueueRows")) byId("taskQueueRows").innerHTML = tasks.length
      ? tasks.map(item => progress(text(item, "title"), text(item, "priority"))).join("")
      : '<div class="v2-empty">처리할 항목이 없습니다.</div>';
  }

  function fillItemOptions(items) {
    ["inventoryOrderItem", "stocktakeItem"].forEach(id => {
      const select = byId(id);
      if (!select) return;
      const chosen = select.value;
      select.innerHTML = ['<option value="">품목 선택</option>'].concat(items.map(item =>
        `<option value="${escapeHtml(item.id)}">${escapeHtml(text(item, "name"))} (현재 ${escapeHtml(number(item.current_stock))}${escapeHtml(text(item, "unit"))})</option>`
      )).join("");
      if (chosen && items.some(item => String(item.id) === chosen)) select.value = chosen;
    });
  }

  async function renderInventory() {
    const [{ items = [] }, { orders = [] }, { stock_balances: balances = [] }] = await Promise.all([
      request(`${API.inventory}/items`),
      request(`${API.inventory}/orders`),
      request(`${API.inventory}/stock-balances`, { limit: 20 })
    ]);
    const low = items.filter(item => Number(item.min_stock || 0) > Number(item.current_stock || 0));
    byId("inventorySummaryRows").innerHTML = [
      progress("등록 품목", `${items.length}개`),
      progress("부족 재고", `${low.length}개`),
      progress("발주", `${orders.length}건`),
      progress("최근 실사", balances.length ? String(text(balances[0], "counted_at")).slice(0, 10) : "기록 없음")
    ].join("");

    fillItemOptions(items);
    restoreDraft();

    byId("inventoryRows").innerHTML = orders.length ? orders.map(row => {
      const received = String(text(row, "status")) === "received";
      const action = received
        ? `<span class="badge good">입고완료</span>`
        : `<button type="button" class="v2-touch" data-receive-order="${escapeHtml(row.id)}">입고 확인</button>`;
      const amount = text(row, "total_amount", "amount", "total_cost");
      return `<tr><td>${escapeHtml(text(row, "order_date", "created_at"))}</td><td>${escapeHtml(text(row, "supplier", "vendor"))}</td><td>${escapeHtml(text(row, "supplier_type"))}</td><td class="num">${escapeHtml(money(amount === "-" ? 0 : amount))}</td><td><span class="badge info">${escapeHtml(text(row, "status"))}</span></td><td>${action}</td></tr>`;
    }).join("") : emptyRow(6);

    const badge = byId("inventoryStocktakeBadge");
    if (badge) badge.textContent = `실사 ${balances.length}건`;
    byId("stockBalanceRows").innerHTML = balances.length ? balances.map(row => {
      const diff = Number(row.difference || 0);
      const diffClass = diff === 0 ? "info" : (diff > 0 ? "good" : "warn");
      return `<tr><td>${escapeHtml(String(text(row, "counted_at")).slice(0, 16).replace("T", " "))}</td><td>${escapeHtml(text(row, "item_name", "item_id"))}</td><td class="num">${escapeHtml(number(row.system_quantity))}</td><td class="num">${escapeHtml(number(row.counted_quantity))}</td><td class="num"><span class="badge ${diffClass}">${diff > 0 ? "+" : ""}${escapeHtml(number(diff))}</span></td><td>${escapeHtml(text(row, "counted_by"))}</td></tr>`;
    }).join("") : emptyRow(6, "재고 실사 기록이 없습니다.");
  }

  async function submitOrder(event) {
    event.preventDefault();
    const form = orderForm();
    const submit = byId("inventoryOrderSubmit");
    const itemId = form.elements.item_id?.value || "";
    const quantity = Number(form.elements.quantity?.value || 0);
    if (!itemId || !(quantity > 0)) {
      setStatus("error", "품목과 0보다 큰 수량을 입력하세요.");
      return;
    }
    if (submit) submit.disabled = true;
    try {
      await send(`${API.inventory}/orders`, {
        ...scope(),
        supplier: form.elements.supplier?.value || "",
        status: "ordered",
        total_amount: Number(form.elements.total_amount?.value || 0),
        memo: form.elements.memo?.value || "",
        items: [{ item_id: itemId, quantity }]
      });
      form.reset();
      clearDraft();
      await refresh("inventory");
      setStatus("ready", "발주를 등록했습니다.");
    } catch (error) {
      if (error.status === 401) return recoverExpiredSession();
      setStatus("error", error.message || "발주를 등록하지 못했습니다.");
    } finally {
      if (submit) submit.disabled = false;
    }
  }

  async function submitStocktake(event) {
    event.preventDefault();
    const form = byId("inventoryStocktakeForm");
    const submit = byId("stocktakeSubmit");
    const itemId = form.elements.item_id?.value || "";
    const raw = form.elements.counted_quantity?.value ?? "";
    const counted = Number(raw);
    if (!itemId || raw === "" || !(counted >= 0)) {
      setStatus("error", "품목과 0 이상의 실사 수량을 입력하세요.");
      return;
    }
    if (submit) submit.disabled = true;
    try {
      const result = await send(`${API.inventory}/items/${encodeURIComponent(itemId)}/stocktake`, {
        counted_quantity: counted,
        memo: form.elements.memo?.value || ""
      });
      form.reset();
      await refresh("inventory");
      const diff = Number(result?.balance?.difference || 0);
      setStatus("ready", diff === 0 ? "실사 결과가 장부와 일치합니다." : `실사 반영 완료 (차이 ${diff > 0 ? "+" : ""}${number(diff)})`);
    } catch (error) {
      if (error.status === 401) return recoverExpiredSession();
      setStatus("error", error.message || "실사를 반영하지 못했습니다.");
    } finally {
      if (submit) submit.disabled = false;
    }
  }

  async function receiveOrder(orderId, button) {
    button.disabled = true;
    try {
      await send(`${API.inventory}/orders/${encodeURIComponent(orderId)}/receive`, {});
      await refresh("inventory");
      setStatus("ready", "입고 처리했습니다. 재고에 반영되었습니다.");
    } catch (error) {
      if (error.status === 401) return recoverExpiredSession();
      setStatus("error", error.message || "입고 처리를 하지 못했습니다.");
      button.disabled = false;
    }
  }

  async function renderAccounting() {
    const [summary, { tax_reports: reports = [] }] = await Promise.all([
      request(`${API.accounting}/summary`), request(`${API.accounting}/tax-reports`)
    ]);
    byId("taxSummaryRows").innerHTML = [progress("매출", money(summary.total_sales)), progress("매입", money(summary.total_purchases)), progress("비용", money(summary.total_expenses)), progress("영업손익", money(summary.operating_profit))].join("");
    byId("taxLedgerRows").innerHTML = reports.length ? reports.map(row => `<tr><td>${escapeHtml(text(row, "report_type"))}</td><td>${escapeHtml(text(row, "period_start"))} ~ ${escapeHtml(text(row, "period_end"))}</td><td>${escapeHtml(text(row, "filing_label", "status"))}</td><td>신고서 검토</td></tr>`).join("") : emptyRow(4, "등록된 세무 신고 자료가 없습니다.");
    if (byId("taxLedgerBadge")) byId("taxLedgerBadge").textContent = `실DB ${reports.length}건`;
  }

  async function renderOps(view) {
    const path = view === "approvals" ? "approvals" : view === "alerts" ? "notifications" : "audit-logs";
    const data = await request(`${API.ops}/${path}`);
    if (view === "approvals") {
      const rows = data.approvals || [];
      byId("approvalSummaryBadge").textContent = `${rows.length}건`;
      byId("approvalRows").innerHTML = rows.length ? rows.map(row => `<tr><td>${escapeHtml(text(row, "approval_type"))}</td><td>${escapeHtml(text(row, "title"))}</td><td><span class="badge info">${escapeHtml(text(row, "status"))}</span></td><td>${escapeHtml(text(row, "requested_by"))}</td></tr>`).join("") : emptyRow(4, "승인 대기 항목이 없습니다.");
    } else if (view === "alerts") {
      const rows = data.notifications || [];
      byId("alertCenterBadge").textContent = `${rows.length}건`;
      byId("alertCenterRows").innerHTML = rows.length ? rows.map(row => progress(text(row, "title"), text(row, "created_at"))).join("") : '<div class="v2-empty">새 알림이 없습니다.</div>';
    } else {
      const rows = data.audit_logs || [];
      byId("auditRows").innerHTML = rows.length ? rows.map(row => `<tr><td>${escapeHtml(text(row, "created_at"))}</td><td>${escapeHtml(text(row, "action"))}</td><td>${escapeHtml(text(row, "resource_type"))}</td><td>${escapeHtml(text(row, "actor"))}</td></tr>`).join("") : emptyRow(4, "기록된 운영 흔적이 없습니다.");
    }
  }

  function currentView() {
    if (mounted) return MOUNTED_VIEW;
    return document.querySelector('.side-nav [data-view].active')?.dataset.view || "dashboard";
  }

  async function refresh(view) {
    if (mounted) view = MOUNTED_VIEW;
    if (!showLivebarFor(view)) return;
    activeController?.abort();
    activeController = new AbortController();
    setStatus("loading", "실데이터 불러오는 중");
    try {
      if (view === MOUNTED_VIEW) {
        // 한 영역이 실패해도 나머지는 그린다. 실패는 상태줄에 첫 원인을 남긴다.
        const failed = (await Promise.allSettled([renderDashboard(), renderInventory(), renderAccounting()])).find(item => item.status === "rejected");
        if (failed) throw failed.reason;
      } else if (view === "dashboard" || view === "tasks") await renderDashboard();
      else if (view === "inventory") await renderInventory();
      else if (view === "tax") await renderAccounting();
      else await renderOps(view);
      setStatus("ready", "실데이터 연결됨");
    } catch (error) {
      if (error.name === "AbortError") return;
      if (error.status === 401) return recoverExpiredSession(error.message);
      setStatus("error", error.message || "데이터를 불러오지 못했습니다.");
    }
  }

  function addBottomNav() {
    const nav = document.createElement("nav");
    nav.className = "v2-bottom-nav app-only hidden";
    nav.setAttribute("aria-label", "모바일 주요 화면");
    [["dashboard", "통합 홈"], ["inventory", "매입·재고"], ["tax", "회계·세무"], ["approvals", "승인"], ["alerts", "알림"]].forEach(([view, label]) => {
      const button = document.createElement("button");
      button.type = "button";
      button.dataset.view = view;
      button.textContent = label;
      nav.append(button);
    });
    document.body.append(nav);
  }

  function accessibility() {
    document.querySelectorAll(".table-wrap").forEach(wrap => {
      wrap.tabIndex = 0;
      wrap.setAttribute("role", "region");
      wrap.setAttribute("aria-label", "가로 스크롤 표");
    });
    document.querySelectorAll('.modal-backdrop [role="dialog"]').forEach(dialog => dialog.setAttribute("tabindex", "-1"));
    document.addEventListener("keydown", event => {
      if (event.key !== "Escape") return;
      const open = [...document.querySelectorAll('.modal-backdrop:not(.hidden), .overlay.open')].pop();
      if (!open) return;
      open.querySelector('[id^="close"], .icon-close')?.click();
      lastFocus?.focus();
    });
    document.addEventListener("click", event => {
      if (event.target.closest('[aria-haspopup="dialog"], [data-preview-contract], [data-sign-contract]')) lastFocus = event.target.closest("button, a");
    }, true);
  }

  function bindInventoryForms() {
    orderForm()?.addEventListener("submit", submitOrder);
    orderForm()?.addEventListener("input", saveDraft);
    orderForm()?.addEventListener("change", saveDraft);
    byId("inventoryOrderReset")?.addEventListener("click", () => {
      orderForm()?.reset();
      clearDraft();
    });
    byId("inventoryStocktakeForm")?.addEventListener("submit", submitStocktake);
    document.addEventListener("click", event => {
      const button = event.target.closest("[data-receive-order]");
      if (button) receiveOrder(button.dataset.receiveOrder, button);
    });
  }

  // ── 새 관리화면용 매장비서 화면 ──────────────────────────────
  // 기존 매장비서와 같은 API·같은 함수(대시보드·발주·실사·입고·회계 요약)를 쓰고, 화면 뼈대만 여기서 그린다.
  function mountedHtml() {
    const table = (head, body) => `<div class="table-wrap" tabindex="0" role="region" aria-label="가로 스크롤 표"><table><thead><tr>${head.map(([label, num]) => `<th${num ? ' class="num"' : ""}>${escapeHtml(label)}</th>`).join("")}</tr></thead><tbody id="${body}"></tbody></table></div>`;
    return `<div class="v2-livebar" role="status" aria-live="polite"><strong>실데이터</strong><span data-v2-state="loading">연동 확인 중</span><span data-v2-sync>마지막 동기화: -</span><button type="button" data-v2-retry hidden>재시도</button></div>
      <section class="sa-block" aria-labelledby="saTodayTitle"><h2 id="saTodayTitle">오늘 현황</h2><div class="sa-kpis" id="kpiGrid"></div></section>
      <section class="sa-block" aria-labelledby="saTaskTitle"><h2 id="saTaskTitle">처리 대기</h2><div class="sa-progress" id="taskQueueRows"></div></section>
      <section class="sa-block" aria-labelledby="saInventoryTitle"><h2 id="saInventoryTitle">재고 요약</h2><div class="sa-progress" id="inventorySummaryRows"></div></section>
      <div class="sa-grid">
        <section class="sa-block" aria-labelledby="saOrderTitle"><h2 id="saOrderTitle">발주 작성</h2><p class="sa-muted" id="inventoryDraftState">작성 중인 발주는 자동 보관됩니다.</p>
          <form id="inventoryOrderForm" class="sa-form" autocomplete="off">
            <label>매입처<input type="text" name="supplier" placeholder="예: 마켓봄"></label>
            <label>품목<select name="item_id" id="inventoryOrderItem"><option value="">품목 선택</option></select></label>
            <label>수량<input type="number" inputmode="decimal" min="0" step="0.01" name="quantity" placeholder="0"></label>
            <label>금액<input type="number" inputmode="numeric" min="0" step="1" name="total_amount" placeholder="0"></label>
            <label class="sa-wide">메모<input type="text" name="memo" placeholder="배송 요청 등"></label>
            <div class="sa-actions"><button type="submit" class="btn primary" id="inventoryOrderSubmit">발주 등록</button><button type="button" class="btn" id="inventoryOrderReset">작성 내용 비우기</button></div>
          </form></section>
        <section class="sa-block" aria-labelledby="saStocktakeTitle"><h2 id="saStocktakeTitle">재고 실사 <span class="sa-muted" id="inventoryStocktakeBadge">실사 0건</span></h2>
          <form id="inventoryStocktakeForm" class="sa-form" autocomplete="off">
            <label>품목<select name="item_id" id="stocktakeItem"><option value="">품목 선택</option></select></label>
            <label>실사 수량<input type="number" inputmode="decimal" min="0" step="0.01" name="counted_quantity" placeholder="0"></label>
            <label class="sa-wide">메모<input type="text" name="memo" placeholder="실사 사유"></label>
            <div class="sa-actions"><button type="submit" class="btn primary" id="stocktakeSubmit">실사 반영</button></div>
          </form>
          ${table([["실사일"], ["품목"], ["장부", 1], ["실사", 1], ["차이", 1], ["실행자"]], "stockBalanceRows")}</section>
      </div>
      <section class="sa-block" aria-labelledby="saOrdersTitle"><h2 id="saOrdersTitle">매입·발주 근거</h2>${table([["일자"], ["매입처"], ["채널"], ["금액", 1], ["상태"], ["입고"]], "inventoryRows")}</section>
      <section class="sa-block" aria-labelledby="saTaxTitle"><h2 id="saTaxTitle">회계 요약 <span class="sa-muted" id="taxLedgerBadge">실DB 0건</span></h2><div class="sa-progress" id="taxSummaryRows"></div>${table([["신고 종류"], ["기간"], ["상태"], ["다음 할 일"]], "taxLedgerRows")}</section>`;
  }

  // options: { businessId, onExpired() }
  function mount(element, options = {}) {
    if (!element) return false;
    activeController?.abort();
    activeController = null;
    mounted = { element, businessId: String(options.businessId || ""), onExpired: options.onExpired };
    element.innerHTML = mountedHtml();
    element.querySelector("[data-v2-retry]")?.addEventListener("click", () => refresh(MOUNTED_VIEW));
    const form = orderForm();
    form?.addEventListener("submit", submitOrder);
    form?.addEventListener("input", saveDraft);
    form?.addEventListener("change", saveDraft);
    byId("inventoryOrderReset")?.addEventListener("click", () => { form?.reset(); clearDraft(); });
    byId("inventoryStocktakeForm")?.addEventListener("submit", submitStocktake);
    element.addEventListener("click", event => {
      const button = event.target.closest("[data-receive-order]");
      if (button) receiveOrder(button.dataset.receiveOrder, button);
    });
    refresh(MOUNTED_VIEW);
    return true;
  }

  window.ObysStoreAssistant = { mount };

  if (!HOST_MODE) document.addEventListener("DOMContentLoaded", () => {
    document.body.classList.add("v2-enhanced");
    livebar();
    addBottomNav();
    document.querySelector(".v2-bottom-nav")?.classList.toggle("hidden", document.body.classList.contains("signed-out"));
    accessibility();
    bindInventoryForms();
    document.addEventListener("click", event => {
      const view = event.target.closest("[data-view]")?.dataset.view;
      if (view) queueMicrotask(() => {
        document.querySelectorAll(".v2-bottom-nav [data-view]").forEach(button => button.classList.toggle("active", button.dataset.view === view));
        refresh(view);
      });
    });
    [byId("monthFilter"), byId("branchFilter")].forEach(control => control?.addEventListener("change", () => refresh(currentView())));
    const authObserver = new MutationObserver(() => {
      document.querySelector(".v2-bottom-nav")?.classList.toggle("hidden", document.body.classList.contains("signed-out"));
      if (!document.body.classList.contains("signed-out")) refresh(currentView());
    });
    authObserver.observe(document.body, { attributes: true, attributeFilter: ["class"] });
    if (!document.body.classList.contains("signed-out")) refresh(currentView());
  });
})();
