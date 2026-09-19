(function () {
  "use strict";

  const API = "/api/v1/yeoljeong-finance";
  const views = {
    salesLedger: { kind: "sales", title: "매출 상세", upload: "sales" },
    purchaseLedger: { kind: "purchase", title: "매입 상세", upload: "purchase" },
    bankLedger: { kind: "bank", title: "은행 거래내역", upload: "transaction" },
    cardLedger: { kind: "card", title: "카드 사용내역", upload: "card" }
  };
  const state = {
    businesses: [],
    view: "",
    businessId: "",
    rows: [],
    uploads: [],
    dateFrom: "",
    dateTo: "",
    loading: false,
    error: ""
  };

  function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>"']/g, char => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
    })[char]);
  }

  function token() {
    const cookie = name => document.cookie.split(";").map(item => item.trim())
      .find(item => item.startsWith(`${name}=`))?.slice(name.length + 1) || "";
    return localStorage.getItem("aads_token") || localStorage.getItem("fb_access_token")
      || cookie("aads_token") || cookie("fb_access_token");
  }

  async function request(path, options = {}) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 15000);
    try {
      const response = await fetch(`${API}${path}`, {
        ...options,
        headers: {
          ...(options.body instanceof FormData ? {} : { "Content-Type": "application/json" }),
          ...(token() ? { Authorization: `Bearer ${token()}` } : {}),
          ...(options.headers || {})
        },
        signal: controller.signal
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) {
        const error = new Error(payload.detail || payload.message || `API ${response.status}`);
        error.status = response.status;
        throw error;
      }
      return payload;
    } catch (error) {
      if (error.name === "AbortError") throw new Error("서버 응답이 지연되고 있습니다. 다시 시도하십시오.");
      throw error;
    } finally {
      clearTimeout(timeout);
    }
  }

  function money(value) {
    const numeric = Number(value || 0);
    return Number.isFinite(numeric) ? `${numeric.toLocaleString("ko-KR")}원` : "-";
  }

  function dateText(value) {
    return String(value || "-").slice(0, 16).replace("T", " ");
  }

  function selectedBusinessName() {
    return state.businesses.find(item => item.id === state.businessId)?.name || "사업자 미선택";
  }

  function recovery(error) {
    if (Number(error?.status) === 401) return "로그인 세션이 만료되었습니다. 다시 로그인한 뒤 재시도하십시오.";
    if (Number(error?.status) === 403 || Number(error?.status) === 404) {
      return "현재 테넌트에 속한 사업자를 선택할 수 없습니다. 사업자 권한을 확인하거나 다시 로그인하십시오.";
    }
    return error?.message || "데이터를 불러오지 못했습니다.";
  }

  function installStyle() {
    if (document.getElementById("obys-ledger-details-style")) return;
    const style = document.createElement("style");
    style.id = "obys-ledger-details-style";
    style.textContent = `
      .ledger-shell{display:grid;gap:14px}.ledger-toolbar,.ledger-actions{display:flex;gap:8px;flex-wrap:wrap;align-items:end}
      .ledger-toolbar .field{min-width:150px;flex:1}.ledger-toolbar label,.ledger-form label{display:grid;gap:5px;font-size:12px;color:var(--muted,#64748b)}
      .ledger-toolbar input,.ledger-toolbar select,.ledger-form input,.ledger-form select,.ledger-form textarea{min-height:44px;border:1px solid #dbe3ec;border-radius:10px;padding:9px 11px;background:#fff;color:#172033}
      .ledger-toolbar button,.ledger-actions button,.ledger-row-actions button,.ledger-upload button,.ledger-drawer button{min-height:44px;min-width:44px}
      .ledger-title{display:flex;justify-content:space-between;gap:10px;align-items:flex-start}.ledger-title h2{margin:0}.ledger-title p{margin:5px 0 0;color:#64748b}
      .ledger-table-wrap{overflow:auto;border:1px solid #e3e8ef;border-radius:14px;background:#fff}.ledger-table{width:100%;border-collapse:collapse;min-width:760px}
      .ledger-table th,.ledger-table td{padding:11px 12px;border-bottom:1px solid #eef2f6;text-align:left;vertical-align:middle}.ledger-table th{font-size:12px;color:#64748b;background:#f8fafc}
      .ledger-table tr[data-detail-id]{cursor:pointer}.ledger-table tr[data-detail-id]:hover{background:#f7fbff}.ledger-source{font-size:12px;color:#64748b}
      .ledger-state{border:1px dashed #ccd7e4;border-radius:14px;padding:24px;text-align:center;background:#fbfdff}.ledger-state.error{border-color:#f3b6b6;background:#fff8f8}
      .ledger-upload{border:1px solid #e3e8ef;border-radius:14px;padding:14px;background:#fff}.ledger-upload h3{margin:0 0 8px}.ledger-upload-row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}.ledger-upload-row input{min-height:44px;max-width:100%}
      .ledger-upload-history{display:grid;gap:7px;margin-top:10px}.ledger-upload-item{display:flex;justify-content:space-between;gap:10px;align-items:center;padding:9px;border-radius:10px;background:#f8fafc}
      .ledger-drawer-backdrop{position:fixed;inset:0;background:rgba(15,23,42,.42);z-index:1900;display:flex;justify-content:flex-end}.ledger-drawer{width:min(520px,100%);height:100%;overflow:auto;background:#fff;padding:20px;box-shadow:-10px 0 30px rgba(15,23,42,.2)}
      .ledger-drawer-head{display:flex;justify-content:space-between;gap:10px;align-items:center}.ledger-drawer h3{margin:0}.ledger-detail-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin:16px 0}.ledger-detail-grid div{padding:10px;background:#f8fafc;border-radius:10px}.ledger-detail-grid b{display:block;font-size:12px;color:#64748b;margin-bottom:4px}
      .ledger-form{display:grid;grid-template-columns:1fr 1fr;gap:10px}.ledger-form .wide{grid-column:1/-1}.ledger-form-actions{grid-column:1/-1;display:flex;justify-content:flex-end;gap:8px;margin-top:8px}.ledger-danger{color:#b42318;border-color:#f3b6b6}
      @media(max-width:600px){.ledger-shell,.ledger-title,.ledger-toolbar,.ledger-table-wrap,.ledger-upload{min-width:0;max-width:100%;width:100%;box-sizing:border-box}.ledger-shell,.ledger-title{grid-template-columns:minmax(0,1fr);overflow:hidden}.ledger-title{display:grid}.ledger-toolbar{display:grid;grid-template-columns:minmax(0,1fr)}.ledger-toolbar .field{min-width:0}.ledger-actions{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));width:100%;max-width:100%}.ledger-actions button{width:100%;min-width:0}.ledger-table{min-width:680px}.ledger-detail-grid,.ledger-form{grid-template-columns:1fr}.ledger-form .wide{grid-column:auto}.ledger-upload-item{align-items:flex-start;flex-direction:column}.ledger-upload-item button{width:100%}.ledger-drawer{padding:16px}}
    `;
    document.head.appendChild(style);
  }

  function viewRoot() {
    return document.getElementById(`${state.view}View`);
  }

  function businessOptions() {
    return state.businesses.map(item => `<option value="${escapeHtml(item.id)}"${item.id === state.businessId ? " selected" : ""}>${escapeHtml(item.name || item.id)}</option>`).join("");
  }

  function rowCells(row, kind) {
    if (kind === "bank") return [dateText(row.occurred_at || row.occurred_date || row.occurred_on), row.counterparty || row.memo || row.description || "-", row.direction === "in" ? "입금" : row.direction === "out" ? "출금" : "-", money(row.amount), row.account_label || "-", row.source || "manual"];
    if (kind === "card") return [dateText(row.occurred_at), row.merchant || "-", row.description || "-", money(row.total_amount), row.card_number_masked || "****", row.source || "manual"];
    return [dateText(row.occurred_on), row.counterparty || "-", row.description || "-", money(row.supply_amount ?? row.amount), money(row.tax_amount), money(row.total_amount ?? row.amount), row.source || (row.upload_id ? "upload" : "manual")];
  }

  function headers(kind) {
    if (kind === "bank") return ["거래일", "거래처·적요", "구분", "금액", "계좌", "원본"];
    if (kind === "card") return ["사용일", "가맹점", "적요", "합계", "카드", "원본"];
    return ["거래일", "거래처", "적요", "공급가", "세액", "합계", "원본"];
  }

  function render() {
    installStyle();
    const root = viewRoot();
    const meta = views[state.view];
    if (!root || !meta) return;
    root.innerHTML = `
      <div class="ledger-shell">
        <div class="ledger-title"><div><h2>${escapeHtml(meta.title)}</h2><p>${escapeHtml(selectedBusinessName())}의 서버 원장입니다. 샘플·로컬 데이터는 표시하지 않습니다.</p></div>
          <div class="ledger-actions"><button type="button" data-ledger-refresh>다시 불러오기</button><button type="button" class="primary" data-ledger-create>신규 수기 등록</button></div></div>
        <div class="ledger-toolbar">
          <label class="field">사업자<select data-ledger-business>${businessOptions()}</select></label>
          <label class="field">시작일<input type="date" data-ledger-from value="${escapeHtml(state.dateFrom)}"></label>
          <label class="field">종료일<input type="date" data-ledger-to value="${escapeHtml(state.dateTo)}"></label>
          <button type="button" data-ledger-filter>조회</button>
        </div>
        <div data-ledger-content>${contentHtml(meta.kind)}</div>
        ${uploadHtml(meta)}
      </div>`;
    bindRoot(root);
  }

  function contentHtml(kind) {
    if (!state.businesses.length) return `<div class="ledger-state error">현재 테넌트에 등록된 사업자가 없습니다.<br>사업자 기초정보를 먼저 등록한 뒤 다시 시도하십시오.</div>`;
    if (state.loading) return `<div class="ledger-state">서버 원장을 불러오는 중입니다…</div>`;
    if (state.error) return `<div class="ledger-state error">${escapeHtml(state.error)}<br><button type="button" data-ledger-refresh>재시도</button></div>`;
    if (!state.rows.length) return `<div class="ledger-state">등록된 ${escapeHtml(views[state.view].title)}이 없습니다.<br>신규 수기 등록 또는 파일 업로드로 시작하십시오.</div>`;
    return `<div class="ledger-table-wrap"><table class="ledger-table"><thead><tr>${headers(kind).map(label => `<th>${escapeHtml(label)}</th>`).join("")}<th>작업</th></tr></thead><tbody>${state.rows.map((row, index) => {
      const source = String(row.source || (row.upload_id ? "upload" : "manual"));
      const mutable = source === "manual";
      return `<tr data-detail-id="${escapeHtml(row.id || index)}" data-detail-index="${index}">${rowCells(row, kind).map(value => `<td>${escapeHtml(value)}</td>`).join("")}<td class="ledger-row-actions"><button type="button" data-ledger-detail="${index}">상세</button>${mutable ? `<button type="button" data-ledger-edit="${index}">수정</button>` : ""}</td></tr>`;
    }).join("")}</tbody></table></div>`;
  }

  function uploadHtml(meta) {
    return `<section class="ledger-upload"><h3>파일 업로드 이력·재시도</h3><p class="ledger-source">원본 파일은 수정하지 않습니다. 실패 파일은 삭제한 뒤 같은 파일을 다시 선택해 재시도할 수 있습니다.</p>
      <div class="ledger-upload-row"><input type="file" data-ledger-file accept=".csv,.xlsx${meta.upload === "sales" || meta.upload === "purchase" || meta.upload === "card" ? ",.pdf" : ""}"><button type="button" data-ledger-upload>파일 업로드</button></div>
      <div class="ledger-upload-history">${state.uploads.length ? state.uploads.map(item => `<div class="ledger-upload-item"><span><b>${escapeHtml(item.original_filename || "파일")}</b><br><small>${escapeHtml(item.status || "-")} · 반영 ${Number(item.imported_rows || 0)}건 · 거부 ${Number(item.rejected_rows || 0)}건 · ${escapeHtml(dateText(item.created_at))}</small></span><button type="button" class="ledger-danger" data-upload-retry="${escapeHtml(item.id)}">삭제 후 재업로드</button></div>`).join("") : `<div class="ledger-source">업로드 이력이 없습니다.</div>`}</div>
    </section>`;
  }

  function bindRoot(root) {
    root.querySelector("[data-ledger-business]")?.addEventListener("change", event => {
      state.businessId = event.target.value;
      load();
    });
    root.querySelectorAll("[data-ledger-refresh],[data-ledger-filter]").forEach(button => button.addEventListener("click", () => {
      state.dateFrom = root.querySelector("[data-ledger-from]")?.value || "";
      state.dateTo = root.querySelector("[data-ledger-to]")?.value || "";
      load();
    }));
    root.querySelector("[data-ledger-create]")?.addEventListener("click", () => openForm());
    root.querySelector("[data-ledger-upload]")?.addEventListener("click", uploadFile);
    root.querySelectorAll("[data-ledger-detail]").forEach(button => button.addEventListener("click", event => {
      event.stopPropagation();
      openDetail(state.rows[Number(button.dataset.ledgerDetail)]);
    }));
    root.querySelectorAll("[data-ledger-edit]").forEach(button => button.addEventListener("click", event => {
      event.stopPropagation();
      openForm(state.rows[Number(button.dataset.ledgerEdit)]);
    }));
    root.querySelectorAll("tr[data-detail-index]").forEach(row => row.addEventListener("click", () => openDetail(state.rows[Number(row.dataset.detailIndex)])));
    root.querySelectorAll("[data-upload-retry]").forEach(button => button.addEventListener("click", () => retryUpload(button.dataset.uploadRetry)));
  }

  function dateQuery() {
    return `${state.dateFrom ? `&date_from=${encodeURIComponent(state.dateFrom)}` : ""}${state.dateTo ? `&date_to=${encodeURIComponent(state.dateTo)}` : ""}`;
  }

  async function ensureBusinesses() {
    if (state.businesses.length) return;
    const payload = await request("/tenant-registry/businesses");
    state.businesses = payload.businesses || [];
    if (!state.businesses.some(item => item.id === state.businessId)) state.businessId = state.businesses[0]?.id || "";
  }

  async function load() {
    state.loading = true;
    state.error = "";
    render();
    try {
      await ensureBusinesses();
      if (!state.businessId) {
        state.rows = [];
        state.uploads = [];
        return;
      }
      const meta = views[state.view];
      const scope = `business_id=${encodeURIComponent(state.businessId)}`;
      let requests;
      if (meta.kind === "sales" || meta.kind === "purchase") {
        requests = [
          request(`/ledger-entries?${scope}&category=${meta.kind}${dateQuery()}`),
          request(`/uploaded-ledger?${scope}&category=${meta.kind}&limit=500`),
          request(`/uploads?${scope}&category=${meta.upload}`)
        ];
        const [manual, uploaded, uploads] = await Promise.all(requests);
        state.rows = [...(manual.entries || []), ...(uploaded.rows || []).map(row => ({ ...row, source: "upload" }))]
          .sort((a, b) => String(b.occurred_on || "").localeCompare(String(a.occurred_on || "")));
        state.uploads = uploads.uploads || [];
      } else if (meta.kind === "card") {
        const [ledger, uploads] = await Promise.all([
          request(`/card-transactions?${scope}${dateQuery()}`), request(`/card-uploads?${scope}`)
        ]);
        state.rows = ledger.card_transactions || [];
        state.uploads = uploads.uploads || [];
      } else {
        const [ledger, imported, uploads] = await Promise.all([
          request(`/ledger-bank-transactions?${scope}${dateQuery()}`),
          request(`/uploaded-ledger?${scope}&category=transaction&limit=500`),
          request(`/uploads?${scope}&category=transaction`)
        ]);
        state.rows = [
          ...(ledger.bank_transactions || []),
          ...(imported.rows || []).map(row => ({ ...row, source: "upload" }))
        ].sort((a, b) => String(b.occurred_at || b.occurred_on || "").localeCompare(String(a.occurred_at || a.occurred_on || "")));
        state.uploads = uploads.uploads || [];
      }
    } catch (error) {
      state.error = recovery(error);
      state.rows = [];
      state.uploads = [];
    } finally {
      state.loading = false;
      render();
    }
  }

  function detailFields(row) {
    const kind = views[state.view].kind;
    const common = [
      [kind === "card" ? "사용일" : "거래일", row.occurred_at || row.occurred_on || row.occurred_date],
      [kind === "card" ? "가맹점" : "거래처", row.merchant || row.counterparty],
      ["적요", row.description || row.memo],
      ["원본 출처", row.source || (row.upload_id ? "upload" : "manual")],
      ["생성시각", row.created_at], ["수정시각", row.updated_at]
    ];
    if (kind === "bank") common.splice(3, 0, ["입·출금", row.direction === "in" ? `입금 ${money(row.amount)}` : row.direction === "out" ? `출금 ${money(row.amount)}` : money(row.amount)], ["계좌", row.account_label || "-"]);
    else common.splice(3, 0, ["공급가", money(row.supply_amount ?? row.amount)], ["세액", money(row.tax_amount)], ["합계", money(row.total_amount ?? row.amount)]);
    if (kind === "card") common.splice(3, 0, ["카드", row.card_number_masked || "****"]);
    return common;
  }

  function drawer(html) {
    closeDrawer();
    const backdrop = document.createElement("div");
    backdrop.className = "ledger-drawer-backdrop";
    backdrop.dataset.ledgerDrawer = "true";
    backdrop.innerHTML = `<aside class="ledger-drawer" role="dialog" aria-modal="true">${html}</aside>`;
    backdrop.addEventListener("click", event => { if (event.target === backdrop) closeDrawer(); });
    document.body.appendChild(backdrop);
    backdrop.querySelector("[data-drawer-close]")?.addEventListener("click", closeDrawer);
    return backdrop;
  }

  function closeDrawer() {
    document.querySelector("[data-ledger-drawer]")?.remove();
  }

  function openDetail(row) {
    if (!row) return;
    const source = String(row.source || (row.upload_id ? "upload" : "manual"));
    const mutable = source === "manual";
    const panel = drawer(`<div class="ledger-drawer-head"><h3>거래 상세</h3><button type="button" data-drawer-close aria-label="닫기">닫기</button></div>
      <div class="ledger-detail-grid">${detailFields(row).map(([label, value]) => `<div><b>${escapeHtml(label)}</b>${escapeHtml(value || "-")}</div>`).join("")}</div>
      <p class="ledger-source">${mutable ? "수기 등록 건만 수정·삭제할 수 있습니다." : "업로드 원본 행은 보존 정책에 따라 수정·삭제할 수 없습니다."}</p>
      ${mutable ? `<div class="ledger-actions"><button type="button" data-detail-edit>수정</button><button type="button" class="ledger-danger" data-detail-delete>삭제</button></div>` : ""}`);
    panel.querySelector("[data-detail-edit]")?.addEventListener("click", () => openForm(row));
    panel.querySelector("[data-detail-delete]")?.addEventListener("click", () => removeRow(row));
  }

  function value(row, key, fallback = "") {
    return escapeHtml(row?.[key] ?? fallback);
  }

  function openForm(row = null) {
    const kind = views[state.view].kind;
    const editing = Boolean(row?.id);
    const occurred = row?.occurred_at ? String(row.occurred_at).slice(0, 16) : String(row?.occurred_on || new Date().toISOString().slice(0, 10));
    let fields;
    if (kind === "bank") {
      fields = `<label>계좌명<input name="account_label" value="${value(row, "account_label")}" maxlength="100" placeholder="예: 신한 운영계좌"></label>
        <label>거래일시<input name="occurred_at" type="datetime-local" value="${escapeHtml(occurred)}" required></label>
        <label>입·출금<select name="direction"><option value="in"${row?.direction === "in" ? " selected" : ""}>입금</option><option value="out"${row?.direction === "out" ? " selected" : ""}>출금</option></select></label>
        <label>금액<input name="amount" type="number" min="0" step="1" value="${value(row, "amount", 0)}" required></label>
        <label>잔액<input name="balance" type="number" min="0" step="1" value="${value(row, "balance")}"></label>
        <label>거래처<input name="counterparty" value="${value(row, "counterparty")}" maxlength="200"></label>
        <label class="wide">적요<textarea name="memo" maxlength="500">${value(row, "memo")}</textarea></label>`;
    } else {
      const partyName = kind === "card" ? "merchant" : "counterparty";
      fields = `<label>${kind === "card" ? "사용일시" : "거래일"}<input name="occurred" type="${kind === "card" ? "datetime-local" : "date"}" value="${escapeHtml(occurred)}" required></label>
        <label>${kind === "card" ? "가맹점" : "거래처"}<input name="party" value="${value(row, partyName)}" maxlength="200"></label>
        ${kind === "card" ? `<label>카드 마지막 4자리<input name="card_last4" inputmode="numeric" pattern="[0-9]{4}" maxlength="4" value="${escapeHtml(String(row?.card_number_masked || "").slice(-4))}" required></label>` : ""}
        <label>공급가<input name="supply_amount" type="number" min="0" step="0.01" value="${value(row, "supply_amount", 0)}" required></label>
        <label>세액<input name="tax_amount" type="number" min="0" step="0.01" value="${value(row, "tax_amount", 0)}" required></label>
        <label>합계<input name="total_amount" type="number" min="0" step="0.01" value="${value(row, "total_amount", 0)}" required></label>
        <label class="wide">적요<textarea name="description" maxlength="500">${value(row, "description")}</textarea></label>`;
    }
    const panel = drawer(`<div class="ledger-drawer-head"><h3>${editing ? "수기 거래 수정" : "신규 수기 등록"}</h3><button type="button" data-drawer-close>닫기</button></div>
      <form class="ledger-form" data-ledger-form>${fields}<div class="ledger-form-actions"><button type="button" data-drawer-close>취소</button><button type="submit" class="primary">저장</button></div></form><p class="ledger-state error hidden" data-form-error></p>`);
    panel.querySelector("[data-ledger-form]")?.addEventListener("submit", event => saveRow(event, row));
    panel.querySelectorAll("[data-drawer-close]").forEach(button => button.addEventListener("click", closeDrawer));
  }

  function isoWithZone(value) {
    if (!value) return "";
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? value : date.toISOString();
  }

  async function saveRow(event, row) {
    event.preventDefault();
    const form = event.currentTarget;
    const data = Object.fromEntries(new FormData(form).entries());
    const kind = views[state.view].kind;
    const editing = Boolean(row?.id);
    let path;
    let payload;
    if (kind === "bank") {
      path = editing ? `/ledger-bank-transactions/${encodeURIComponent(row.id)}` : "/ledger-bank-transactions";
      payload = { ...data, amount: Number(data.amount), balance: data.balance === "" ? null : Number(data.balance), occurred_at: isoWithZone(data.occurred_at) };
      if (!editing) payload.business_id = state.businessId;
    } else if (kind === "card") {
      path = editing ? `/card-transactions/${encodeURIComponent(row.id)}` : "/card-transactions";
      payload = { occurred_at: isoWithZone(data.occurred), merchant: data.party, description: data.description, supply_amount: data.supply_amount, tax_amount: data.tax_amount, total_amount: data.total_amount, card_last4: data.card_last4 };
      if (!editing) payload.business_id = state.businessId;
    } else {
      path = editing ? `/ledger-entries/${kind}/${encodeURIComponent(row.id)}` : `/ledger-entries/${kind}`;
      payload = { occurred_on: data.occurred, counterparty: data.party, description: data.description, supply_amount: data.supply_amount, tax_amount: data.tax_amount, total_amount: data.total_amount };
      if (!editing) payload.business_id = state.businessId;
    }
    try {
      form.querySelector("button[type=submit]").disabled = true;
      await request(path, { method: editing ? "PATCH" : "POST", body: JSON.stringify(payload) });
      closeDrawer();
      await load();
    } catch (error) {
      const note = document.querySelector("[data-form-error]");
      if (note) { note.textContent = recovery(error); note.classList.remove("hidden"); }
      form.querySelector("button[type=submit]").disabled = false;
    }
  }

  async function removeRow(row) {
    if (!window.confirm("이 수기 거래를 삭제하시겠습니까?")) return;
    const kind = views[state.view].kind;
    const path = kind === "bank" ? `/ledger-bank-transactions/${encodeURIComponent(row.id)}`
      : kind === "card" ? `/card-transactions/${encodeURIComponent(row.id)}`
        : `/ledger-entries/${kind}/${encodeURIComponent(row.id)}`;
    try {
      await request(path, { method: "DELETE" });
      closeDrawer();
      await load();
    } catch (error) {
      window.alert(recovery(error));
    }
  }

  async function uploadFile() {
    const input = viewRoot()?.querySelector("[data-ledger-file]");
    const file = input?.files?.[0];
    if (!file || !state.businessId) return window.alert("사업자와 업로드 파일을 선택하십시오.");
    const form = new FormData();
    form.append("business_id", state.businessId);
    if (views[state.view].kind !== "card") form.append("category", views[state.view].upload);
    form.append("file", file);
    try {
      await request(views[state.view].kind === "card" ? "/card-uploads" : "/uploads", { method: "POST", body: form });
      await load();
    } catch (error) {
      window.alert(recovery(error));
    }
  }

  async function retryUpload(uploadId) {
    if (!window.confirm("이 업로드와 파생 원장 행을 삭제하고 재업로드 대기 상태로 전환하시겠습니까?")) return;
    try {
      const base = views[state.view].kind === "card" ? "/card-uploads" : "/uploads";
      await request(`${base}/${encodeURIComponent(uploadId)}`, { method: "DELETE" });
      await load();
      viewRoot()?.querySelector("[data-ledger-file]")?.click();
    } catch (error) {
      window.alert(recovery(error));
    }
  }

  async function open(view) {
    if (!views[view]) return;
    state.view = view;
    state.error = "";
    state.rows = [];
    state.uploads = [];
    await load();
  }

  function setBusinesses(rows) {
    state.businesses = Array.isArray(rows) ? rows : [];
    if (!state.businesses.some(item => item.id === state.businessId)) state.businessId = state.businesses[0]?.id || "";
    if (state.view) load();
  }

  window.obysLedgerDetails = { open, setBusinesses, closeDrawer };
  installStyle();
})();
