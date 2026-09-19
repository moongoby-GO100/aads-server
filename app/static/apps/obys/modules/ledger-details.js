(function () {
  "use strict";

  const API = "/api/v1/yeoljeong-finance";
  const MAX_FILE_BYTES = 10 * 1024 * 1024;
  const views = {
    salesLedger: {
      kind: "sales",
      upload: "sales",
      eyebrow: "SALES LEDGER",
      title: "매출 상세",
      description: "사업자별 매출을 조회하고 수기 거래와 엑셀 원장을 한 화면에서 관리합니다.",
      accent: "#2563eb",
      totalLabel: "조회 매출액",
      columns: ["거래일자", "매출액", "거래처", "적요"],
      sample: ["2026-09-01", "125000", "네이버페이", "온라인 주문 매출"]
    },
    purchaseLedger: {
      kind: "purchase",
      upload: "purchase",
      eyebrow: "PURCHASE LEDGER",
      title: "매입 상세",
      description: "매입처별 비용과 증빙 원장을 확인하고 엑셀 내역을 사업자 장부에 반영합니다.",
      accent: "#7c3aed",
      totalLabel: "조회 매입액",
      columns: ["거래일자", "매입액", "매입처", "적요"],
      sample: ["2026-09-01", "83000", "식자재상사", "9월 식자재"]
    },
    bankLedger: {
      kind: "bank",
      upload: "transaction",
      eyebrow: "BANK TRANSACTIONS",
      title: "은행 거래내역",
      description: "계좌 수집 내역과 업로드한 은행 엑셀을 합쳐 입금·출금 흐름을 확인합니다.",
      accent: "#047857",
      totalLabel: "입금 합계",
      columns: ["거래일자", "금액", "거래처", "적요"],
      sample: ["2026-09-01", "550000", "배달정산", "신한 운영계좌 입금"]
    },
    cardLedger: {
      kind: "card",
      upload: "card",
      eyebrow: "CARD TRANSACTIONS",
      title: "카드 사용내역",
      description: "카드 사용내역을 가맹점·금액별로 조회하고 카드사 엑셀을 일괄 등록합니다.",
      accent: "#c2410c",
      totalLabel: "카드 사용액",
      columns: ["이용일", "이용금액", "가맹점명", "적요"],
      sample: ["2026-09-01", "45000", "온라인몰", "소모품 구입"]
    }
  };

  const state = {
    businesses: [],
    view: "",
    businessId: "",
    rows: [],
    uploads: [],
    dateFrom: "",
    dateTo: "",
    query: "",
    source: "all",
    loading: false,
    error: "",
    uploadFile: null,
    uploadBusy: false,
    uploadMessage: ""
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

  function rowDate(row) {
    return String(row.occurred_at || row.occurred_date || row.occurred_on || "").slice(0, 10);
  }

  function rowAmount(row) {
    return Number(row.total_amount ?? row.amount ?? 0) || 0;
  }

  function rowSource(row) {
    return String(row.source || (row.upload_id ? "upload" : "manual"));
  }

  function selectedBusinessName() {
    return state.businesses.find(item => item.id === state.businessId)?.name || "사업자 미선택";
  }

  function recovery(error) {
    if (Number(error?.status) === 401) return "로그인 세션이 만료되었습니다. 다시 로그인한 뒤 재시도하십시오.";
    if (Number(error?.status) === 403 || Number(error?.status) === 404) {
      return "현재 계정에서 이 사업자 원장을 사용할 수 없습니다. 사업자 권한을 확인하십시오.";
    }
    return error?.message || "데이터를 불러오지 못했습니다.";
  }

  function viewRoot() {
    return document.getElementById(`${state.view}View`);
  }

  function businessOptions() {
    return state.businesses.map(item => `<option value="${escapeHtml(item.id)}"${item.id === state.businessId ? " selected" : ""}>${escapeHtml(item.name || item.id)}</option>`).join("");
  }

  function filteredRows() {
    const query = state.query.trim().toLowerCase();
    return state.rows.filter(row => {
      const sourceMatch = state.source === "all" || rowSource(row) === state.source;
      const date = rowDate(row);
      const dateMatch = (!state.dateFrom || date >= state.dateFrom) && (!state.dateTo || date <= state.dateTo);
      const searchMatch = !query || [
        row.counterparty, row.merchant, row.description, row.memo, row.account_label,
        row.card_number_masked, row.amount, row.total_amount
      ].some(value => String(value || "").toLowerCase().includes(query));
      return sourceMatch && dateMatch && searchMatch;
    });
  }

  function kpiHtml(meta) {
    const rows = filteredRows();
    const manualCount = rows.filter(row => rowSource(row) === "manual").length;
    const uploadCount = rows.length - manualCount;
    let primary = rows.reduce((sum, row) => sum + rowAmount(row), 0);
    let secondaryLabel = "파일 반영 건";
    let secondaryValue = `${uploadCount.toLocaleString("ko-KR")}건`;
    if (meta.kind === "bank") {
      primary = rows.filter(row => row.direction !== "out").reduce((sum, row) => sum + rowAmount(row), 0);
      secondaryLabel = "출금 합계";
      secondaryValue = money(rows.filter(row => row.direction === "out").reduce((sum, row) => sum + rowAmount(row), 0));
    }
    return `
      <div class="ledger-kpis" aria-label="${escapeHtml(meta.title)} 요약">
        <article><small>조회 거래</small><strong>${rows.length.toLocaleString("ko-KR")}건</strong><span>선택 기간·검색 조건</span></article>
        <article><small>${escapeHtml(meta.totalLabel)}</small><strong>${money(primary)}</strong><span>현재 표시 행 합계</span></article>
        <article><small>${escapeHtml(secondaryLabel)}</small><strong>${secondaryValue}</strong><span>엑셀·CSV 원장 기준</span></article>
        <article><small>수기 등록 건</small><strong>${manualCount.toLocaleString("ko-KR")}건</strong><span>직접 수정 가능한 거래</span></article>
      </div>`;
  }

  function rowCells(row, kind) {
    if (kind === "bank") {
      const direction = row.direction === "out" ? "출금" : row.direction === "in" ? "입금" : "파일";
      return [dateText(row.occurred_at || row.occurred_on), row.counterparty || row.memo || row.description || "-", direction, money(row.amount), row.account_alias || row.account_label || "-", rowSource(row)];
    }
    if (kind === "card") {
      return [dateText(row.occurred_at || row.occurred_on), row.merchant || row.counterparty || "-", row.description || "-", money(row.total_amount ?? row.amount), row.card_number_masked || "파일 등록", rowSource(row)];
    }
    return [
      dateText(row.occurred_on), row.counterparty || "-", row.description || "-",
      money(row.supply_amount ?? row.amount), money(row.tax_amount), money(row.total_amount ?? row.amount), rowSource(row)
    ];
  }

  function headers(kind) {
    if (kind === "bank") return ["거래일", "거래처·적요", "구분", "금액", "계좌", "등록 방식"];
    if (kind === "card") return ["사용일", "가맹점", "적요", "합계", "카드", "등록 방식"];
    return ["거래일", "거래처", "적요", "공급가", "세액", "합계", "등록 방식"];
  }

  function contentHtml(kind) {
    if (!state.businesses.length) return `<div class="ledger-state error"><strong>등록된 사업자가 없습니다.</strong><span>사업자·지점 메뉴에서 사업자 기초정보를 먼저 등록하십시오.</span></div>`;
    if (state.loading) return `<div class="ledger-state loading"><strong>원장을 불러오는 중입니다.</strong><span>사업자 권한과 서버 원장을 확인하고 있습니다.</span></div>`;
    if (state.error) return `<div class="ledger-state error"><strong>원장을 불러오지 못했습니다.</strong><span>${escapeHtml(state.error)}</span><button type="button" data-ledger-refresh>다시 시도</button></div>`;
    const rows = filteredRows();
    if (!rows.length) return `<div class="ledger-state"><strong>조건에 맞는 거래가 없습니다.</strong><span>기간·검색 조건을 바꾸거나 아래 엑셀 등록 화면에서 원장을 추가하십시오.</span><button type="button" class="primary" data-ledger-upload-focus>엑셀 파일 등록</button></div>`;
    return `<div class="ledger-list-card">
      <div class="ledger-card-head"><div><h2>거래 목록</h2><span>행을 누르면 원본 출처와 상세 금액을 확인할 수 있습니다.</span></div><b>${rows.length.toLocaleString("ko-KR")}건</b></div>
      <div class="ledger-table-wrap"><table class="ledger-table"><thead><tr>${headers(kind).map(label => `<th>${escapeHtml(label)}</th>`).join("")}<th>작업</th></tr></thead>
      <tbody>${rows.map((row, index) => {
        const mutable = rowSource(row) === "manual";
        return `<tr data-detail-index="${index}">${rowCells(row, kind).map(value => `<td>${escapeHtml(value)}</td>`).join("")}<td class="ledger-row-actions"><button type="button" data-ledger-detail="${index}">상세</button>${mutable ? `<button type="button" data-ledger-edit="${index}">수정</button>` : ""}</td></tr>`;
      }).join("")}</tbody></table></div></div>`;
  }

  function statusLabel(status) {
    return ({
      imported: "반영 완료",
      duplicate: "중복 확인",
      rejected: "반려",
      pending_review: "검토 필요"
    })[status] || status || "확인 중";
  }

  function uploadHistoryHtml() {
    if (!state.uploads.length) return `<div class="ledger-upload-empty">아직 등록한 파일이 없습니다.</div>`;
    return state.uploads.map(item => `
      <article class="ledger-upload-item">
        <div class="ledger-file-icon" aria-hidden="true">XL</div>
        <div class="ledger-upload-copy"><b>${escapeHtml(item.original_filename || "원장 파일")}</b>
          <span>${escapeHtml(dateText(item.created_at))} · ${Number(item.byte_size || 0).toLocaleString("ko-KR")} bytes</span>
          <small>반영 ${Number(item.imported_rows || 0)} · 중복 ${Number(item.duplicate_rows || 0)} · 반려 ${Number(item.rejected_rows || 0)}</small>
        </div>
        <span class="ledger-upload-status ${escapeHtml(item.status || "")}">${escapeHtml(statusLabel(item.status))}</span>
        <div class="ledger-upload-actions"><button type="button" data-upload-download="${escapeHtml(item.id)}">원본 받기</button><button type="button" class="ledger-danger" data-upload-delete="${escapeHtml(item.id)}">삭제·재등록</button></div>
      </article>`).join("");
  }

  function uploadHtml(meta) {
    const file = state.uploadFile;
    const messageClass = state.uploadMessage.startsWith("오류:") ? " error" : "";
    const completed = state.uploadMessage.startsWith("등록 완료:") || state.uploadMessage.startsWith("동일한 파일");
    const activeStep = state.uploadBusy || completed ? 3 : file ? 2 : 1;
    const stepClass = step => step === activeStep ? "active" : step < activeStep ? "done" : "";
    return `<section class="ledger-upload-panel" id="ledgerUploadSection" aria-labelledby="ledgerUploadTitle">
      <div class="ledger-upload-heading"><div><span class="ledger-eyebrow">EXCEL IMPORT</span><h2 id="ledgerUploadTitle">${escapeHtml(meta.title)} 엑셀 파일 등록</h2><p>엑셀 또는 CSV를 선택하면 행을 해석해 현재 사업자의 서버 원장에 반영합니다.</p></div>
        <button type="button" data-template-download>등록 양식 내려받기</button></div>
      <div class="ledger-upload-steps" aria-label="파일 등록 순서">
        <span class="${stepClass(1)}"><b>1</b> 파일 선택</span><span class="${stepClass(2)}"><b>2</b> 사업자·열 확인</span><span class="${stepClass(3)}"><b>3</b> 서버 반영 결과</span>
      </div>
      <div class="ledger-upload-grid">
        <div class="ledger-drop-zone${file ? " has-file" : ""}" data-upload-drop tabindex="0" role="button" aria-label="엑셀 파일 선택">
          <input type="file" data-ledger-file accept=".xlsx,.csv,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,text/csv">
          <div class="ledger-drop-icon" aria-hidden="true">XLSX</div>
          <strong>${file ? escapeHtml(file.name) : "엑셀 파일을 여기로 끌어오십시오"}</strong>
          <span>${file ? `${(file.size / 1024).toFixed(1)} KB · 등록 준비 완료` : "또는 눌러서 .xlsx / .csv 파일 선택"}</span>
          <small>최대 10MB · 첫 행은 열 제목이어야 합니다.</small>
        </div>
        <div class="ledger-upload-guide">
          <h3>현재 등록 대상</h3>
          <dl><div><dt>사업자</dt><dd>${escapeHtml(selectedBusinessName())}</dd></div><div><dt>원장</dt><dd>${escapeHtml(meta.title)}</dd></div><div><dt>필수 열</dt><dd>${meta.columns.slice(0, 2).map(escapeHtml).join(", ")}</dd></div></dl>
          <p><b>권장 열:</b> ${meta.columns.map(escapeHtml).join(" · ")}</p>
          <button type="button" class="primary" data-ledger-upload ${!file || state.uploadBusy ? "disabled" : ""}>${state.uploadBusy ? "서버에 반영 중…" : "선택한 파일 등록"}</button>
          <div class="ledger-upload-message${messageClass}" aria-live="polite">${escapeHtml(state.uploadMessage || "파일 선택 후 사업자와 원장 구분을 확인하십시오.")}</div>
        </div>
      </div>
      <div class="ledger-history-head"><div><h3>최근 등록 파일</h3><p>처리 건수와 오류를 확인하고 원본을 다시 받을 수 있습니다.</p></div><button type="button" data-ledger-refresh>이력 새로고침</button></div>
      <div class="ledger-upload-history">${uploadHistoryHtml()}</div>
    </section>`;
  }

  function render() {
    const root = viewRoot();
    const meta = views[state.view];
    if (!root || !meta) return;
    root.style.setProperty("--ledger-accent", meta.accent);
    const manualButton = `<button type="button" data-ledger-create>수기 거래 등록</button>`;
    root.innerHTML = `
      <div class="ledger-shell">
        <header class="ledger-hero"><div><span class="ledger-eyebrow">${escapeHtml(meta.eyebrow)}</span><h1>${escapeHtml(meta.title)}</h1><p>${escapeHtml(meta.description)}</p></div>
          <div class="ledger-hero-actions"><button type="button" class="primary" data-ledger-upload-focus>엑셀 파일 등록</button>${manualButton}</div></header>
        <div class="ledger-toolbar">
          <label>사업자<select data-ledger-business>${businessOptions()}</select></label>
          <label>시작일<input type="date" data-ledger-from value="${escapeHtml(state.dateFrom)}"></label>
          <label>종료일<input type="date" data-ledger-to value="${escapeHtml(state.dateTo)}"></label>
          <label class="ledger-search">검색<input type="search" data-ledger-search value="${escapeHtml(state.query)}" placeholder="거래처·적요·금액 검색"></label>
          <label>등록 방식<select data-ledger-source><option value="all">전체</option><option value="manual"${state.source === "manual" ? " selected" : ""}>수기</option><option value="upload"${state.source === "upload" ? " selected" : ""}>파일</option></select></label>
          <button type="button" data-ledger-filter>조회</button>
        </div>
        <div data-ledger-kpis>${kpiHtml(meta)}</div>
        <div data-ledger-content>${contentHtml(meta.kind)}</div>
        ${uploadHtml(meta)}
      </div>`;
    bindRoot(root);
  }

  function bindRoot(root) {
    root.querySelector("[data-ledger-business]")?.addEventListener("change", event => {
      state.businessId = event.target.value;
      state.uploadFile = null;
      state.uploadMessage = "";
      load();
    });
    root.querySelector("[data-ledger-filter]")?.addEventListener("click", () => {
      state.dateFrom = root.querySelector("[data-ledger-from]")?.value || "";
      state.dateTo = root.querySelector("[data-ledger-to]")?.value || "";
      state.query = root.querySelector("[data-ledger-search]")?.value || "";
      state.source = root.querySelector("[data-ledger-source]")?.value || "all";
      render();
    });
    root.querySelector("[data-ledger-search]")?.addEventListener("keydown", event => {
      if (event.key === "Enter") root.querySelector("[data-ledger-filter]")?.click();
    });
    root.querySelector("[data-ledger-source]")?.addEventListener("change", event => {
      state.source = event.target.value;
      render();
    });
    root.querySelectorAll("[data-ledger-refresh]").forEach(button => button.addEventListener("click", load));
    root.querySelectorAll("[data-ledger-upload-focus]").forEach(button => button.addEventListener("click", () => {
      root.querySelector("#ledgerUploadSection")?.scrollIntoView({ behavior: "smooth", block: "start" });
      setTimeout(() => root.querySelector("[data-upload-drop]")?.focus(), 350);
    }));
    root.querySelector("[data-ledger-create]")?.addEventListener("click", () => openForm());
    const fileInput = root.querySelector("[data-ledger-file]");
    fileInput?.addEventListener("change", () => chooseFile(fileInput.files?.[0] || null));
    const drop = root.querySelector("[data-upload-drop]");
    drop?.addEventListener("click", event => {
      if (event.target !== fileInput) fileInput?.click();
    });
    drop?.addEventListener("keydown", event => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        fileInput?.click();
      }
    });
    ["dragenter", "dragover"].forEach(type => drop?.addEventListener(type, event => {
      event.preventDefault();
      drop.classList.add("dragging");
    }));
    ["dragleave", "drop"].forEach(type => drop?.addEventListener(type, event => {
      event.preventDefault();
      drop.classList.remove("dragging");
    }));
    drop?.addEventListener("drop", event => chooseFile(event.dataTransfer?.files?.[0] || null));
    root.querySelector("[data-ledger-upload]")?.addEventListener("click", uploadFile);
    root.querySelector("[data-template-download]")?.addEventListener("click", downloadTemplate);
    const rows = filteredRows();
    root.querySelectorAll("[data-ledger-detail]").forEach(button => button.addEventListener("click", event => {
      event.stopPropagation();
      openDetail(rows[Number(button.dataset.ledgerDetail)]);
    }));
    root.querySelectorAll("[data-ledger-edit]").forEach(button => button.addEventListener("click", event => {
      event.stopPropagation();
      openForm(rows[Number(button.dataset.ledgerEdit)]);
    }));
    root.querySelectorAll("tr[data-detail-index]").forEach(row => row.addEventListener("click", () => openDetail(rows[Number(row.dataset.detailIndex)])));
    root.querySelectorAll("[data-upload-download]").forEach(button => button.addEventListener("click", () => downloadUpload(button.dataset.uploadDownload)));
    root.querySelectorAll("[data-upload-delete]").forEach(button => button.addEventListener("click", () => deleteUpload(button.dataset.uploadDelete)));
  }

  function chooseFile(file) {
    state.uploadMessage = "";
    if (!file) {
      state.uploadFile = null;
      render();
      return;
    }
    const extension = file.name.toLowerCase().split(".").pop();
    if (!["xlsx", "csv"].includes(extension)) {
      state.uploadFile = null;
      state.uploadMessage = "오류: .xlsx 또는 .csv 파일만 등록할 수 있습니다.";
    } else if (file.size > MAX_FILE_BYTES) {
      state.uploadFile = null;
      state.uploadMessage = "오류: 파일은 10MB 이하여야 합니다.";
    } else {
      state.uploadFile = file;
      state.uploadMessage = `${file.name}을 ${selectedBusinessName()} ${views[state.view].title}에 등록할 준비가 됐습니다.`;
    }
    render();
  }

  function dateQuery() {
    return `${state.dateFrom ? `&date_from=${encodeURIComponent(state.dateFrom)}` : ""}${state.dateTo ? `&date_to=${encodeURIComponent(state.dateTo)}` : ""}`;
  }

  async function ensureBusinesses(force = false) {
    if (state.businesses.length && !force) return;
    const payload = await request("/tenant-registry/businesses");
    state.businesses = payload.businesses || [];
    if (!state.businesses.some(item => item.id === state.businessId)) state.businessId = state.businesses[0]?.id || "";
  }

  function normalizeUploaded(row) {
    return {
      ...row,
      occurred_at: row.occurred_at || row.occurred_on,
      total_amount: row.total_amount ?? row.amount,
      supply_amount: row.supply_amount ?? row.amount,
      tax_amount: row.tax_amount ?? 0,
      merchant: row.merchant || row.counterparty,
      source: "upload"
    };
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
      const uploadCategory = encodeURIComponent(meta.upload);
      if (meta.kind === "sales" || meta.kind === "purchase") {
        const [manual, uploaded, uploads] = await Promise.all([
          request(`/ledger-entries?${scope}&category=${meta.kind}${dateQuery()}`),
          request(`/uploaded-ledger?${scope}&category=${uploadCategory}&limit=500`),
          request(`/uploads?${scope}&category=${uploadCategory}`)
        ]);
        state.rows = [...(manual.entries || []), ...(uploaded.rows || []).map(normalizeUploaded)];
        state.uploads = uploads.uploads || [];
      } else if (meta.kind === "card") {
        const [manual, uploads] = await Promise.all([
          request(`/card-transactions?${scope}${dateQuery()}`),
          request(`/card-uploads?${scope}`)
        ]);
        state.rows = manual.card_transactions || [];
        state.uploads = uploads.uploads || [];
      } else {
        const [bank, uploaded, uploads] = await Promise.all([
          request(`/ledger-bank-transactions?${scope}${dateQuery()}`),
          request(`/uploaded-ledger?${scope}&category=transaction&limit=500`),
          request(`/uploads?${scope}&category=transaction`)
        ]);
        state.rows = [...(bank.bank_transactions || []), ...(uploaded.rows || []).map(normalizeUploaded)];
        state.uploads = uploads.uploads || [];
      }
      state.rows.sort((a, b) => rowDate(b).localeCompare(rowDate(a)));
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
      ["등록 방식", rowSource(row) === "manual" ? "수기 등록" : "파일 등록"],
      ["생성시각", row.created_at]
    ];
    if (kind === "bank") common.splice(3, 0, ["입·출금", row.direction === "out" ? `출금 ${money(row.amount)}` : row.direction === "in" ? `입금 ${money(row.amount)}` : money(row.amount)], ["계좌", row.account_alias || row.account_label || "-"]);
    else common.splice(3, 0, ["공급가", money(row.supply_amount ?? row.amount)], ["세액", money(row.tax_amount)], ["합계", money(row.total_amount ?? row.amount)]);
    if (kind === "card") common.splice(3, 0, ["카드", row.card_number_masked || "파일 등록"]);
    return common;
  }

  function drawer(html) {
    closeDrawer();
    const backdrop = document.createElement("div");
    backdrop.className = "ledger-drawer-backdrop";
    backdrop.dataset.ledgerDrawer = "true";
    backdrop.style.setProperty("--ledger-accent", views[state.view]?.accent || "#2563eb");
    backdrop.innerHTML = `<aside class="ledger-drawer" role="dialog" aria-modal="true">${html}</aside>`;
    backdrop.addEventListener("click", event => { if (event.target === backdrop) closeDrawer(); });
    backdrop.addEventListener("keydown", event => { if (event.key === "Escape") closeDrawer(); });
    document.body.appendChild(backdrop);
    backdrop.querySelector("[data-drawer-close]")?.addEventListener("click", closeDrawer);
    backdrop.querySelector("[data-drawer-close]")?.focus();
    return backdrop;
  }

  function closeDrawer() {
    document.querySelector("[data-ledger-drawer]")?.remove();
  }

  function openDetail(row) {
    if (!row) return;
    const kind = views[state.view].kind;
    const mutable = rowSource(row) === "manual";
    const panel = drawer(`<div class="ledger-drawer-head"><div><span class="ledger-eyebrow">TRANSACTION DETAIL</span><h3>거래 상세</h3></div><button type="button" data-drawer-close aria-label="닫기">닫기</button></div>
      <div class="ledger-detail-grid">${detailFields(row).map(([label, value]) => `<div><b>${escapeHtml(label)}</b>${escapeHtml(value || "-")}</div>`).join("")}</div>
      <p class="ledger-source">${mutable ? "수기 등록 건은 수정·삭제할 수 있습니다." : "업로드 원본 행은 보존 정책에 따라 수정·삭제할 수 없습니다."}</p>
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
    const panel = drawer(`<div class="ledger-drawer-head"><div><span class="ledger-eyebrow">MANUAL ENTRY</span><h3>${editing ? "수기 거래 수정" : "신규 수기 등록"}</h3></div><button type="button" data-drawer-close>닫기</button></div>
      <form class="ledger-form" data-ledger-form>${fields}<div class="ledger-form-actions"><button type="button" data-drawer-close>취소</button><button type="submit" class="primary">저장</button></div></form><p class="ledger-form-error hidden" data-form-error></p>`);
    panel.querySelector("[data-ledger-form]")?.addEventListener("submit", event => saveRow(event, row));
    panel.querySelectorAll("[data-drawer-close]").forEach(button => button.addEventListener("click", closeDrawer));
    const supply = panel.querySelector('[name="supply_amount"]');
    const tax = panel.querySelector('[name="tax_amount"]');
    const total = panel.querySelector('[name="total_amount"]');
    [supply, tax].forEach(input => input?.addEventListener("input", () => {
      total.value = String((Number(supply.value || 0) + Number(tax.value || 0)).toFixed(2));
    }));
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
      payload = {
        account_label: data.account_label,
        occurred_at: isoWithZone(data.occurred_at),
        direction: data.direction,
        amount: Number(data.amount),
        balance: data.balance === "" ? null : Number(data.balance),
        counterparty: data.counterparty,
        memo: data.memo,
        category: data.direction === "in" ? "입금" : "출금"
      };
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
      form.querySelector('button[type="submit"]').disabled = true;
      await request(path, { method: editing ? "PATCH" : "POST", body: JSON.stringify(payload) });
      closeDrawer();
      await load();
    } catch (error) {
      const note = document.querySelector("[data-form-error]");
      if (note) {
        note.textContent = recovery(error);
        note.classList.remove("hidden");
      }
      form.querySelector('button[type="submit"]').disabled = false;
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
    const file = state.uploadFile;
    if (!file || !state.businessId || state.uploadBusy) return;
    state.uploadBusy = true;
    state.uploadMessage = "파일을 안전하게 전송하고 원장 행을 확인하고 있습니다.";
    render();
    const form = new FormData();
    form.append("business_id", state.businessId);
    if (views[state.view].kind !== "card") form.append("category", views[state.view].upload);
    form.append("file", file);
    try {
      const base = views[state.view].kind === "card" ? "/card-uploads" : "/uploads";
      const payload = await request(base, { method: "POST", body: form });
      state.uploadFile = null;
      state.uploadMessage = payload.status === "duplicate"
        ? "동일한 파일이 이미 등록되어 중복 반영하지 않았습니다."
        : payload.status === "pending_review"
          ? "등록 완료: 파일 검토 대기 중입니다."
        : `등록 완료: ${Number(payload.imported_rows || 0)}행 반영, ${Number(payload.rejected_rows || 0)}행 반려`;
      await load();
    } catch (error) {
      state.uploadMessage = `오류: ${recovery(error)}`;
    } finally {
      state.uploadBusy = false;
      render();
    }
  }

  async function downloadUpload(uploadId) {
    try {
      const base = views[state.view].kind === "card" ? "/card-uploads" : "/uploads";
      const response = await fetch(`${API}${base}/${encodeURIComponent(uploadId)}/download`, {
        headers: token() ? { Authorization: `Bearer ${token()}` } : {}
      });
      if (!response.ok) {
        const payload = await response.json().catch(() => ({}));
        throw new Error(payload.detail || `다운로드 ${response.status}`);
      }
      const blob = await response.blob();
      const disposition = response.headers.get("content-disposition") || "";
      const filename = decodeURIComponent(disposition.match(/filename\*=UTF-8''([^;]+)/i)?.[1] || "ledger-upload");
      const link = document.createElement("a");
      link.href = URL.createObjectURL(blob);
      link.download = filename;
      link.click();
      URL.revokeObjectURL(link.href);
    } catch (error) {
      state.uploadMessage = `오류: ${recovery(error)}`;
      render();
    }
  }

  async function deleteUpload(uploadId) {
    if (!window.confirm("이 파일과 파일에서 반영된 원장 행을 삭제하시겠습니까?")) return;
    try {
      const base = views[state.view].kind === "card" ? "/card-uploads" : "/uploads";
      await request(`${base}/${encodeURIComponent(uploadId)}`, { method: "DELETE" });
      state.uploadMessage = "파일을 삭제했습니다. 같은 파일을 다시 등록할 수 있습니다.";
      await load();
    } catch (error) {
      state.uploadMessage = `오류: ${recovery(error)}`;
      render();
    }
  }

  function csvCell(value) {
    return `"${String(value).replace(/"/g, '""')}"`;
  }

  function downloadTemplate() {
    const meta = views[state.view];
    const csv = "\ufeff" + [meta.columns, meta.sample].map(row => row.map(csvCell).join(",")).join("\r\n");
    const link = document.createElement("a");
    link.href = URL.createObjectURL(new Blob([csv], { type: "text/csv;charset=utf-8" }));
    link.download = `${meta.upload}-ledger-template.csv`;
    link.click();
    URL.revokeObjectURL(link.href);
  }

  async function open(view) {
    if (!views[view]) return;
    const changedView = state.view !== view;
    state.view = view;
    state.error = "";
    state.rows = [];
    state.uploads = [];
    if (changedView) {
      state.query = "";
      state.source = "all";
      state.uploadFile = null;
      state.uploadMessage = "";
    }
    try {
      await ensureBusinesses(true);
    } catch (error) {
      state.error = recovery(error);
    }
    await load();
  }

  function setBusinesses(rows) {
    state.businesses = Array.isArray(rows) ? rows : [];
    if (!state.businesses.some(item => item.id === state.businessId)) state.businessId = state.businesses[0]?.id || "";
    if (state.view) load();
  }

  window.obysLedgerDetails = { open, setBusinesses, closeDrawer };
})();
