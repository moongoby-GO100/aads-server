/* 오비서 V4.1 새 관리화면(admin.html) — 기존 관리화면에만 있던 업무 화면을 옮겨 온다.
 *
 *  - 자료 수집 현황  : modules/clobe-collection.js 의 화면을 mount() 로 띄운다.
 *  - 원장 상세       : modules/ledger-details.js 의 화면을 mount() 로 띄운다.
 *  - 매장비서        : modules/store-assistant-v2.js 의 화면을 mount() 로 띄운다.
 *  - 월마감          : 서버 집계 API 만으로 마감 점검표와 리포트를 만든다(쓰기 없음).
 *  - 기초 설정·계좌 등록 : 서버 기초설정(/settings)을 읽고, 계좌 등록만 기존 저장 경로로 보낸다.
 *
 * 기존 관리화면(index.html)의 전역 함수·화면 요소·로그인 저장값을 쓰지 않는다.
 * 화면 상태 안내·이동·세션 만료 처리는 admin.html 이 넘기는 ctx 로만 한다.
 * 데이터는 기존 서버 API 만 쓰며, 쓰기 권한·승인 판정은 서버가 한다(여기서 완화하지 않는다).
 */
(() => {
  "use strict";

  const ROUTES = {
    "account-master": {
      title: "계정과목",
      desc: "현재 회사의 계정코드·계정과목·출처·활성 여부를 조회합니다."
    },
    "store-assistant": {
      title: "매장비서",
      desc: "오늘 매출·처리 대기·재고·회계 요약을 한 화면에서 보고 발주·재고 실사·입고를 처리합니다."
    },
    "ledger-details": {
      title: "원장 상세",
      desc: "매출·매입·통장·카드 원장을 사업자별로 조회하고, 수기 거래와 엑셀 원장을 관리합니다.",
      tabs: [["salesLedger", "매출 상세"], ["purchaseLedger", "매입 상세"], ["bankLedger", "통장 거래내역"], ["cardLedger", "카드 사용내역"]]
    },
    "month-close": {
      title: "월마감",
      desc: "이번 달 매출·정산·검토·승인·근태 정정 상태를 점검하고 마감 리포트를 만듭니다. 매출·근태 정정은 선택 월, 정산·검토·승인은 현재 남은 전체 건으로 봅니다. 조회 전용입니다."
    },
    "data-collection": {
      title: "자료 수집 현황",
      desc: "은행·세금계산서·현금영수증·카드 자료의 수집 상태를 보고, 수집된 거래를 검토·확정합니다.",
      tabs: [["clobeOverview", "자료 현황"], ["clobeReview", "검토할 거래"], ["clobeSummary", "경영 요약"], ["clobeSettings", "자료 연결"]]
    },
    "basic-settings": {
      title: "기초 설정·계좌 등록",
      desc: "사업자·지점·정산 계좌·직원 기본정보 등 모든 입력의 기준값을 확인하고 정산 계좌를 등록합니다."
    }
  };
  // 새 화면 메뉴 경로 → 기존 화면 이름. 모듈 화면 안의 "이동" 버튼을 새 화면 메뉴로 바꾼다.
  const ROUTE_ALIASES = { documents: "documents", integrations: "integrations", business: "businesses", employees: "employees", approvals: "approvals" };
  const TAB_KEY = "obys_v41_extra_tabs";

  const tabs = loadTabs();

  function loadTabs() {
    try { return JSON.parse(sessionStorage.getItem(TAB_KEY) || "{}") || {}; } catch (_) { return {}; }
  }

  function saveTabs() {
    try { sessionStorage.setItem(TAB_KEY, JSON.stringify(tabs)); } catch (_) { /* 저장 불가 환경은 화면 상태만 유지 */ }
  }

  function esc(value) {
    return String(value ?? "").replace(/[&<>'"]/g, ch => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" })[ch]);
  }

  const won = value => `${Math.round(Number(value || 0)).toLocaleString("ko-KR")}원`;
  const count = value => `${Number(value || 0).toLocaleString("ko-KR")}건`;

  function tabOf(route) {
    const list = ROUTES[route]?.tabs || [];
    return list.some(([key]) => key === tabs[route]) ? tabs[route] : (list[0]?.[0] || "");
  }

  // ── 상태 안내(0건·오류·권한·만료) ────────────────────────────
  // 기존 관리화면의 판정 순서를 그대로 옮겼다: 401 → 403 → 그 외 오류 → 검토 대기 → 미연결 → 기간 → 화면별 원인.
  function errorReason(error) {
    const status = Number(error?.status || 0);
    const message = String(error?.message || "");
    if (status === 504 || /지연|timeout|Abort/i.test(message)) return "서버 응답이 늦습니다. 잠시 후 다시 시도하십시오.";
    if (status >= 500) return "서버에 일시적인 문제가 있습니다. 잠시 후 다시 시도하십시오.";
    if (status === 404) return "요청한 자료를 찾을 수 없습니다. 선택한 사업자를 확인하십시오.";
    if (!status && /fetch|network|Failed/i.test(message)) return "네트워크에 연결할 수 없습니다. 인터넷 연결을 확인하십시오.";
    return message && !/^(API|HTTP) \d+$/.test(message) && message !== "http" ? message : "자료를 불러오지 못했습니다.";
  }

  function viewStateConfig(view, ctx = {}, env = {}) {
    const retry = ctx.onRetry || env.retry;
    const error = ctx.error || null;
    const status = Number(error?.status || 0);
    if (error && status === 401) {
      return { kind: "expired", title: "로그인이 만료되었습니다", reason: "다시 로그인하면 이어서 볼 수 있습니다.", actions: [{ label: "다시 로그인", primary: true, onClick: env.login }] };
    }
    if (error && status === 403) {
      return { kind: "forbidden", title: "이 사업자 자료를 볼 권한이 없습니다.", reason: ctx.forbiddenReason || "현재 계정의 역할로는 이 화면을 볼 수 없습니다. 대표·관리자에게 권한을 요청하십시오.", actions: [] };
    }
    if (error) {
      return { kind: "error", title: "불러오지 못했습니다", reason: ctx.errorReason || errorReason(error), code: status ? String(status) : "", actions: retry ? [{ label: "다시 시도", primary: true, onClick: retry }] : [] };
    }
    const pending = Number(ctx.pendingReview || 0);
    if (pending > 0) {
      return { kind: "empty", title: "확정된 거래가 아직 없습니다.", reason: `수집된 ${pending.toLocaleString("ko-KR")}건이 검토를 기다리고 있습니다.`, actions: [{ label: "검토할 거래 열기", primary: true, onClick: ctx.onReview, view: ctx.onReview ? undefined : "clobeReview" }] };
    }
    if (ctx.connected === false) {
      return {
        kind: "empty",
        title: `${ctx.source || "자료"} 연결이 필요합니다.`,
        reason: "연결하면 자동으로 모입니다. 지금 바로 보려면 파일을 올리십시오.",
        actions: [
          { label: "연결하기", primary: true, view: ctx.connectView || "integrations" },
          { label: "파일 올리기", onClick: ctx.onUpload, view: ctx.onUpload ? undefined : "documents" }
        ]
      };
    }
    if (ctx.periodFiltered) {
      return { kind: "empty", title: "선택한 기간에 거래가 없습니다.", reason: "기간을 넓히면 다른 거래를 볼 수 있습니다.", actions: [{ label: "최근 30일 보기", primary: true, onClick: ctx.onRecent || retry }] };
    }
    if (ctx.emptyTitle) {
      return { kind: "empty", title: ctx.emptyTitle, reason: ctx.emptyReason || "", actions: ctx.emptyActions || (retry ? [{ label: "새로고침", onClick: retry }] : []) };
    }
    return { kind: "empty", title: "자료가 없습니다", reason: "조회 결과가 0건입니다. 새로 들어온 자료가 있는지 다시 확인하십시오.", actions: retry ? [{ label: "새로고침", primary: true, onClick: retry }] : [] };
  }

  const STATE_ROLE = { empty: "status", error: "alert", forbidden: "status", expired: "alert", loading: "status" };

  function renderStateInto(el, cfg = {}, goView) {
    if (!el) return;
    const kind = STATE_ROLE[cfg.kind] ? cfg.kind : "empty";
    const actions = Array.isArray(cfg.actions) ? cfg.actions.filter(a => a && (typeof a.onClick === "function" || a.view)) : [];
    el.innerHTML = `<div class="obx-state ${kind}" role="${STATE_ROLE[kind]}">`
      + `<strong>${esc(cfg.title || "자료가 없습니다")}</strong>`
      + (cfg.reason ? `<span>${esc(cfg.reason)}</span>` : "")
      + (cfg.code ? `<small>오류 번호 ${esc(cfg.code)}</small>` : "")
      + (actions.length ? `<div class="obx-state-actions">${actions.map(a => `<button type="button" class="btn${a.primary ? " primary" : ""}">${esc(a.label)}</button>`).join("")}</div>` : "")
      + `</div>`;
    el.querySelectorAll(".obx-state-actions button").forEach((button, i) => {
      const action = actions[i];
      button.addEventListener("click", event => {
        if (typeof action.onClick === "function") action.onClick(event);
        else if (action.view && typeof goView === "function") goView(action.view);
      });
    });
  }

  // ── 서버 요청 ───────────────────────────────────────────────
  async function api(ctx, path, options = {}) {
    const headers = { Accept: "application/json", ...(typeof ctx.authHeaders === "function" ? ctx.authHeaders() : {}), ...(options.headers || {}) };
    const response = await fetch(path, { credentials: "same-origin", ...options, headers });
    let body = {};
    try { body = (await response.json()) || {}; } catch (_) { /* 본문 없는 응답 */ }
    if (!response.ok) {
      const detail = body.detail;
      const error = new Error(typeof detail === "string" ? detail : detail?.message || detail?.reason || `HTTP ${response.status}`);
      error.status = response.status;
      if (response.status === 401 && typeof ctx.onExpired === "function") ctx.onExpired();
      throw error;
    }
    return body;
  }

  const settled = promise => promise.then(data => ({ ok: true, data }), error => ({ ok: false, error }));

  // ── 계정과목 조회 ──────────────────────────────────────────
  function renderAccountMaster(body, ctx, env) {
    let requestId = 0;
    let offset = 0;
    let query = "";
    const limit = 100;
    body.innerHTML = `<form class="toolbar" data-account-search>
      <label>코드·이름 검색 <input class="field" name="q" type="search" maxlength="200" placeholder="계정코드 또는 계정과목"></label>
      <button class="btn primary" type="submit">조회</button></form>
      <div data-account-list aria-live="polite"></div>`;
    const list = body.querySelector("[data-account-list]");
    async function load() {
      const seq = ++requestId;
      list.textContent = "계정과목을 불러오고 있습니다.";
      try {
        const params = new URLSearchParams({q: query, limit: String(limit), offset: String(offset)});
        const data = await api(ctx, `/api/v1/yeoljeong-finance/account-master?${params}`);
        if (seq !== requestId || !body.isConnected) return;
        const rows = data.accounts || [];
        list.innerHTML = rows.length ? `<p>${offset + 1}~${offset + rows.length}번째 계정과목</p>
          <div style="overflow-x:auto"><table><thead><tr><th>계정코드</th><th>계정과목</th><th>출처</th><th>기준 사업장</th><th>활성 여부</th></tr></thead>
          <tbody>${rows.map(row => `<tr><td>${esc(row.account_code)}</td><td>${esc(row.account_name)}</td><td>${esc(row.source === "clobe" ? "클로브" : row.source)}</td><td>${esc(row.source_company_label)}</td><td>${row.is_active ? "활성" : "비활성"}</td></tr>`).join("")}</tbody></table></div>`
          : `<div class="empty">${query ? "검색 결과가 없습니다." : "등록된 계정과목이 없습니다."}</div>`;
        list.insertAdjacentHTML("beforeend", `<div class="toolbar"><button class="btn" type="button" data-account-prev ${offset ? "" : "disabled"}>이전</button><button class="btn" type="button" data-account-next ${data.has_more ? "" : "disabled"}>다음</button></div>`);
        list.querySelector("[data-account-prev]").onclick = () => { offset = Math.max(0, offset - limit); load(); };
        list.querySelector("[data-account-next]").onclick = () => { offset += limit; load(); };
      } catch (error) {
        if (seq !== requestId || !body.isConnected) return;
        renderStateInto(list, viewStateConfig("account-master", {error, onRetry: load}, env));
      }
    }
    body.querySelector("[data-account-search]").onsubmit = event => {
      event.preventDefault();
      query = String(event.currentTarget.elements.q.value || "").trim();
      offset = 0;
      load();
    };
    load();
  }

  // ── 월마감 ──────────────────────────────────────────────────
  function kstMonth(now = new Date()) {
    return new Intl.DateTimeFormat("en-CA", { timeZone: "Asia/Seoul", year: "numeric", month: "2-digit" }).format(now).slice(0, 7);
  }

  function monthRange(month) {
    if (!/^\d{4}-\d{2}$/.test(String(month || ""))) return null;
    const [y, m] = month.split("-").map(Number);
    if (m < 1 || m > 12) return null;
    const last = new Date(Date.UTC(y, m, 0)).getUTCDate();
    return { from: `${month}-01`, to: `${month}-${String(last).padStart(2, "0")}` };
  }

  // 마감 점검에 쓰는 기존 조회 API. 기간을 받는 것은 회계 summary 하나뿐이다.
  // 정산(settlement-summary)·승인(approvals/count)·자료 수집(status)·근태 정정 목록은 기간 인자가 없어
  // "지금 남아 있는 전체" 를 돌려준다 — 그래서 점검표는 항목마다 범위(scope)를 따로 적는다.
  function closingPaths(businessId, month) {
    const range = monthRange(month);
    const bid = encodeURIComponent(businessId);
    return {
      summary: `/api/v1/yeoljeong-accounting/summary?business_id=${bid}&date_from=${range.from}&date_to=${range.to}`,
      settlement: `/api/v1/yeoljeong-dashboard/settlement-summary?business_id=${bid}`,
      approvals: `/api/v1/yeoljeong-ops/approvals/count?business_id=${bid}`,
      collections: "/api/v1/obys-collections/status",
      corrections: `/api/v1/workspaces/${bid}/attendance/corrections`
    };
  }

  // 서버 근태 정정 목록은 최신 500건까지만 준다(app/services/obys_attendance_corrections.py, LIMIT 500).
  // 이만큼 받았으면 그 이전 요청은 보이지 않으므로 "대기 0건" 으로 확정하지 않는다.
  const CORRECTIONS_LIST_LIMIT = 500;

  function jsonField(value) {
    if (value && typeof value === "object") return value;
    if (typeof value !== "string" || !value) return {};
    try { const parsed = JSON.parse(value); return parsed && typeof parsed === "object" ? parsed : {}; } catch (_) { return {}; }
  }

  // 정정 요청이 걸린 근무일들. 기존 근태 정정은 원래 근무일과 바꿀 근무일이 다를 수 있어 둘 다 본다.
  function correctionWorkDates(item) {
    return [jsonField(item?.requested).work_date, jsonField(item?.original).work_date]
      .map(value => String(value || "").slice(0, 10))
      .filter(value => /^\d{4}-\d{2}-\d{2}$/.test(value));
  }

  // 현재 상태는 과거 월의 완료 이력을 보장하지 않는다. 0건도 월별 판정 근거로 쓰지 않는다.
  function currentStateStatus() {
    return "hold";
  }

  // 조회 결과로 점검표를 만든다. 조회에 실패한 항목은 0 으로 두지 않고 "확인 불가"로 남긴다.
  // scope: "month" = 선택 월 자료로 판정, "all" = 기간 구분 없는 현재 상태로 판정.
  function closingChecks(businessId, results, month) {
    const r = results || {};
    const range = monthRange(month);
    const checks = [];
    const metrics = {};
    const add = (key, name, scope, status, detail) => checks.push({ key, name, scope, status, detail });

    if (r.summary?.ok) {
      const s = r.summary.data || {};
      Object.assign(metrics, { sales: Number(s.total_sales || 0), purchases: Number(s.total_purchases || 0), expenses: Number(s.total_expenses || 0), profit: Number(s.operating_profit || 0) });
      add("sales", "이번 달 매출 집계", "month", metrics.sales > 0 ? "pass" : "fail", metrics.sales > 0 ? won(metrics.sales) : "집계된 매출이 없습니다");
    } else add("sales", "이번 달 매출 집계", "month", "unknown", "회계 집계를 확인할 수 없습니다");

    if (r.settlement?.ok) {
      const rows = Array.isArray(r.settlement.data?.summary) ? r.settlement.data.summary : [];
      const pendingCount = rows.reduce((sum, row) => sum + Number(row.pending_count || 0), 0);
      const pendingAmount = rows.reduce((sum, row) => sum + Number(row.pending_amount || 0), 0);
      Object.assign(metrics, { pendingSettlementCount: pendingCount, pendingSettlementAmount: pendingAmount });
      add("settlement", "정산 미입금 0건", "all", currentStateStatus(pendingCount),
        pendingCount === 0 ? "현재 미입금 0건 — 선택 월 입금 완료 여부는 별도 확인하십시오" : `전체 기간 미입금 ${count(pendingCount)} · ${won(pendingAmount)} — 이 달 정산인지 확인하십시오`);
    } else add("settlement", "정산 미입금 0건", "all", "unknown", "정산 현황을 확인할 수 없습니다");

    if (r.collections?.ok) {
      const companies = (r.collections.data?.companies || []).filter(item => item.business_id === businessId && item.link_status === "linked");
      if (!companies.length) add("review", "검토 대기 거래 0건", "all", "skip", "자료 수집이 연결되지 않은 사업자입니다");
      else {
        const waiting = companies.reduce((sum, item) => sum + Number(item.item_stage_counts?.review_box || 0) + Number(item.item_stage_counts?.reviewed || 0), 0);
        metrics.reviewWaiting = waiting;
        add("review", "검토 대기 거래 0건", "all", currentStateStatus(waiting),
          waiting === 0 ? "현재 확정 전 0건 — 선택 월 확정 여부는 별도 확인하십시오" : `전체 기간 확정 전 ${count(waiting)} — 이 달 거래인지 확인하십시오`);
      }
    } else add("review", "검토 대기 거래 0건", "all", "unknown", "자료 수집 상태를 확인할 수 없습니다");

    if (r.approvals?.ok) {
      const pending = Number(r.approvals.data?.pending || 0);
      metrics.pendingApprovals = pending;
      add("approvals", "승인 대기 0건", "all", currentStateStatus(pending),
        pending === 0 ? "현재 승인 대기 0건 — 선택 월 승인 완료 여부는 별도 확인하십시오" : `전체 기간 승인 대기 ${count(pending)} — 이 달 건인지 확인하십시오`);
    } else add("approvals", "승인 대기 0건", "all", "unknown", "승인함을 확인할 수 없습니다");

    if (r.corrections?.ok && Array.isArray(r.corrections.data?.items)) {
      const items = r.corrections.data.items;
      const truncated = items.length >= CORRECTIONS_LIST_LIMIT;
      const pendingAll = items.filter(item => item.status === "pending");
      const undated = pendingAll.filter(item => !correctionWorkDates(item).length);
      const inMonth = pendingAll.filter(item => correctionWorkDates(item).some(day => range && day >= range.from && day <= range.to));
      const otherMonths = pendingAll.length - undated.length - inMonth.length;
      metrics.pendingCorrections = inMonth.length;
      if (inMonth.length) add("corrections", "근태 정정 대기 0건", "month", "fail", truncated ? `이 달 근무 정정 대기 최소 ${count(inMonth.length)}` : `이 달 근무 정정 대기 ${count(inMonth.length)}`);
      else if (truncated) add("corrections", "근태 정정 대기 0건", "month", "unknown", `최근 ${count(CORRECTIONS_LIST_LIMIT)}만 조회되어 그 이전 정정 요청을 확인할 수 없습니다`);
      else if (!range) add("corrections", "근태 정정 대기 0건", "month", "unknown", "마감 월이 올바르지 않습니다");
      else if (undated.length) add("corrections", "근태 정정 대기 0건", "month", "unknown", `근무일을 알 수 없는 정정 대기 ${count(undated.length)}`);
      else add("corrections", "근태 정정 대기 0건", "month", "pass", otherMonths ? `이 달 0건 (다른 달 대기 ${count(otherMonths)})` : "0건");
    } else add("corrections", "근태 정정 대기 0건", "month", "unknown", "근태 정정 요청을 확인할 수 없습니다");

    const counted = checks.filter(item => item.status !== "skip");
    const verdict = counted.some(item => item.status === "fail") ? "추가 확인 필요"
      : counted.some(item => item.status === "unknown") ? "일부 자료를 확인하지 못해 판정 보류"
        : counted.some(item => item.status === "hold") ? "기간 구분 없는 현재 상태는 선택 월 확인 후 판정" : "마감 가능";
    return { checks, metrics, verdict };
  }

  const CLOSING_STATUS_LABEL = { pass: "통과", fail: "확인 필요", unknown: "확인 불가", skip: "해당 없음", hold: "해당 월 확인 필요" };
  const CLOSING_SCOPE_LABEL = { month: "선택 월", all: "현재 상태·기간 미구분" };

  function closingReport(month, businessName, result) {
    const m = result.metrics;
    const has = key => Object.prototype.hasOwnProperty.call(m, key);
    const money = key => (has(key) ? won(m[key]) : "확인 불가");
    const rate = key => (has(key) && has("sales") && m.sales ? `${(m[key] / m.sales * 100).toFixed(1)}%` : "-");
    return [
      `[${month} ${businessName || "선택 사업자"} 월마감 리포트]`,
      "",
      `매출: ${money("sales")}`,
      `매입: ${money("purchases")} / 매입률 ${rate("purchases")}`,
      `비용: ${money("expenses")}`,
      `영업손익: ${money("profit")}`,
      "",
      ...result.checks.map(item => `${item.name} [${CLOSING_SCOPE_LABEL[item.scope] || "-"}]: ${CLOSING_STATUS_LABEL[item.status] || "확인 불가"} (${item.detail})`),
      "",
      "※ 정산·검토·승인은 기간 구분 없이 현재 남은 전체 건수입니다. 0건이 아니면 해당 월 건인지 확인하십시오.",
      `마감 판정: ${result.verdict}`
    ].join("\n");
  }

  async function renderMonthClose(body, ctx, env) {
    const month = /^\d{4}-\d{2}$/.test(env.memo.month || "") ? env.memo.month : kstMonth();
    env.memo.month = month;
    body.innerHTML = `<section class="section"><div class="section-head"><h2>마감 대상</h2><p>${esc(ctx.businessName || "선택 사업자")} 기준으로 점검합니다. 이 화면은 자료를 바꾸지 않습니다.</p></div>
      <div class="toolbar"><label class="obx-inline">마감 월 <input class="field" type="month" data-obx-month value="${esc(month)}"></label><button type="button" class="btn" data-obx-reload>다시 점검</button><button type="button" class="btn" data-obx-print>인쇄</button></div></section>
      <div class="obx-grid"><section class="section"><div class="section-head"><h2>월마감 점검표</h2></div><div data-obx-checks><div class="empty" role="status">점검 자료를 불러오는 중입니다.</div></div></section>
      <section class="section"><div class="section-head"><h2>마감 리포트</h2></div><textarea class="obx-report" data-obx-report readonly aria-label="월마감 리포트"></textarea></section></div>`;
    body.querySelector("[data-obx-month]").addEventListener("change", event => { env.memo.month = event.target.value; env.rerender(); });
    body.querySelector("[data-obx-reload]").addEventListener("click", () => env.rerender());
    body.querySelector("[data-obx-print]").addEventListener("click", () => window.print());
    const paths = closingPaths(ctx.businessId, month);
    const entries = await Promise.all(Object.entries(paths).map(async ([key, path]) => [key, await settled(api(ctx, path))]));
    if (!body.isConnected) return;
    const result = closingChecks(ctx.businessId, Object.fromEntries(entries), month);
    body.querySelector("[data-obx-checks]").innerHTML = `<ul class="obx-checks">${result.checks.map(item => `<li class="${item.status}" data-check="${esc(item.key)}" data-scope="${esc(item.scope)}"><b>${esc(item.name)} <em class="obx-scope">${esc(CLOSING_SCOPE_LABEL[item.scope] || "")}</em></b><span class="badge">${esc(CLOSING_STATUS_LABEL[item.status] || "확인 불가")}</span><small>${esc(item.detail)}</small></li>`).join("")}</ul><p class="obx-verdict" role="status">마감 판정: <b>${esc(result.verdict)}</b></p>`;
    body.querySelector("[data-obx-report]").value = closingReport(month, ctx.businessName, result);
  }

  // ── 기초 설정 · 계좌 등록 ──────────────────────────────────
  function maskAccount(value) {
    const digits = String(value || "").replace(/\D/g, "");
    return digits.length > 4 ? `••••${digits.slice(-4)}` : (digits ? "••••" : "-");
  }

  function newAccountId() {
    return `acct-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
  }

  // 서버에서 방금 받은 기초설정에 계좌 한 건만 더한다. 저장은 기존 경로(PUT /settings)가 통째로 받으므로
  // 받은 목록이 비었거나 대상 사업자가 없으면 다른 자료를 지울 수 있어 저장하지 않는다.
  function buildAccountSettings(fresh, input, businessId, makeId = newAccountId) {
    const settings = fresh?.settings;
    if (!settings || !Array.isArray(settings.businesses) || !settings.businesses.length) throw new Error("기초 설정을 다시 불러오지 못해 저장을 멈췄습니다. 새로고침 후 다시 시도하십시오.");
    if (!settings.businesses.some(item => String(item.id) === String(businessId))) throw new Error("선택한 사업자가 기초 설정에 없습니다. 사업자 현황에서 먼저 등록하십시오.");
    const bank = String(input.bank || "").trim();
    const name = String(input.name || "").trim();
    const accountNo = String(input.accountNo || "").trim();
    if (!bank || !name || !accountNo) throw new Error("은행·계좌명·계좌번호를 모두 입력하십시오.");
    const digits = accountNo.replace(/\D/g, "");
    if (!/^[\d-]+$/.test(accountNo) || digits.length < 6 || digits.length > 30) throw new Error("계좌번호는 숫자와 - 만 6~30자리로 입력하십시오.");
    const accounts = Array.isArray(settings.accounts) ? settings.accounts : [];
    if (accounts.some(item => String(item.businessId || item.business_id) === String(businessId) && String(item.accountNo || "").replace(/\D/g, "") === digits)) throw new Error("이미 등록된 계좌입니다.");
    const account = { id: makeId(), bank, name, accountNo, businessId: String(businessId) };
    return { account, settings: { ...settings, accounts: [...accounts, account] } };
  }

  async function renderBasicSettings(body, ctx, env) {
    const canManage = ["owner", "admin"].includes(String(ctx.role || ""));
    body.innerHTML = `<div data-obx-settings><div class="empty" role="status">기초 설정을 불러오는 중입니다.</div></div>`;
    const slot = body.querySelector("[data-obx-settings]");
    let payload;
    try {
      payload = await ctx.financeApi("/settings");
    } catch (error) {
      if (!body.isConnected) return;
      renderStateInto(slot, viewStateConfig("settings", { error, onRetry: env.rerender }, env), env.goView);
      return;
    }
    if (!body.isConnected) return;
    const settings = payload?.settings || {};
    const bid = String(ctx.businessId || "");
    const business = (settings.businesses || []).find(item => String(item.id) === bid) || null;
    const branches = (settings.branches || []).filter(item => String(item.businessId) === bid);
    const branchNames = new Set(branches.map(item => item.name));
    const accounts = (settings.accounts || []).filter(item => String(item.businessId || item.business_id) === bid);
    const staff = (settings.staff || []).filter(item => branchNames.has(item.branch));
    const missing = Array.isArray(business?.missing_registration_fields) ? business.missing_registration_fields : [];
    const row = (cells, head) => `<tr>${cells.map(cell => head ? `<th>${esc(cell)}</th>` : `<td>${esc(cell)}</td>`).join("")}</tr>`;
    const table = (head, rows, empty) => rows.length
      ? `<div class="table-wrap"><table><thead>${row(head, true)}</thead><tbody>${rows.map(cells => row(cells)).join("")}</tbody></table></div>`
      : `<div class="empty">${esc(empty)}</div>`;
    const go = (route, label) => `<button type="button" class="btn" data-obx-go="${esc(route)}">${esc(label)}</button>`;
    slot.innerHTML = `
      <section class="section"><div class="section-head"><h2>사업자 기본정보</h2><p>신고·계약·급여 문서에 쓰이는 기준값입니다.</p><span class="right">${go("businesses", "사업자 정보 수정")}</span></div>
        ${business ? `<dl class="obx-facts"><div><dt>상호</dt><dd>${esc(business.tradeName || business.name)}</dd></div><div><dt>사업자등록번호</dt><dd>${esc(business.registrationNo || "미등록")}</dd></div><div><dt>대표자</dt><dd>${esc(business.representative || "미등록")}</dd></div><div><dt>과세유형</dt><dd>${esc(business.taxType || "미등록")}</dd></div><div><dt>개업일</dt><dd>${esc(business.openedAt || "미등록")}</dd></div><div><dt>사업장 주소</dt><dd>${esc(business.address || "미등록")}</dd></div></dl>${missing.length ? `<p class="notice">사업자등록증 기준 정보 ${missing.length}개가 비어 있습니다. 사업자 정보 수정에서 채워 주십시오.</p>` : ""}`
          : `<div class="empty">기초 설정에 이 사업자가 없습니다. 사업자 현황에서 먼저 등록하십시오.</div>`}
      </section>
      <section class="section"><div class="section-head"><h2>지점</h2><span class="right">${go("branches", "지점 등록·수정")}</span></div>
        ${table(["지점명", "운영상태", "전화", "주소"], branches.map(item => [item.name, { active: "운영중", paused: "보류", closed: "폐점" }[item.status] || item.status || "-", item.phone || "-", item.address || "-"]), "등록된 지점이 없습니다.")}
      </section>
      <section class="section"><div class="section-head"><h2>정산 계좌</h2><p>입금 대사와 급여 이체 기준 계좌입니다. 계좌번호는 끝 4자리만 표시합니다.</p><span class="right">${go("bank-connect", "은행·카드 연동")}</span></div>
        ${table(["은행", "계좌명", "계좌번호"], accounts.map(item => [item.bank || "-", item.name || "-", maskAccount(item.accountNo)]), "등록된 정산 계좌가 없습니다.")}
        ${canManage && business ? `<form class="obx-form" data-obx-account-form autocomplete="off"><h3>계좌 등록</h3>
          <label>은행<input class="field" name="bank" placeholder="예: 국민은행" required></label>
          <label>계좌명<input class="field" name="name" placeholder="예: 본점 매출입금" required></label>
          <label>계좌번호<input class="field" name="accountNo" inputmode="numeric" placeholder="숫자와 - 만" required></label>
          <label>소속 사업자<input class="field" value="${esc(business.tradeName || business.name)}" disabled></label>
          <div class="obx-form-actions"><button type="submit" class="btn primary">계좌 저장</button><span role="status" data-obx-account-status></span></div></form>`
          : `<p class="notice">${canManage ? "사업자를 먼저 등록해야 계좌를 등록할 수 있습니다." : "계좌 등록은 대표·관리자만 할 수 있습니다."}</p>`}
      </section>
      <section class="section"><div class="section-head"><h2>직원 기본정보</h2><p>지점별 기본시급과 근무유형입니다. 신규 직원은 초대·가입승인으로 등록합니다.</p><span class="right">${go("employees", "직원 초대·가입승인")}</span></div>
        ${table(["직원명", "소속 지점", "기본시급", "근무유형"], staff.map(item => [item.name || "-", item.branch || "-", item.hourly ? won(item.hourly) : "-", item.employmentType || "-"]), "등록된 직원 기본정보가 없습니다.")}
      </section>
      <section class="section"><div class="section-head"><h2>자동연동</h2><p>판매사이트·은행·카드·홈택스 연결은 자동연동 관리에서 합니다.</p><span class="right">${go("integrations", "자동연동 관리")}${go("data-collection", "자료 수집 현황")}</span></div></section>`;
    slot.querySelectorAll("[data-obx-go]").forEach(button => button.addEventListener("click", () => ctx.goRoute(button.dataset.obxGo)));
    const form = slot.querySelector("[data-obx-account-form]");
    form?.addEventListener("submit", event => {
      event.preventDefault();
      saveAccount(form, ctx, env, business);
    });
  }

  async function saveAccount(form, ctx, env, business) {
    const status = form.querySelector("[data-obx-account-status]");
    const submit = form.querySelector("button[type=submit]");
    const input = Object.fromEntries(new FormData(form).entries());
    const label = business?.tradeName || business?.name || "선택 사업자";
    if (!window.confirm(`${String(input.bank || "").trim()} ${String(input.name || "").trim()} 계좌를 ${label} 기초 설정에 등록합니다.`)) return;
    submit.disabled = true;
    status.textContent = "저장 중입니다.";
    try {
      const fresh = await ctx.financeApi("/settings");
      const next = buildAccountSettings(fresh, input, ctx.businessId);
      await ctx.financeApi("/settings", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ settings: next.settings }) });
      const check = await ctx.financeApi("/settings");
      const saved = (check?.settings?.accounts || []).some(item => item.id === next.account.id);
      ctx.toast(saved ? "정산 계좌를 저장했습니다. 다시 불러와 저장을 확인했습니다." : "저장 요청은 끝났지만 다시 불러온 목록에서 계좌를 찾지 못했습니다. 새로고침으로 확인하십시오.");
      env.rerender();
    } catch (error) {
      if (!form.isConnected) return;
      status.textContent = Number(error.status) === 403 ? "기초설정 저장 권한이 없습니다." : (error.message || "저장하지 못했습니다.");
      submit.disabled = false;
    }
  }

  // ── 모듈 화면 띄우기 ────────────────────────────────────────
  function mountModule(body, route, tab, ctx, env) {
    body.innerHTML = `<div class="obx-module" data-obx-module></div>`;
    const el = body.querySelector("[data-obx-module]");
    const missing = () => renderStateInto(el, { kind: "error", title: "화면을 불러오지 못했습니다", reason: "페이지를 새로고침해 주십시오.", actions: [{ label: "새로고침", primary: true, onClick: () => window.location.reload() }] });
    const viewState = (slot, view, stateCtx) => renderStateInto(slot, viewStateConfig(view, stateCtx, env), env.goView);
    const renderState = (slot, cfg) => renderStateInto(slot, cfg, env.goView);
    if (route === "data-collection") {
      const mod = window.obysClobe;
      if (!mod?.mount || !mod.mount(el, tab, { role: ctx.role, viewState, renderState, goView: env.goView, login: ctx.onExpired })) missing();
    } else if (route === "ledger-details") {
      const mod = window.obysLedgerDetails;
      if (!mod?.mount || !mod.mount(el, tab, { businessId: ctx.businessId, viewState })) missing();
    } else if (route === "store-assistant") {
      const mod = window.ObysStoreAssistant;
      if (!mod?.mount || !mod.mount(el, { businessId: ctx.businessId, onExpired: ctx.onExpired })) missing();
    }
  }

  const memo = {};

  function render(route, page, ctx) {
    const meta = ROUTES[route];
    if (!meta || !page) return false;
    const tab = tabOf(route);
    const key = `${route}|${ctx.businessId || ""}|${tab}`;
    const current = page.querySelector(":scope > [data-admin-extra]");
    if (current && current.dataset.key === key && !ctx.force) return true;
    const routeMemo = memo[route] || (memo[route] = {});
    const env = {
      memo: routeMemo,
      login: ctx.onExpired,
      retry: () => render(route, page, { ...ctx, force: true }),
      rerender: () => render(route, page, { ...ctx, force: true }),
      // 모듈 화면의 이동 요청: 같은 메뉴의 탭이면 탭을 바꾸고, 다른 업무면 새 화면 메뉴로 보낸다.
      goView(view) {
        for (const [other, item] of Object.entries(ROUTES)) {
          if ((item.tabs || []).some(([k]) => k === view)) {
            tabs[other] = view;
            saveTabs();
            if (other === route) render(route, page, { ...ctx, force: true });
            else ctx.goRoute(other);
            return true;
          }
        }
        const target = ROUTE_ALIASES[view];
        if (target) { ctx.goRoute(target); return true; }
        return false;
      }
    };
    env.retry = env.rerender;
    page.innerHTML = `<div class="obx" data-admin-extra="${esc(route)}" data-key="${esc(key)}">
      <div class="hero"><div><h1>${esc(meta.title)}</h1><p>${esc(meta.desc)}</p></div></div>
      ${meta.tabs ? `<div class="obx-tabs" role="tablist" aria-label="${esc(meta.title)} 화면">${meta.tabs.map(([k, label]) => `<button type="button" role="tab" class="obx-tab${k === tab ? " active" : ""}" aria-selected="${k === tab}" data-obx-tab="${esc(k)}">${esc(label)}</button>`).join("")}</div>` : ""}
      <div class="obx-body" data-obx-body></div></div>`;
    page.querySelectorAll("[data-obx-tab]").forEach(button => button.addEventListener("click", () => {
      tabs[route] = button.dataset.obxTab;
      saveTabs();
      render(route, page, { ...ctx, force: true });
    }));
    const body = page.querySelector("[data-obx-body]");
    if (!ctx.businessId && route !== "data-collection") {
      body.innerHTML = `<div class="empty">사업자를 선택하면 이 화면을 볼 수 있습니다.</div>`;
      return true;
    }
    if (route === "account-master") renderAccountMaster(body, ctx, env);
    else if (route === "month-close") renderMonthClose(body, ctx, env);
    else if (route === "basic-settings") renderBasicSettings(body, ctx, env);
    else mountModule(body, route, tab, ctx, env);
    return true;
  }

  window.ObysAdminExtras = {
    routes: Object.keys(ROUTES),
    has: route => Object.prototype.hasOwnProperty.call(ROUTES, route),
    title: route => ROUTES[route]?.title || "",
    render,
    // 단위 시험용 순수 함수
    viewStateConfig,
    closingChecks,
    closingReport,
    closingPaths,
    correctionWorkDates,
    CORRECTIONS_LIST_LIMIT,
    monthRange,
    buildAccountSettings,
    maskAccount
  };
})();
