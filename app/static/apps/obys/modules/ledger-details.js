(() => {
  "use strict";

  const API = "/api/v1/yeoljeong-finance";
  const TOKEN_KEYS = ["aads_token", "fb_access_token"];
  const VIEWS = {
    salesLedger: { kind: "sales", title: "매출 상세", noun: "매출", color: "#2563eb" },
    purchaseLedger: { kind: "purchase", title: "매입 상세", noun: "매입", color: "#7c3aed" },
    bankLedger: { kind: "bank", title: "은행 거래내역", noun: "은행 거래", color: "#0f766e" },
    cardLedger: { kind: "card", title: "카드 사용내역", noun: "카드 사용", color: "#c2410c" }
  };

  const state = {
    activeView: "salesLedger",
    businesses: [],
    businessId: "",
    rows: [],
    editId: "",
    search: "",
    dateFrom: "",
    dateTo: ""
  };

  const token = () => TOKEN_KEYS.map(key => localStorage.getItem(key)).find(Boolean) || "";
  const escapeHtml = value => String(value ?? "").replace(/[&<>'"]/g, character => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;"
  })[character]);
  const money = value => `${Number(value || 0).toLocaleString("ko-KR")}원`;
  const dateText = value => String(value || "").slice(0, 10) || "-";
  const localDateTime = value => {
    const date = value ? new Date(value) : new Date();
    const offset = date.getTimezoneOffset() * 60000;
    return new Date(date.getTime() - offset).toISOString().slice(0, 16);
  };

  function section() {
    return document.getElementById(`${state.activeView}View`);
  }

  async function request(path, options = {}) {
    const authToken = token();
    if (!authToken) throw Object.assign(new Error("로그인이 필요합니다."), { status: 401 });
    const response = await fetch(`${API}${path}`, {
      ...options,
      headers: {
        Accept: "application/json",
        Authorization: `Bearer ${authToken}`,
        ...(options.body ? { "Content-Type": "application/json" } : {}),
        ...(options.headers || {})
      }
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw Object.assign(new Error(payload.detail || `서버 응답 오류 (${response.status})`), { status: response.status });
    }
    return payload;
  }

  function scopedBusinessKey() {
    const tenantId = JSON.parse(localStorage.getItem("yeoljeong-finance-auth-user") || "null")?.tenant?.id || "tenant";
    return `obys_ledger_business:${tenantId}`;
  }

  async function loadBusinesses() {
    const payload = await request("/tenant-registry/businesses");
    state.businesses = Array.isArray(payload.businesses) ? payload.businesses : [];
    const saved = localStorage.getItem(scopedBusinessKey()) || "";
    if (!state.businesses.some(item => item.id === state.businessId)) {
      state.businessId = state.businesses.some(item => item.id === saved) ? saved : (state.businesses[0]?.id || "");
    }
    if (state.businessId) localStorage.setItem(scopedBusinessKey(), state.businessId);
  }

  function normalizeManual(row, kind) {
    if (kind === "bank") {
      return {
        ...row,
        kind,
        date: row.occurred_at,
        party: row.counterparty || "-",
        detail: [row.account_label, row.category, row.memo].filter(Boolean).join(" · ") || "-",
        amount: row.amount,
        direction: row.direction,
        editable: row.source === "manual"
      };
    }
    if (kind === "card") {
      return {
        ...row,
        kind,
        date: row.occurred_at,
        party: row.merchant || "-",
        detail: [row.card_number_masked, row.description].filter(Boolean).join(" · ") || "-",
        amount: row.total_amount,
        editable: row.source === "manual"
      };
    }
    return {
      ...row,
      kind,
      date: row.occurred_on,
      party: row.counterparty || "-",
      detail: row.description || "-",
      amount: row.total_amount,
      editable: row.source === "manual"
    };
  }

  function normalizeImported(row, kind) {
    return {
      ...row,
      id: `upload-${row.id}`,
      kind,
      date: row.occurred_on,
      party: row.counterparty || "-",
      detail: row.description || "업로드 원장",
      amount: row.amount,
      source: "upload",
      editable: false
    };
  }

  async function loadRows() {
    if (!state.businessId) {
      state.rows = [];
      return;
    }
    const config = VIEWS[state.activeView];
    const params = new URLSearchParams({ business_id: state.businessId });
    if (state.dateFrom) params.set("date_from", state.dateFrom);
    if (state.dateTo) params.set("date_to", state.dateTo);
    if (config.kind === "sales" || config.kind === "purchase") {
      params.set("category", config.kind);
      const [manual, uploaded] = await Promise.all([
        request(`/ledger-entries?${params}`),
        request(`/uploaded-ledger?business_id=${encodeURIComponent(state.businessId)}&category=${config.kind}&limit=1000`)
      ]);
      state.rows = [
        ...(manual.entries || []).map(row => normalizeManual(row, config.kind)),
        ...(uploaded.rows || []).map(row => normalizeImported(row, config.kind))
      ];
    } else if (config.kind === "bank") {
      const [manual, uploaded] = await Promise.all([
        request(`/ledger-bank-transactions?${params}`),
        request(`/uploaded-ledger?business_id=${encodeURIComponent(state.businessId)}&category=transaction&limit=1000`)
      ]);
      state.rows = [
        ...(manual.bank_transactions || []).map(row => normalizeManual(row, "bank")),
        ...(uploaded.rows || []).map(row => normalizeImported(row, "bank"))
      ];
    } else {
      const payload = await request(`/card-transactions?${params}`);
      state.rows = (payload.card_transactions || []).map(row => normalizeManual(row, "card"));
    }
    state.rows.sort((left, right) => String(right.date || "").localeCompare(String(left.date || "")));
  }

  function businessOptions() {
    return state.businesses.map(item => `<option value="${escapeHtml(item.id)}"${item.id === state.businessId ? " selected" : ""}>${escapeHtml(item.name || item.id)}</option>`).join("");
  }

  function filteredRows() {
    const term = state.search.trim().toLowerCase();
    return state.rows.filter(row => {
      if (!term) return true;
      return `${row.party || ""} ${row.detail || ""} ${row.category || ""} ${row.account_label || ""}`.toLowerCase().includes(term);
    });
  }

  function formFields(config, row = {}) {
    if (config.kind === "bank") {
      return `
        <label>거래일시<input name="occurred_at" type="datetime-local" required value="${escapeHtml(localDateTime(row.occurred_at))}"></label>
        <label>입출금<select name="direction"><option value="in"${row.direction === "in" ? " selected" : ""}>입금</option><option value="out"${row.direction === "out" ? " selected" : ""}>출금</option></select></label>
        <label>금액<input name="amount" type="number" min="0" step="1" required value="${escapeHtml(row.amount ?? "")}"></label>
        <label>거래 후 잔액<input name="balance" type="number" min="0" step="1" value="${escapeHtml(row.balance ?? "")}"></label>
        <label>거래처<input name="counterparty" maxlength="200" value="${escapeHtml(row.counterparty || "")}"></label>
        <label>계좌명<input name="account_label" maxlength="100" value="${escapeHtml(row.account_label || "")}" placeholder="예: 신한 주계좌"></label>
        <label>분류<input name="category" maxlength="100" value="${escapeHtml(row.category || "")}" placeholder="예: 매출입금, 임차료"></label>
        <label class="wide">메모<input name="memo" maxlength="500" value="${escapeHtml(row.memo || "")}"></label>`;
    }
    const partyName = config.kind === "card" ? "merchant" : "counterparty";
    const partyLabel = config.kind === "card" ? "가맹점" : "거래처";
    const occurredName = config.kind === "card" ? "occurred_at" : "occurred_on";
    const occurredType = config.kind === "card" ? "datetime-local" : "date";
    const occurredValue = config.kind === "card" ? localDateTime(row.occurred_at) : (dateText(row.occurred_on) === "-" ? new Date().toISOString().slice(0, 10) : dateText(row.occurred_on));
    return `
      <label>거래${config.kind === "card" ? "일시" : "일"}<input name="${occurredName}" type="${occurredType}" required value="${escapeHtml(occurredValue)}"></label>
      <label>${partyLabel}<input name="${partyName}" maxlength="200" value="${escapeHtml(row[partyName] || "")}"></label>
      ${config.kind === "card" ? `<label>카드 끝 4자리<input name="card_last4" inputmode="numeric" pattern="[0-9]{4}" maxlength="4" required value="${escapeHtml(String(row.card_number_masked || "").slice(-4))}"></label>` : ""}
      <label>공급가액<input name="supply_amount" data-money-part type="number" min="0" step="0.01" required value="${escapeHtml(row.supply_amount ?? 0)}"></label>
      <label>세액<input name="tax_amount" data-money-part type="number" min="0" step="0.01" required value="${escapeHtml(row.tax_amount ?? 0)}"></label>
      <label>합계<input name="total_amount" type="number" min="0" step="0.01" readonly value="${escapeHtml(row.total_amount ?? 0)}"></label>
      <label class="wide">적요<input name="description" maxlength="500" value="${escapeHtml(row.description || "")}"></label>`;
  }

  function rowHtml(row) {
    const source = row.editable ? "수기등록" : "파일업로드";
    const direction = row.kind === "bank" ? `<span class="ledger-direction ${row.direction === "out" ? "out" : "in"}">${row.direction === "out" ? "출금" : "입금"}</span>` : "";
    return `<tr data-ledger-row="${escapeHtml(row.id)}">
      <td>${escapeHtml(dateText(row.date))}</td>
      <td>${direction}${escapeHtml(row.party || "-")}</td>
      <td>${escapeHtml(row.detail || "-")}</td>
      <td class="num">${escapeHtml(money(row.amount))}</td>
      <td><span class="badge ${row.editable ? "info" : "good"}">${source}</span></td>
      <td>${row.editable ? `<button type="button" data-edit-ledger="${escapeHtml(row.id)}">수정</button><button type="button" class="danger" data-delete-ledger="${escapeHtml(row.id)}">삭제</button>` : '<span class="muted">조회 전용</span>'}</td>
    </tr>`;
  }

  function render() {
    const root = section();
    if (!root) return;
    const config = VIEWS[state.activeView];
    const rows = filteredRows();
    const total = rows.reduce((sum, row) => sum + Number(row.amount || 0) * (row.kind === "bank" && row.direction === "out" ? -1 : 1), 0);
    const editRow = state.rows.find(row => row.id === state.editId && row.editable) || {};
    root.style.setProperty("--ledger-accent", config.color);
    root.innerHTML = `
      <header class="ledger-hero">
        <div><span class="ledger-eyebrow">사업자별 실데이터</span><h1>${config.title}</h1><p>목록에서 거래를 확인하고 같은 화면에서 신규 등록·수정할 수 있습니다.</p></div>
        <button type="button" class="primary" data-new-ledger>+ ${config.noun} 등록</button>
      </header>
      <div class="ledger-toolbar">
        <label>사업자<select data-ledger-business>${businessOptions()}</select></label>
        <label>시작일<input data-ledger-from type="date" value="${escapeHtml(state.dateFrom)}"></label>
        <label>종료일<input data-ledger-to type="date" value="${escapeHtml(state.dateTo)}"></label>
        <label class="ledger-search">검색<input data-ledger-search type="search" value="${escapeHtml(state.search)}" placeholder="거래처·적요 검색"></label>
        <button type="button" data-ledger-refresh>새로고침</button>
      </div>
      <div class="ledger-kpis">
        <article><small>표시 건수</small><strong>${rows.length.toLocaleString("ko-KR")}건</strong></article>
        <article><small>${config.kind === "bank" ? "순입출금" : "합계"}</small><strong>${money(total)}</strong></article>
        <article><small>선택 사업자</small><strong>${escapeHtml(state.businesses.find(item => item.id === state.businessId)?.name || "미등록")}</strong></article>
      </div>
      <div class="ledger-layout">
        <section class="ledger-list-card">
          <div class="ledger-card-head"><h2>${config.noun} 목록</h2><span aria-live="polite">최근 ${rows.length}건</span></div>
          <div class="table-wrap"><table><thead><tr><th>일자</th><th>거래처</th><th>상세</th><th>금액</th><th>출처</th><th>관리</th></tr></thead>
          <tbody>${rows.length ? rows.map(rowHtml).join("") : '<tr><td colspan="6"><div class="ledger-empty">등록된 내역이 없습니다. 첫 거래를 등록해 주세요.</div></td></tr>'}</tbody></table></div>
        </section>
        <aside class="ledger-form-card" data-ledger-form-card>
          <div class="ledger-card-head"><h2>${state.editId ? `${config.noun} 수정` : `${config.noun} 등록`}</h2>${state.editId ? '<button type="button" data-cancel-ledger>신규 등록으로</button>' : ""}</div>
          <form data-ledger-form>
            <div class="ledger-form-grid">${formFields(config, editRow)}</div>
            <p class="ledger-form-status" data-ledger-status aria-live="polite">모든 데이터는 현재 사업자에만 저장됩니다.</p>
            <button type="submit" class="primary"${state.businessId ? "" : " disabled"}>${state.editId ? "수정 저장" : "등록 저장"}</button>
          </form>
        </aside>
      </div>`;
    bind(root);
  }

  function renderError(error) {
    const root = section();
    if (!root) return;
    const recovery = error?.status === 401 ? "로그인 세션이 만료되었습니다. 다시 로그인한 뒤 재시도해 주세요." : error.message;
    root.innerHTML = `<div class="ledger-error"><strong>데이터를 불러오지 못했습니다.</strong><p>${escapeHtml(recovery)}</p><button type="button" data-ledger-retry>다시 시도</button></div>`;
    root.querySelector("[data-ledger-retry]")?.addEventListener("click", () => open(state.activeView, true));
  }

  function payloadFromForm(form, config) {
    const data = Object.fromEntries(new FormData(form).entries());
    if (data.occurred_at) data.occurred_at = new Date(data.occurred_at).toISOString();
    if (config.kind === "bank") {
      data.amount = Number(data.amount || 0);
      data.balance = data.balance === "" ? null : Number(data.balance);
    } else {
      ["supply_amount", "tax_amount", "total_amount"].forEach(key => { data[key] = Number(data[key] || 0); });
    }
    if (!state.editId) data.business_id = state.businessId;
    return data;
  }

  function endpoint(config, id = "") {
    if (config.kind === "sales" || config.kind === "purchase") return `/ledger-entries/${config.kind}${id ? `/${id}` : ""}`;
    if (config.kind === "bank") return `/ledger-bank-transactions${id ? `/${id}` : ""}`;
    return `/card-transactions${id ? `/${id}` : ""}`;
  }

  async function submit(form) {
    const config = VIEWS[state.activeView];
    const status = form.querySelector("[data-ledger-status]");
    const button = form.querySelector('button[type="submit"]');
    button.disabled = true;
    status.textContent = "저장 중입니다…";
    try {
      await request(endpoint(config, state.editId), {
        method: state.editId ? "PATCH" : "POST",
        body: JSON.stringify(payloadFromForm(form, config))
      });
      state.editId = "";
      await loadRows();
      render();
    } catch (error) {
      status.textContent = error.message;
      status.classList.add("error");
      button.disabled = false;
    }
  }

  async function remove(id) {
    if (!confirm("선택한 거래를 삭제하시겠습니까?")) return;
    const config = VIEWS[state.activeView];
    await request(endpoint(config, id), { method: "DELETE" });
    if (state.editId === id) state.editId = "";
    await loadRows();
    render();
  }

  function bind(root) {
    root.querySelector("[data-ledger-business]")?.addEventListener("change", async event => {
      state.businessId = event.target.value;
      localStorage.setItem(scopedBusinessKey(), state.businessId);
      state.editId = "";
      await refresh();
    });
    root.querySelector("[data-ledger-search]")?.addEventListener("input", event => {
      state.search = event.target.value;
      render();
      root.querySelector("[data-ledger-search]")?.focus();
    });
    root.querySelector("[data-ledger-refresh]")?.addEventListener("click", async () => {
      state.dateFrom = root.querySelector("[data-ledger-from]")?.value || "";
      state.dateTo = root.querySelector("[data-ledger-to]")?.value || "";
      await refresh();
    });
    root.querySelector("[data-new-ledger]")?.addEventListener("click", () => {
      state.editId = "";
      render();
      section()?.querySelector("[data-ledger-form-card]")?.scrollIntoView({ behavior: "smooth", block: "start" });
    });
    root.querySelector("[data-cancel-ledger]")?.addEventListener("click", () => { state.editId = ""; render(); });
    root.querySelectorAll("[data-edit-ledger]").forEach(button => button.addEventListener("click", () => {
      state.editId = button.dataset.editLedger;
      render();
      section()?.querySelector("[data-ledger-form-card]")?.scrollIntoView({ behavior: "smooth", block: "start" });
    }));
    root.querySelectorAll("[data-delete-ledger]").forEach(button => button.addEventListener("click", () => {
      remove(button.dataset.deleteLedger).catch(renderError);
    }));
    const form = root.querySelector("[data-ledger-form]");
    form?.addEventListener("submit", event => { event.preventDefault(); submit(form); });
    form?.querySelectorAll("[data-money-part]").forEach(input => input.addEventListener("input", () => {
      form.elements.total_amount.value = (Number(form.elements.supply_amount.value || 0) + Number(form.elements.tax_amount.value || 0)).toFixed(2);
    }));
  }

  async function refresh() {
    const root = section();
    if (root) root.innerHTML = '<div class="ledger-loading" role="status">사업자별 원장을 불러오는 중입니다…</div>';
    try {
      await loadRows();
      render();
    } catch (error) {
      renderError(error);
    }
  }

  async function open(view, force = false) {
    if (!VIEWS[view]) return;
    const changed = state.activeView !== view;
    state.activeView = view;
    if (changed) {
      state.editId = "";
      state.search = "";
    }
    const root = section();
    if (root) root.innerHTML = '<div class="ledger-loading" role="status">사업자 정보를 확인하는 중입니다…</div>';
    try {
      if (force || !state.businesses.length) await loadBusinesses();
      await loadRows();
      render();
    } catch (error) {
      renderError(error);
    }
  }

  window.obysLedgerDetails = { open, refresh: () => open(state.activeView, true) };
})();
