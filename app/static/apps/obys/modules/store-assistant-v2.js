(() => {
  "use strict";

  const API = {
    dashboard: "/api/v1/yeoljeong-dashboard",
    inventory: "/api/v1/yeoljeong-inventory",
    accounting: "/api/v1/yeoljeong-accounting",
    ops: "/api/v1/yeoljeong-ops"
  };
  const TOKEN_KEYS = ["aads_token", "fb_access_token"];
  const connectedViews = new Set(["dashboard", "tasks", "inventory", "tax", "approvals", "alerts", "audit"]);
  let activeController = null;
  let lastFocus = null;

  const byId = id => document.getElementById(id);
  const escapeHtml = value => String(value ?? "").replace(/[&<>'"]/g, char => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;"
  })[char]);
  const money = value => `${Number(value || 0).toLocaleString("ko-KR")}원`;
  const text = (row, ...keys) => keys.map(key => row?.[key]).find(value => value !== undefined && value !== null && value !== "") ?? "-";
  const token = () => TOKEN_KEYS.map(key => localStorage.getItem(key)).find(Boolean) || "";

  function scope() {
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

  async function request(path, extra = {}) {
    const authToken = token();
    if (!authToken) throw Object.assign(new Error("로그인이 필요합니다."), { status: 401 });
    const response = await fetch(`${path}?${query(extra)}`, {
      headers: { Authorization: `Bearer ${authToken}`, Accept: "application/json" },
      signal: activeController?.signal
    });
    if (!response.ok) {
      const labels = { 401: "로그인이 만료되었습니다.", 403: "이 화면을 볼 권한이 없습니다." };
      throw Object.assign(new Error(labels[response.status] || `서버 응답 오류 (${response.status})`), { status: response.status });
    }
    return response.json();
  }

  function livebar() {
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
    const state = bar.querySelector("[data-v2-state]");
    state.dataset.v2State = kind;
    state.textContent = message;
    if (kind === "ready") bar.querySelector("[data-v2-sync]").textContent = `마지막 동기화: ${new Date().toLocaleString("ko-KR")}`;
    bar.querySelector("[data-v2-retry]").hidden = kind !== "error";
  }

  const emptyRow = (colspan, message = "연결된 데이터가 없습니다.") => `<tr><td colspan="${colspan}"><div class="v2-empty">${escapeHtml(message)}</div></td></tr>`;
  const progress = (label, value) => `<div class="progress-item"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></div>`;

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

  async function renderInventory() {
    const [{ items = [] }, { orders = [] }] = await Promise.all([
      request(`${API.inventory}/items`), request(`${API.inventory}/orders`)
    ]);
    const low = items.filter(item => Number(item.min_stock || 0) > Number(item.current_stock || 0));
    byId("inventorySummaryRows").innerHTML = [progress("등록 품목", `${items.length}개`), progress("부족 재고", `${low.length}개`), progress("발주", `${orders.length}건`)].join("");
    byId("inventoryRows").innerHTML = orders.length ? orders.map(row => `<tr><td>${escapeHtml(text(row, "order_date", "created_at"))}</td><td>${escapeHtml(text(row, "supplier", "vendor"))}</td><td>실DB 발주</td><td class="num">${escapeHtml(money(text(row, "total_amount", "amount", "total_cost") === "-" ? 0 : text(row, "total_amount", "amount", "total_cost")))}</td><td><span class="badge info">${escapeHtml(text(row, "status"))}</span></td></tr>`).join("") : emptyRow(5);
  }

  async function renderAccounting() {
    const [summary, { tax_reports: reports = [] }] = await Promise.all([
      request(`${API.accounting}/summary`), request(`${API.accounting}/tax-reports`)
    ]);
    byId("taxSummaryRows").innerHTML = [progress("매출", money(summary.total_sales)), progress("매입", money(summary.total_purchases)), progress("비용", money(summary.total_expenses)), progress("영업손익", money(summary.operating_profit))].join("");
    byId("taxLedgerRows").innerHTML = reports.length ? reports.map(row => `<tr><td>${escapeHtml(text(row, "report_type"))}</td><td>${escapeHtml(text(row, "period_start"))} ~ ${escapeHtml(text(row, "period_end"))}</td><td>${escapeHtml(text(row, "status"))}</td><td>신고서 검토</td></tr>`).join("") : emptyRow(4, "등록된 세무 신고 자료가 없습니다.");
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
    return document.querySelector('.side-nav [data-view].active')?.dataset.view || "dashboard";
  }

  async function refresh(view) {
    if (!connectedViews.has(view) || document.body.classList.contains("signed-out")) return;
    activeController?.abort();
    activeController = new AbortController();
    setStatus("loading", "실데이터 불러오는 중");
    try {
      if (view === "dashboard" || view === "tasks") await renderDashboard();
      else if (view === "inventory") await renderInventory();
      else if (view === "tax") await renderAccounting();
      else await renderOps(view);
      setStatus("ready", "실데이터 연결됨");
    } catch (error) {
      if (error.name === "AbortError") return;
      setStatus("error", error.message || "데이터를 불러오지 못했습니다.");
    }
  }

  function addBottomNav() {
    const nav = document.createElement("nav");
    nav.className = "v2-bottom-nav app-only hidden";
    nav.setAttribute("aria-label", "모바일 주요 화면");
    [["dashboard", "통합 홈"], ["inventory", "재고·발주"], ["tax", "회계·세무"], ["approvals", "승인"], ["alerts", "알림"]].forEach(([view, label]) => {
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

  document.addEventListener("DOMContentLoaded", () => {
    document.body.classList.add("v2-enhanced");
    livebar();
    addBottomNav();
    document.querySelector(".v2-bottom-nav")?.classList.toggle("hidden", document.body.classList.contains("signed-out"));
    accessibility();
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
