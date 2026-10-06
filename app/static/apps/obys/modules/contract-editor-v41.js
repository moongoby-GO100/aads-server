/* 오비서 V4.1 현재 관리자 화면 — 직원 계약 작성·서명요청·서명본 관리.
 *
 * PRD AADS-LAYOUT-025(obys-admin-contract-prd v1.0.1) 구현. 기존 관리화면으로 이동하지 않고
 * 직원 현황(employees)과 계약·인사증빙(hr-docs) 두 진입점이 같은 편집기를 연다.
 *
 * 서버 API 는 기존 것을 그대로 쓴다(새 API·DB 변경 없음):
 *   GET  /employees/approved                      승인 직원(회사·점포 연결)
 *   GET  /contracts, POST /contracts              목록·저장(서버 validator 가 최종 판정)
 *   POST /contracts/{id}/request-signature        서명요청 — 계약 결과와 notify.status 가 따로 온다
 *   POST /contracts/{id}/resend-signature-notice  알림만 재전송(계약을 새로 만들지 않는다)
 *   GET  /contracts/{id}/signed-pdf               서명본 PDF
 *   POST /contracts/{id}/signed-pdf/regenerate    서명본 PDF 재생성
 * 양식·검증·미리보기는 modules/contract-core.js(기존 화면과 같은 코드)를 쓴다.
 *
 * 개인정보는 브라우저 저장소에 남기지 않는다. 작성 중 값은 이 창의 메모리에만 있고,
 * URL 에는 계약/입사요청 ID 만 남긴다(새로고침 시 서버에서 다시 읽는다).
 */
(function () {
  "use strict";
  const C = window.ObysContractCore;
  if (!C) return;
  const esc = C.escapeHtml;

  const ROUTES = new Set(["employees", "hr-docs"]);
  const S = { ctx: null, employees: [], contracts: [], businesses: [], loadedAt: 0, loading: null, loadError: null, filter: "all" };
  let E = null; // 열린 편집기 상태
  let dialog = null;

  // 화면 표시 이름 = 서버/기존 validator 의 누락 항목 이름. 오류가 오면 이 이름으로 칸을 찾는다.
  const FIELDS = [
    { g: "basic", name: "contractType", label: "계약 유형", type: "select", options: () => Object.entries(C.contractTypeLabels).filter(([k]) => k !== "confidentiality") },
    { g: "basic", name: "contractDate", label: "계약 작성일", type: "date" },
    { g: "basic", name: "startDate", label: "입사일", type: "date", altLabels: ["용역 시작일"] },
    { g: "basic", name: "endDate", label: "계약 종료일", type: "date", hint: "기간의 정함이 없으면 비워 둡니다." },
    { g: "basic", name: "workplace", label: "근무장소", type: "text", altLabels: ["용역 수행장소/방식"] },
    { g: "basic", name: "workplaceSizeCategory", label: "상시근로자 수", type: "select", options: () => [["under_5", "5인 미만"], ["over_5", "5인 이상"]] },
    { g: "basic", name: "jobDescription", label: "업무내용", type: "textarea", altLabels: ["용역 업무내용"] },
    { g: "wage", name: "wageType", label: "임금 산정 방식", type: "select", options: () => Object.entries(C.wageTypeLabels) },
    { g: "wage", name: "wage", label: "확정 임금", type: "number", altLabels: ["확정 용역비"], hint: "시급·월급·용역비 금액을 원 단위로 입력합니다. 기본값을 넣지 않습니다." },
    { g: "wage", name: "mealProvision", label: "식사 제공", type: "select", options: () => [["employer_meal", "사용자가 식사 제공"], ["cash_no_meal", "식사 미제공·현금 식대"]] },
    { g: "wage", name: "baseSalary", label: "과세 기본급", type: "number", hint: "월급제에서 구성을 나눌 때만 입력합니다. 세 항목 합계 = 확정 임금." },
    { g: "wage", name: "nonTaxMealAllowance", label: "비과세 식대", type: "number" },
    { g: "wage", name: "taxableAllowance", label: "기타 과세수당", type: "number" },
    { g: "wage", name: "payDate", label: "급여지급일", type: "text" },
    { g: "wage", name: "payMethod", label: "지급방법", type: "text" },
    { g: "wage", name: "wageComposition", label: "임금 구성/공제", type: "textarea" },
    { g: "wage", name: "probationPeriod", label: "수습", type: "textarea" },
    { g: "work", name: "workDays", label: "근무일/요일", type: "text" },
    { g: "work", name: "workTime", label: "근무시간", type: "text" },
    { g: "work", name: "restTime", label: "휴게시간", type: "text" },
    { g: "work", name: "weeklyHours", label: "주 소정근로시간", type: "text", hint: "예: 주 20시간 — '주 N시간' 형식이면 최저임금 검사가 함께 됩니다." },
    { g: "work", name: "dailyWorkSchedule", label: "근로일별 근로시간", type: "textarea", hint: "단시간 근로자는 요일별 시작·종료·휴게를 적습니다. 점포가 다르면 줄을 나눕니다." },
    { g: "work", name: "holidays", label: "휴일/주휴", type: "text" },
    { g: "work", name: "overtimeTerms", label: "연장·야간·휴일근로", type: "textarea" },
    { g: "work", name: "leaveTerms", label: "연차/휴가/결근", type: "textarea" },
    { g: "terms", name: "employmentTaxType", label: "신고 구분", type: "select", options: () => Object.entries(C.employmentTaxTypeLabels), hint: "계약 유형에 맞춰 자동으로 정해집니다." },
    { g: "terms", name: "insuranceTerms", label: "4대보험/세무 처리", type: "textarea" },
    { g: "terms", name: "mealUniformTerms", label: "식대·유니폼·보건증", type: "textarea" },
    { g: "terms", name: "terminationTerms", label: "계약 해지/기성정산 기준", type: "textarea" },
    { g: "terms", name: "confidentialityTerms", label: "비밀유지/자료보호", type: "textarea" },
    { g: "terms", name: "freelancerScope", label: "용역 업무범위/산출물", type: "textarea", only: "freelancer" },
    { g: "terms", name: "freelancerSettlementTerms", label: "용역비 정산/해지", type: "textarea", only: "freelancer" },
    { g: "terms", name: "terms", label: "추가 특약", type: "textarea" },
    { g: "employee", name: "employeeName", label: "직원명", type: "text" },
    { g: "employee", name: "employeeEmail", label: "직원 이메일", type: "text", readonly: true },
    { g: "employee", name: "employeePhone", label: "근로자 연락처", type: "text" },
    { g: "employee", name: "employeeBirthDate", label: "근로자 생년월일", type: "date" },
    { g: "employee", name: "employeeAddress", label: "근로자 주소", type: "text" },
    { g: "employee", name: "employeeNationality", label: "국적", type: "text" },
    { g: "employee", name: "foreignWorker", label: "외국인 근로자", type: "select", options: () => [["false", "해당 없음"], ["true", "외국인 근로자"]] },
    { g: "employee", name: "visaStatus", label: "체류자격", type: "text" },
    { g: "employee", name: "foreignRegistrationNoMasked", label: "외국인등록번호(마스킹)", type: "text" },
    { g: "employee", name: "minorGuardianName", label: "친권자/후견인 성명", type: "text" },
    { g: "employee", name: "minorGuardianPhone", label: "친권자/후견인 연락처", type: "text" },
    { g: "employee", name: "minorGuardianConsent", label: "친권자/후견인 동의서 확인", type: "select", options: () => [["not_applicable", "해당 없음(18세 이상)"], ["confirmed", "동의서 확인함"]] },
    { g: "employer", name: "employerName", label: "사용자 상호", type: "text" },
    { g: "employer", name: "employerRegistrationNo", label: "사업자등록번호", type: "text" },
    { g: "employer", name: "employerRepresentative", label: "대표자", type: "text" },
    { g: "employer", name: "employerPhone", label: "사용자 연락처", type: "text" },
    { g: "employer", name: "employerAddress", label: "사용자 주소", type: "text" }
  ];
  const GROUPS = [
    ["basic", "계약 기본"], ["wage", "임금"], ["work", "근무시간·휴일"], ["terms", "보험·조항"],
    ["employee", "근로자 인적사항 (가입·입사서류에서 자동 채움)"], ["employer", "사용자(사업자) 정보 (비워 두면 사업자 등록정보로 자동 채움)"]
  ];
  const FIELD_BY_LABEL = new Map();
  FIELDS.forEach(f => [f.label, ...(f.altLabels || [])].forEach(label => FIELD_BY_LABEL.set(label, f.name)));
  const NUMERIC = new Set(["wage", "baseSalary", "nonTaxMealAllowance", "taxableAllowance"]);
  const EMPLOYER_FIELDS = ["employerName", "employerRegistrationNo", "employerRepresentative", "employerAddress"];
  // 서버 기본값이 실제 금액을 정하면 안 된다(PRD: 실제 금액 기본값 추정 금지).
  const NO_DEFAULT = new Set(["wage", "baseSalary"]);
  const PASS_THROUGH = ["bankName", "bankAccountHolder", "bankAccountMasked", "healthCertificateValidUntil", "onboardingDocumentSummary"];

  // ---------- 값 변환 ----------
  const snake = key => key.replace(/[A-Z]/g, ch => "_" + ch.toLowerCase());
  const pick = (obj, camel) => {
    const value = obj?.[snake(camel)] ?? obj?.[camel];
    return value === null || value === undefined ? "" : value;
  };
  function statusOf(contract) {
    const key = String(contract?.status || "draft");
    return [key, ...(C.CONTRACT_STATUS[key] || [key, "info"])];
  }
  function employeeKey(employee) { return String(employee?.id || ""); }
  function contractsOf(employee) {
    const id = employeeKey(employee);
    const email = String(employee?.email || "").toLowerCase();
    const biz = String(employee?.business_id || "");
    return S.contracts
      .filter(item => String(item.status || "") !== "cancelled")
      .filter(item => String(pick(item, "employeeRequestId")) === id
        || (email && String(pick(item, "employeeEmail")).toLowerCase() === email && String(pick(item, "businessId")) === biz))
      .sort((a, b) => String(b.updated_at || "").localeCompare(String(a.updated_at || "")));
  }
  function businessName(id) {
    return S.businesses.find(item => String(item.id) === String(id))?.name || id || "사업자 미지정";
  }
  function businessRecord(id) { return S.businesses.find(item => String(item.id) === String(id)) || {}; }
  function fmtTime(value) { return String(value || "").replace("T", " ").slice(0, 16) || "-"; }

  function valuesFromEmployee(employee) {
    const business = businessRecord(employee.business_id);
    const contractType = "part_time";
    const cls = C.classificationFor(contractType);
    const values = { contractType, ...cls };
    Object.entries(C.defaultsFor(contractType, cls.employmentTaxType)).forEach(([key, value]) => { if (!NO_DEFAULT.has(key)) values[key] = value; });
    Object.assign(values, {
      contractDate: C.today(),
      startDate: "",
      endDate: "",
      workplace: employee.branch ? `${businessName(employee.business_id)} ${employee.branch}` : values.workplace,
      employeeName: employee.name || "",
      employeeEmail: employee.email || "",
      employeePhone: employee.phone || "",
      employeeBirthDate: employee.birth_date || "",
      employeeAddress: employee.address || "",
      employeeNationality: employee.nationality || "대한민국",
      foreignWorker: "false",
      minorGuardianConsent: "not_applicable",
      employerName: business.name || "",
      employerRegistrationNo: business.registration_no || "",
      employerRepresentative: business.representative || "",
      employerPhone: business.phone || "",
      employerAddress: business.address || "",
      bankName: employee.bank_name || "",
      bankAccountHolder: employee.bank_account_holder || "",
      bankAccountMasked: employee.bank_account_masked || "",
      healthCertificateValidUntil: employee.health_certificate_valid_until || "",
      onboardingDocumentSummary: employee.onboarding_document_summary || ""
    });
    return values;
  }
  function valuesFromContract(contract) {
    const values = {};
    [...FIELDS.map(f => f.name), ...PASS_THROUGH].forEach(name => {
      const value = pick(contract, name);
      values[name] = name === "foreignWorker" ? String(value === true || value === "true") : String(value ?? "");
    });
    return values;
  }

  /** 편집 값 → 기존 validateContractDraft 가 받는 draft(기존 contractDraftFromForm 과 같은 모양). */
  function draftFromValues(values, target) {
    const draft = {};
    [...FIELDS.map(f => f.name), ...PASS_THROUGH].forEach(name => {
      const raw = values[name];
      draft[name] = NUMERIC.has(name) ? Number(raw || 0) : String(raw ?? "").trim();
    });
    draft.foreignWorker = values.foreignWorker === "true";
    draft.minorGuardianConsent = values.minorGuardianConsent || "not_applicable";
    draft.wageType = draft.wageType || "hourly";
    draft.workplaceSizeCategory = draft.workplaceSizeCategory || "under_5";
    draft.mealProvision = draft.mealProvision || "employer_meal";
    draft.employeeRequestId = String(target.employee_request_id || "");
    draft.businessId = String(target.business_id || "");
    draft.branch = String(target.branch || "");
    draft.workplace = draft.workplace || draft.branch;
    return draft;
  }
  /** 서버로 보낼 본문 — 기존 화면과 같은 snake_case. 비운 사용자 정보는 서버가 사업자 등록정보로 채운다. */
  function payloadFromDraft(draft, contractId) {
    const body = { id: contractId || null };
    Object.entries(draft).forEach(([key, value]) => { body[snake(key)] = value; });
    body.memo = "V4.1 현재 관리자 화면 계약 작성";
    return body;
  }
  function checkLocally(draft) {
    // 사용자(사업자) 정보는 서버가 채우므로 비어 있어도 화면 검증에서는 막지 않는다.
    const probe = { ...draft };
    EMPLOYER_FIELDS.forEach(key => { if (!String(probe[key] || "").trim()) probe[key] = "서버 자동 채움"; });
    return C.checkDraft(probe);
  }
  function missingFromMessage(message) {
    const text = String(message || "");
    const index = text.indexOf("확인하십시오: ");
    return index < 0 ? [] : text.slice(index + "확인하십시오: ".length).split(", ").map(s => s.trim()).filter(Boolean);
  }

  // ---------- API ----------
  async function api(path, options = {}) {
    const fn = S.ctx?.api;
    if (!fn) throw new Error("화면 초기화 전입니다.");
    return fn(path, options);
  }
  async function load(force = false) {
    if (S.loading) return S.loading;
    if (!force && S.loadedAt && Date.now() - S.loadedAt < 15000) return;
    S.loading = (async () => {
      try {
        const [employees, contracts, registry] = await Promise.all([
          api("/employees/approved"),
          api("/contracts"),
          api("/tenant-registry/businesses").catch(() => ({ businesses: [] }))
        ]);
        S.employees = employees.employees || [];
        S.contracts = contracts.contracts || [];
        S.businesses = registry.businesses || [];
        S.loadedAt = Date.now();
        S.loadError = null;
      } catch (error) {
        S.loadError = error;
        throw error;
      } finally {
        S.loading = null;
      }
    })();
    return S.loading;
  }
  function errorText(error, action) {
    const status = Number(error?.status || 0);
    if (status === 401) return "로그인이 만료되었습니다. 다시 로그인한 뒤 같은 작업을 다시 누르십시오. 입력한 값은 이 창을 닫지 않는 동안 남아 있습니다.";
    if (status === 403) return `${action} 권한이 없습니다. 대표·운영관리자 계정으로 로그인했는지 확인하십시오. (${error.message})`;
    if (status === 409) return `${error.message} — 최신 상태를 다시 불러왔습니다.`;
    if (status === 429) return error.message;
    if (!status) return `${action} 결과를 확인하지 못했습니다(네트워크). 최신 상태를 다시 불러와 대조합니다.`;
    return error.message || `${action} 실패 (HTTP ${status})`;
  }

  // ---------- 목록 섹션 ----------
  function mount(route, page, ctx) {
    S.ctx = ctx;
    if (!ROUTES.has(route) || !page) return;
    page.querySelector("#contractV41")?.remove();
    const box = document.createElement("section");
    box.className = "section contract-v41";
    box.id = "contractV41";
    box.innerHTML = '<div class="section-head"><h2>…</h2></div><div class="empty">계약 현황을 불러오는 중입니다.</div>';
    const anchor = page.querySelector("#empOnboardV41") || page.querySelector(".hero");
    if (anchor) anchor.after(box); else page.prepend(box);
    renderSection(route, box);
    // 화면에 들어올 때마다 서버에서 다시 읽는다 — 다른 관리자·직원 서명으로 상태가 바뀌었을 수 있다.
    load(true).then(() => { renderSection(route, box); restoreFromUrl(); }).catch(() => renderSection(route, box));
  }
  function refresh() {
    S.loadedAt = 0;
    const box = document.getElementById("contractV41");
    const route = S.ctx?.route?.() || "";
    return load(true).then(() => { if (box && ROUTES.has(route)) renderSection(route, box); }).catch(() => { if (box && ROUTES.has(route)) renderSection(route, box); });
  }
  function renderSection(route, box) {
    if (!box.isConnected) return;
    const head = route === "employees"
      ? '<div class="section-head"><h2>승인 직원 계약</h2><p>가입을 승인한 직원은 여기서 바로 계약서를 작성하고 서명을 요청합니다.</p></div>'
      : '<div class="section-head"><h2>근로계약 작성·서명 현황</h2><p>계약서 저장, 서명요청, 알림 결과, 서명본 PDF를 한 곳에서 처리합니다.</p><span class="right"><button class="btn primary" type="button" data-cv41-new>새 계약 작성</button></span></div>';
    if (S.loadError && !S.loadedAt) {
      box.innerHTML = `${head}<div class="empty"><b>계약 정보를 불러오지 못했습니다.</b><p>${esc(errorText(S.loadError, "계약 조회"))}</p><button class="btn" type="button" data-cv41-reload>다시 불러오기</button></div>`;
    } else if (!S.loadedAt) {
      box.innerHTML = `${head}<div class="empty">계약 현황을 불러오는 중입니다.</div>`;
    } else {
      box.innerHTML = head + (route === "employees" ? employeeTable() : contractTable());
    }
    box.querySelector("[data-cv41-reload]")?.addEventListener("click", () => refresh());
    box.querySelector("[data-cv41-new]")?.addEventListener("click", () => openEditor({}));
    box.querySelectorAll("[data-cv41-filter]").forEach(button => button.addEventListener("click", () => { S.filter = button.dataset.cv41Filter; renderSection(route, box); }));
    box.querySelectorAll("[data-cv41-employee]").forEach(button => button.addEventListener("click", () => {
      const employee = S.employees.find(item => employeeKey(item) === button.dataset.cv41Employee);
      const latest = employee && contractsOf(employee)[0];
      openEditor(latest ? { contract: latest } : { employee });
    }));
    box.querySelectorAll("[data-cv41-contract]").forEach(button => button.addEventListener("click", () => {
      const contract = S.contracts.find(item => String(item.id) === button.dataset.cv41Contract);
      if (contract) openEditor({ contract });
    }));
    box.querySelectorAll("[data-cv41-pdf]").forEach(button => button.addEventListener("click", () => downloadPdf(button.dataset.cv41Pdf, button)));
  }
  function badge([, text, tone]) { return `<span class="badge ${esc(tone)}">${esc(text)}</span>`; }
  function employeeTable() {
    if (!S.employees.length) return '<div class="empty">승인된 직원이 없습니다. 위 가입요청을 승인하면 여기에 표시됩니다.</div>';
    const rows = S.employees.map(employee => {
      const latest = contractsOf(employee)[0];
      const [key] = latest ? statusOf(latest) : ["none"];
      const action = !latest ? "계약 작성" : key === "draft" ? "이어서 작성" : key === "requested" ? "서명 현황" : "계약 보기";
      const docs = Number(employee.onboarding_document_count || 0);
      return `<tr>
        <td data-label="직원"><b>${esc(employee.name || "이름 없음")}</b><small>${esc(employee.email_masked || "")}</small></td>
        <td data-label="사업자·지점">${esc(businessName(employee.business_id))}<small>${esc(employee.branch || "지점 미정")}</small></td>
        <td data-label="입사서류">${docs ? `${docs}건` : '<span class="badge warn">미제출</span>'}</td>
        <td data-label="계약">${latest ? `${badge(statusOf(latest))}<small>${esc(fmtTime(latest.updated_at))}</small>` : '<span class="badge bad">작성 필요</span>'}</td>
        <td data-label="처리"><button class="btn ${latest ? "" : "primary"}" type="button" data-cv41-employee="${esc(employeeKey(employee))}">${action}</button></td>
      </tr>`;
    }).join("");
    return `<div class="table-wrap"><table class="cv41-table"><thead><tr><th>직원</th><th>사업자·지점</th><th>입사서류</th><th>계약</th><th>처리</th></tr></thead><tbody>${rows}</tbody></table></div>`;
  }
  function contractTable() {
    const counts = { all: 0, draft: 0, requested: 0, signed: 0 };
    S.contracts.forEach(item => { counts.all += 1; const key = statusOf(item)[0]; if (key in counts) counts[key] += 1; });
    const filters = [["all", "전체"], ["draft", "작성중"], ["requested", "서명요청"], ["signed", "서명완료"]]
      .map(([key, label]) => `<button class="btn ${S.filter === key ? "primary" : ""}" type="button" data-cv41-filter="${key}" aria-pressed="${S.filter === key}">${label} ${counts[key]}</button>`).join("");
    const list = S.contracts.filter(item => S.filter === "all" || statusOf(item)[0] === S.filter);
    const rows = list.map(contract => {
      const [key] = statusOf(contract);
      const when = key === "signed" ? `서명 ${fmtTime(contract.signed_at)}` : key === "requested" ? `요청 ${fmtTime(contract.requested_at)}` : `저장 ${fmtTime(contract.updated_at)}`;
      return `<tr>
        <td data-label="직원"><b>${esc(pick(contract, "employeeName") || "-")}</b><small>${esc(contract.employee_email_masked || "")}</small></td>
        <td data-label="사업자·지점">${esc(businessName(pick(contract, "businessId")))}<small>${esc(pick(contract, "branch") || "-")}</small></td>
        <td data-label="계약 유형">${esc(C.contractTypeLabels[pick(contract, "contractType")] || pick(contract, "contractType") || "-")}</td>
        <td data-label="상태">${badge(statusOf(contract))}<small>${esc(when)}</small></td>
        <td data-label="처리"><button class="btn" type="button" data-cv41-contract="${esc(contract.id)}">${key === "draft" ? "이어서 작성" : "열기"}</button>${key === "signed" ? ` <button class="btn" type="button" data-cv41-pdf="${esc(contract.id)}">서명본 PDF</button>` : ""}</td>
      </tr>`;
    }).join("");
    return `<div class="toolbar">${filters}</div>${list.length ? `<div class="table-wrap"><table class="cv41-table"><thead><tr><th>직원</th><th>사업자·지점</th><th>계약 유형</th><th>상태</th><th>처리</th></tr></thead><tbody>${rows}</tbody></table></div>` : '<div class="empty">조건에 맞는 계약서가 없습니다. 새 계약 작성으로 시작하십시오.</div>'}`;
  }

  // ---------- 편집기 ----------
  const STEPS = ["대상 확인", "계약조건 입력", "미리보기", "저장·서명요청"];
  function ensureDialog() {
    if (dialog) return dialog;
    dialog = document.createElement("dialog");
    dialog.id = "contractEditorV41";
    dialog.className = "cv41-dialog";
    dialog.setAttribute("aria-labelledby", "cv41Title");
    dialog.addEventListener("cancel", event => { event.preventDefault(); closeEditor(); });
    document.body.append(dialog);
    return dialog;
  }
  function openEditor({ employee = null, contract = null }) {
    const target = contract
      ? { employee_request_id: pick(contract, "employeeRequestId"), business_id: pick(contract, "businessId"), branch: pick(contract, "branch") }
      : employee ? { employee_request_id: employee.id, business_id: employee.business_id, branch: employee.branch } : null;
    E = {
      employee: employee || (contract ? S.employees.find(item => employeeKey(item) === String(pick(contract, "employeeRequestId"))) : null) || null,
      target,
      contract: contract || null,
      values: contract ? valuesFromContract(contract) : employee ? valuesFromEmployee(employee) : {},
      step: contract ? (statusOf(contract)[0] === "draft" ? 1 : 3) : employee ? 1 : 0,
      dirty: false,
      previewed: false,
      busy: "",
      error: "",
      missing: [],
      notify: null,
      sessionLost: false
    };
    setUrl(contract ? { cv41_contract: contract.id } : employee ? { cv41_request: employee.id } : {});
    ensureDialog();
    render();
    if (!dialog.open) dialog.showModal();
  }
  function closeEditor() {
    if (E?.dirty && !confirm("저장하지 않은 계약 조건이 있습니다. 닫으면 입력값이 사라집니다. 닫을까요?")) return;
    E = null;
    setUrl({});
    dialog?.close();
  }
  function setUrl(params) {
    const url = new URL(location.href);
    url.searchParams.delete("cv41_contract");
    url.searchParams.delete("cv41_request");
    Object.entries(params).forEach(([key, value]) => { if (value) url.searchParams.set(key, value); });
    history.replaceState(history.state, "", url.toString());
  }
  function restoreFromUrl() {
    if (E) return;
    const params = new URLSearchParams(location.search);
    const contractId = params.get("cv41_contract");
    const requestId = params.get("cv41_request");
    if (contractId) {
      const contract = S.contracts.find(item => String(item.id) === contractId);
      if (contract) openEditor({ contract }); else { setUrl({}); S.ctx?.toast?.("열려 있던 계약서를 찾을 수 없습니다. 권한이나 삭제 여부를 확인하십시오."); }
    } else if (requestId) {
      const employee = S.employees.find(item => employeeKey(item) === requestId);
      if (employee) openEditor({ employee }); else setUrl({});
    }
  }
  function readOnly() { const key = E.contract ? statusOf(E.contract)[0] : "draft"; return key === "signed"; }

  function render() {
    if (!E || !dialog) return;
    const saved = E.contract;
    const [statusKey] = saved ? statusOf(saved) : ["new"];
    const who = E.values.employeeName || E.employee?.name || "직원 선택 전";
    const where = E.target ? `${businessName(E.target.business_id)} · ${E.target.branch || "지점 미정"}` : "회사·점포 선택 전";
    const steps = STEPS.map((label, index) => `<li class="${index === E.step ? "active" : index < E.step ? "done" : ""}" ${index === E.step ? 'aria-current="step"' : ""}>${index + 1}. ${label}</li>`).join("");
    let body = "";
    if (E.step === 0) body = stepTarget();
    else if (E.step === 1) body = stepConditions();
    else if (E.step === 2) body = stepPreview();
    else body = stepSaved();
    dialog.innerHTML = `
      <div class="modal-head cv41-head"><div><h2 id="cv41Title">근로계약서 ${saved ? "관리" : "작성"}</h2><p>${esc(who)} · ${esc(where)}${saved ? ` · ${badge(statusOf(saved))}` : ""}</p></div><button class="close" type="button" data-cv41-close aria-label="닫기">×</button></div>
      <ol class="cv41-steps">${steps}</ol>
      ${E.sessionLost ? `<div class="cv41-alert bad" role="alert"><b>로그인이 만료되었습니다.</b> 입력값은 이 창에 남아 있습니다. <a href="/static/apps/obys/index.html" target="_blank" rel="noopener">새 창에서 로그인</a>한 뒤 아래 버튼을 다시 누르십시오. 다른 계정으로 로그인하면 이 창을 닫고 새로 여십시오.</div>` : ""}
      ${E.error ? `<div class="cv41-alert bad" role="alert" id="cv41Error">${esc(E.error)}${E.missing.length ? `<ul>${E.missing.map(label => `<li>${esc(label)}</li>`).join("")}</ul>` : ""}</div>` : ""}
      <div class="cv41-body">${body}</div>
      <div class="cv41-foot">${footer(statusKey)}</div>`;
    wire();
  }
  function stepTarget() {
    const options = S.employees.map(employee => {
      const latest = contractsOf(employee)[0];
      return `<option value="${esc(employeeKey(employee))}">${esc(employee.name || "이름 없음")} · ${esc(businessName(employee.business_id))} · ${esc(employee.branch || "지점 미정")}${latest ? ` (계약 ${esc(statusOf(latest)[1])})` : ""}</option>`;
    }).join("");
    const employee = E.employee;
    const latest = employee ? contractsOf(employee)[0] : null;
    const gaps = employee ? [["연락처", employee.phone], ["생년월일", employee.birth_date], ["주소", employee.address]].filter(([, v]) => !v).map(([k]) => k) : [];
    return `
      <p class="notice">승인된 입사요청만 계약 대상이 됩니다. 한 직원이 여러 점포에서 일하면 점포(사업자)마다 따로 계약합니다.</p>
      <div class="form-field"><label for="cv41Employee">계약할 승인 직원</label><select id="cv41Employee" class="field"><option value="">직원을 고르십시오</option>${options}</select></div>
      ${S.employees.length ? "" : '<div class="empty">승인된 직원이 없습니다. 직원 현황에서 가입요청을 먼저 승인하십시오.</div>'}
      ${employee ? `<div class="detail-grid cv41-target">
        <div class="detail-cell"><small>직원</small><b>${esc(employee.name || "-")}</b><small>${esc(employee.email_masked || "")}</small></div>
        <div class="detail-cell"><small>사업자(사용자)</small><b>${esc(businessName(employee.business_id))}</b></div>
        <div class="detail-cell"><small>근무 지점</small><b>${esc(employee.branch || "지점 미정")}</b></div>
        <div class="detail-cell"><small>입사서류</small><b>${Number(employee.onboarding_document_count || 0)}건</b><small>${gaps.length ? `비어 있는 정보: ${esc(gaps.join(", "))} — 계약조건 단계에서 입력합니다.` : "인적사항 확인됨"}</small></div>
      </div>${latest ? `<p class="cv41-alert warn">이 직원에게 이미 ${esc(statusOf(latest)[1])} 계약서가 있습니다. <button class="btn" type="button" data-cv41-contract-open="${esc(latest.id)}">기존 계약 열기</button> 새로 작성하면 별도 계약서가 됩니다.</p>` : ""}` : ""}`;
  }
  function fieldControl(field) {
    const value = E.values[field.name] ?? "";
    const disabled = readOnly() || field.readonly ? "readonly" : "";
    const invalid = E.missing.some(label => FIELD_BY_LABEL.get(label) === field.name);
    const id = `cv41_${field.name}`;
    const common = `id="${id}" name="${field.name}" data-cv41-field ${invalid ? 'aria-invalid="true"' : ""} ${field.hint ? `aria-describedby="${id}_hint"` : ""}`;
    let control;
    if (field.type === "select") {
      control = `<select class="field" ${common} ${readOnly() ? "disabled" : ""}>${field.options().map(([key, label]) => `<option value="${esc(key)}" ${String(value) === String(key) ? "selected" : ""}>${esc(label)}</option>`).join("")}</select>`;
    } else if (field.type === "textarea") {
      control = `<textarea class="field" rows="3" ${common} ${disabled}>${esc(value)}</textarea>`;
    } else {
      const type = field.type === "number" ? 'type="number" inputmode="numeric" min="0" step="1"' : `type="${field.type}"`;
      control = `<input class="field" ${type} ${common} value="${esc(value)}" ${disabled}>`;
    }
    return `<div class="form-field ${invalid ? "invalid" : ""} ${field.type === "textarea" ? "wide" : ""}"><label for="${id}">${esc(field.label)}</label>${control}${field.hint ? `<small id="${id}_hint">${esc(field.hint)}</small>` : ""}</div>`;
  }
  function stepConditions() {
    const type = E.values.contractType || "part_time";
    const visible = field => !field.only || field.only === type;
    const groupHtml = GROUPS.map(([key, title]) => {
      const fields = FIELDS.filter(field => field.g === key && visible(field));
      const hasMissing = fields.some(field => E.missing.some(label => FIELD_BY_LABEL.get(label) === field.name));
      const collapsible = key === "employee" || key === "employer";
      const inner = `<div class="form-grid">${fields.map(fieldControl).join("")}</div>`;
      return collapsible
        ? `<details class="cv41-group" ${hasMissing ? "open" : ""}><summary>${esc(title)}</summary>${inner}</details>`
        : `<fieldset class="cv41-group"><legend>${esc(title)}</legend>${inner}</fieldset>`;
    }).join("");
    const lockNote = E.contract && statusOf(E.contract)[0] === "requested"
      ? '<p class="cv41-alert warn">서명요청을 보낸 계약서입니다. 조건을 바꿔 저장하면 보낸 서명 링크가 무효가 되고 서명요청을 다시 보내야 합니다.</p>' : "";
    return `${lockNote}<p class="notice">표준 조항은 초안입니다. 실제 근무표·임금으로 고쳐 쓰십시오. 금액은 자동으로 넣지 않습니다. <button class="btn" type="button" data-cv41-defaults ${readOnly() ? "disabled" : ""}>빈 칸을 표준 조항으로 채우기</button></p>${groupHtml}`;
  }
  function currentDraft() { return draftFromValues(E.values, E.target || {}); }
  function stepPreview() {
    const html = C.contractPreviewHtml(currentDraft());
    return `<p class="notice">아래는 입력한 조건 그대로의 계약서입니다. 사용자 정보가 비어 있으면 저장할 때 사업자 등록정보로 채워지고, 저장 후 화면에 서버 기준 계약서가 다시 표시됩니다.</p><article class="cv41-paper">${html}</article>`;
  }
  function notifyHtml() {
    if (!E.notify) return "";
    const status = String(E.notify.status || "");
    const ok = status === "sent" || status === "success" || status === "ok";
    const channels = (E.notify.channels || []).map(item => `<li>${esc(item.channel || "-")}: ${esc(item.status || "-")}${item.target_masked ? ` (${esc(item.target_masked)})` : ""}${item.error_detail ? ` — ${esc(String(item.error_detail).slice(0, 120))}` : ""}</li>`).join("");
    return `<div class="cv41-alert ${ok ? "good" : "warn"}" role="status"><b>${ok ? "직원에게 서명 알림을 보냈습니다." : "서명요청은 저장됐지만 알림 전송은 확인되지 않았습니다."}</b>${ok ? "" : " 서명 링크를 복사해 직접 보내거나 ‘알림 다시 보내기’를 누르십시오."}${channels ? `<ul>${channels}</ul>` : ""}</div>`;
  }
  function stepSaved() {
    const contract = E.contract;
    if (!contract) return '<div class="empty">아직 저장되지 않았습니다.</div>';
    const [key] = statusOf(contract);
    const rows = [
      ["계약서 번호", contract.id], ["상태", statusOf(contract)[1]], ["마지막 저장", fmtTime(contract.updated_at)],
      ["서명요청", fmtTime(contract.requested_at)], ["서명 완료", fmtTime(contract.signed_at)],
      ["서명본 PDF", key === "signed" ? (contract.signed_pdf_path ? "준비됨" : "미생성 — 다시 만들기 가능") : "서명 후 생성"]
    ];
    const next = key === "draft" ? "다음 단계: 서명 요청 보내기" : key === "requested" ? "직원 본인 서명을 기다리는 중입니다." : key === "signed" ? "서명이 끝났습니다. 서명본 PDF를 내려받아 보관하십시오." : "";
    return `${E.dirty ? '<p class="cv41-alert warn">저장 후 조건을 바꿨습니다. 다시 저장해야 서명요청을 보낼 수 있습니다.</p>' : ""}
      <div class="detail-grid">${rows.map(([k, v]) => `<div class="detail-cell"><small>${esc(k)}</small><b>${esc(v || "-")}</b></div>`).join("")}</div>
      <p class="notice"><b>${esc(next)}</b></p>${notifyHtml()}
      <article class="cv41-paper">${C.contractPreviewHtml(contract)}</article>`;
  }
  function footer(statusKey) {
    const busy = E.busy;
    const dis = flag => (busy || flag ? "disabled" : "");
    const parts = [];
    if (E.step === 0) parts.push(`<button class="btn primary" type="button" data-cv41-go="1" ${dis(!E.employee)}>계약조건 입력</button>`);
    if (E.step === 1) {
      parts.push(`<button class="btn" type="button" data-cv41-go="${E.contract ? 3 : 0}" ${dis(false)}>${E.contract ? "저장된 계약 보기" : "대상 다시 고르기"}</button>`);
      parts.push(`<button class="btn primary" type="button" data-cv41-preview ${dis(readOnly())}>미리보기</button>`);
    }
    if (E.step === 2) {
      parts.push(`<button class="btn" type="button" data-cv41-go="1" ${dis(false)}>조건 수정</button>`);
      parts.push(`<button class="btn primary" type="button" data-cv41-save ${dis(readOnly())}>${busy === "save" ? "저장 중…" : "이 내용으로 저장"}</button>`);
    }
    if (E.step === 3 && E.contract) {
      if (statusKey !== "signed") parts.push(`<button class="btn" type="button" data-cv41-go="1" ${dis(false)}>조건 수정</button>`);
      if (statusKey === "draft") parts.push(`<button class="btn primary" type="button" data-cv41-request ${dis(E.dirty)}>${busy === "request" ? "요청 중…" : "서명 요청 보내기"}</button>`);
      if (statusKey === "requested") {
        parts.push(`<button class="btn" type="button" data-cv41-copy ${dis(!E.contract.sign_token)}>서명 링크 복사</button>`);
        parts.push(`<button class="btn primary" type="button" data-cv41-resend ${dis(false)}>${busy === "resend" ? "보내는 중…" : "알림 다시 보내기"}</button>`);
      }
      if (statusKey === "signed") {
        parts.push(`<button class="btn" type="button" data-cv41-regen ${dis(false)}>${busy === "regen" ? "만드는 중…" : "PDF 다시 만들기"}</button>`);
        parts.push(`<button class="btn primary" type="button" data-cv41-pdf-now ${dis(false)}>${busy === "pdf" ? "내려받는 중…" : "서명본 PDF 내려받기"}</button>`);
      }
    }
    parts.push(`<button class="btn" type="button" data-cv41-close>닫기</button>`);
    return parts.join("");
  }
  function wire() {
    dialog.querySelectorAll("[data-cv41-close]").forEach(button => button.addEventListener("click", closeEditor));
    dialog.querySelector("#cv41Employee")?.addEventListener("change", event => {
      const employee = S.employees.find(item => employeeKey(item) === event.target.value) || null;
      E.employee = employee;
      E.target = employee ? { employee_request_id: employee.id, business_id: employee.business_id, branch: employee.branch } : null;
      E.values = employee ? valuesFromEmployee(employee) : {};
      E.contract = null;
      setUrl(employee ? { cv41_request: employee.id } : {});
      render();
      dialog.querySelector("#cv41Employee").value = employee ? employeeKey(employee) : "";
    });
    if (E.employee && dialog.querySelector("#cv41Employee")) dialog.querySelector("#cv41Employee").value = employeeKey(E.employee);
    dialog.querySelector("[data-cv41-contract-open]")?.addEventListener("click", event => {
      const contract = S.contracts.find(item => String(item.id) === event.target.dataset.cv41ContractOpen);
      if (contract) openEditor({ contract });
    });
    dialog.querySelectorAll("[data-cv41-field]").forEach(input => {
      const handler = () => {
        E.values[input.name] = input.value;
        E.dirty = true;
        E.previewed = false;
        if (input.name === "contractType") {
          Object.assign(E.values, C.classificationFor(input.value));
          applyDefaults(false);
          render();
        }
      };
      input.addEventListener(input.tagName === "SELECT" ? "change" : "input", handler);
    });
    dialog.querySelector("[data-cv41-defaults]")?.addEventListener("click", () => { applyDefaults(false); E.dirty = true; render(); });
    dialog.querySelectorAll("[data-cv41-go]").forEach(button => button.addEventListener("click", () => { E.step = Number(button.dataset.cv41Go); E.error = ""; render(); }));
    dialog.querySelector("[data-cv41-preview]")?.addEventListener("click", previewStep);
    dialog.querySelector("[data-cv41-save]")?.addEventListener("click", save);
    dialog.querySelector("[data-cv41-request]")?.addEventListener("click", requestSignature);
    dialog.querySelector("[data-cv41-resend]")?.addEventListener("click", resendNotice);
    dialog.querySelector("[data-cv41-copy]")?.addEventListener("click", copyLink);
    dialog.querySelector("[data-cv41-regen]")?.addEventListener("click", regeneratePdf);
    dialog.querySelector("[data-cv41-pdf-now]")?.addEventListener("click", event => downloadPdf(E.contract.id, event.target));
    const firstInvalid = dialog.querySelector('[aria-invalid="true"]');
    if (firstInvalid) firstInvalid.focus();
  }
  function applyDefaults(overwrite) {
    const defaults = C.defaultsFor(E.values.contractType || "part_time", E.values.employmentTaxType || "four_insurance");
    Object.entries(defaults).forEach(([key, value]) => {
      if (NO_DEFAULT.has(key)) return;
      const current = String(E.values[key] ?? "").trim();
      if (overwrite || !current) E.values[key] = String(value);
    });
  }
  function previewStep() {
    const result = checkLocally(currentDraft());
    if (!result.ok) {
      E.error = result.missing.length ? "저장 전에 빈 칸을 채우십시오." : result.message;
      E.missing = result.missing;
      E.step = 1;
      render();
      return;
    }
    E.error = "";
    E.missing = [];
    E.previewed = true;
    E.step = 2;
    render();
  }
  async function save() {
    if (E.busy) return;
    const requested = E.contract && statusOf(E.contract)[0] === "requested";
    if (requested && !confirm("서명요청을 보낸 계약서입니다. 저장하면 보낸 서명 링크가 무효가 되고 서명요청을 다시 보내야 합니다. 저장할까요?")) return;
    const draft = currentDraft();
    const result = checkLocally(draft);
    if (!result.ok) { E.error = result.message; E.missing = result.missing; E.step = 1; render(); return; }
    const startedAt = new Date().toISOString();
    E.busy = "save"; E.error = ""; render();
    try {
      const response = await api("/contracts", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payloadFromDraft(draft, E.contract?.id)) });
      adopt(response.contract);
      E.dirty = false; E.notify = null; E.sessionLost = false; E.step = 3;
      S.ctx?.toast?.(`계약서를 저장했습니다 (번호 ${String(response.contract?.id || "").slice(0, 8)}).`);
    } catch (error) {
      await handleError(error, "계약서 저장");
      if (!error.status) {
        // 저장 성공 여부를 모르면 목록에서 같은 계약을 찾아 대조한다(중복 저장 방지).
        const match = S.contracts.find(item => (E.contract?.id && item.id === E.contract.id && String(item.updated_at || "") >= startedAt)
          || (!E.contract && String(pick(item, "employeeRequestId")) === String(draft.employeeRequestId) && String(item.created_at || "") >= startedAt));
        if (match) { adopt(match); E.dirty = false; E.step = 3; E.error = "네트워크가 끊겼지만 서버에 저장된 계약서를 확인했습니다."; }
      }
      if (Number(error.status) === 400) { E.missing = missingFromMessage(error.message); E.step = E.missing.length ? 1 : E.step; }
    } finally {
      E.busy = ""; render(); rerenderSection();
    }
  }
  function adopt(contract) {
    if (!contract) return;
    E.contract = contract;
    E.values = valuesFromContract(contract);
    E.target = { employee_request_id: pick(contract, "employeeRequestId"), business_id: pick(contract, "businessId"), branch: pick(contract, "branch") };
    S.contracts = [contract, ...S.contracts.filter(item => item.id !== contract.id)];
    setUrl({ cv41_contract: contract.id });
  }
  async function requestSignature() {
    if (E.busy || !E.contract || E.dirty) return;
    E.busy = "request"; E.error = ""; render();
    try {
      const response = await api(`/contracts/${encodeURIComponent(E.contract.id)}/request-signature`, { method: "POST" });
      adopt(response.contract);
      E.notify = response.notify || null;
      E.sessionLost = false;
      S.ctx?.toast?.("서명요청을 저장했습니다. 알림 결과를 확인하십시오.");
    } catch (error) {
      await handleError(error, "서명 요청");
    } finally {
      E.busy = ""; render(); rerenderSection();
    }
  }
  async function resendNotice() {
    if (E.busy || !E.contract) return;
    E.busy = "resend"; E.error = ""; render();
    try {
      const response = await api(`/contracts/${encodeURIComponent(E.contract.id)}/resend-signature-notice`, { method: "POST" });
      E.notify = response.notify || null;
      E.sessionLost = false;
    } catch (error) {
      await handleError(error, "알림 재전송");
    } finally {
      E.busy = ""; render();
    }
  }
  function signLink(contract) {
    return `${location.origin}/static/apps/obys/index.html?yf_contract_token=${encodeURIComponent(contract.sign_token || "")}`;
  }
  async function copyLink() {
    if (!E.contract?.sign_token) return;
    const link = signLink(E.contract);
    const name = pick(E.contract, "employeeName") || "직원";
    const message = `${name}님 근로계약서 서명 요청입니다.\n아래 링크에서 본인 계정으로 로그인한 뒤 내용을 확인하고 서명해 주세요.\n${link}`;
    try { await navigator.clipboard.writeText(message); S.ctx?.toast?.("서명 링크를 복사했습니다. 문자나 카톡으로 보내십시오."); }
    catch (_error) { window.prompt("아래 서명 링크를 복사하십시오.", link); }
  }
  async function regeneratePdf() {
    if (E.busy || !E.contract) return;
    E.busy = "regen"; E.error = ""; render();
    try {
      await api(`/contracts/${encodeURIComponent(E.contract.id)}/signed-pdf/regenerate`, { method: "POST" });
      await load(true);
      const fresh = S.contracts.find(item => item.id === E.contract.id);
      if (fresh) adopt(fresh);
      S.ctx?.toast?.("서명본 PDF를 다시 만들었습니다.");
    } catch (error) {
      await handleError(error, "PDF 재생성");
    } finally {
      E.busy = ""; render();
    }
  }
  async function downloadPdf(contractId, button) {
    if (button) button.disabled = true;
    if (E) { E.busy = "pdf"; }
    try {
      const response = await fetch(`/api/v1/yeoljeong-finance/contracts/${encodeURIComponent(contractId)}/signed-pdf`, { credentials: "same-origin", headers: { ...(S.ctx?.headers?.() || {}) } });
      const type = response.headers.get("content-type") || "";
      if (!response.ok || !type.includes("application/pdf")) {
        let detail = "";
        try { detail = (await response.json()).detail || ""; } catch (_error) {}
        const error = new Error(detail || `서명본 PDF를 받을 수 없습니다 (HTTP ${response.status})`);
        error.status = response.status;
        throw error;
      }
      const blob = await response.blob();
      const match = /filename\*?=(?:UTF-8'')?"?([^";]+)"?/i.exec(response.headers.get("content-disposition") || "");
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = match ? decodeURIComponent(match[1]) : `contract-${contractId}.pdf`;
      document.body.append(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 30000);
      S.ctx?.toast?.(`서명본 PDF를 내려받았습니다 (${Math.round(blob.size / 1024)}KB).`);
    } catch (error) {
      const text = errorText(error, "서명본 PDF 다운로드");
      if (E) { E.error = `${text} 서명본이 아직 없으면 ‘PDF 다시 만들기’를 누르십시오.`; if (Number(error.status) === 401) E.sessionLost = true; }
      else S.ctx?.toast?.(text);
    } finally {
      if (button) button.disabled = false;
      if (E) { E.busy = ""; render(); }
    }
  }
  async function handleError(error, action) {
    const status = Number(error?.status || 0);
    E.error = errorText(error, action);
    E.missing = [];
    if (status === 401) E.sessionLost = true;
    if (status === 409 || !status) {
      try {
        await load(true);
        const fresh = E.contract && S.contracts.find(item => item.id === E.contract.id);
        if (fresh) { E.contract = fresh; if (!E.dirty) E.values = valuesFromContract(fresh); }
      } catch (_error) { /* 재조회 실패는 원래 오류 메시지로 충분하다 */ }
    }
  }
  function rerenderSection() {
    const box = document.getElementById("contractV41");
    const route = S.ctx?.route?.() || "";
    if (box && ROUTES.has(route)) renderSection(route, box);
  }

  window.obysContractV41 = {
    mount, refresh, open: openEditor,
    // 단위 검증용(브라우저 밖 node 테스트)
    _test: { draftFromValues, payloadFromDraft, checkLocally, missingFromMessage, valuesFromContract, FIELD_BY_LABEL }
  };
})();
