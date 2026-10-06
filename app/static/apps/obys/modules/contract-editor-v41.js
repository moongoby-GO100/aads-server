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
  // 추천값이 없는 필수값의 안내. 생년월일·주소·임금은 추정하지 않는다(시급제 최저시급만 버튼으로 제안).
  const NO_SUGGEST_HINT = {
    employeeBirthDate: "입사서류(신분증·주민등록등본)나 직원에게 확인해 입력하십시오.",
    employeeAddress: "입사서류(주민등록등본)나 직원에게 확인해 입력하십시오.",
    wage: "실제 임금을 직접 입력하십시오."
  };

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
  // 화면 위 '조회 사업자'(#businessSelect)와 같은 사업자의 직원·계약만 보인다. 사업자가 하나여도 이 칸은 값이 채워진다.
  function currentBusinessId() {
    const select = document.getElementById("businessSelect");
    return String((select && select.value) || "");
  }
  function inCurrentBusiness(businessId) {
    const current = currentBusinessId();
    return !current || String(businessId || "") === current;
  }
  function visibleEmployees() { return S.employees.filter(item => inCurrentBusiness(item.business_id)); }
  function visibleContracts() { return S.contracts.filter(item => inCurrentBusiness(pick(item, "businessId"))); }
  function scopeNote() {
    const current = currentBusinessId();
    return current ? `<p class="cv41-scope">현재 사업자 <b>${esc(businessName(current))}</b>의 직원·계약만 표시합니다. 다른 사업자는 화면 위 사업자 선택에서 바꾸십시오.</p>` : "";
  }
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
    const businessSelect = document.getElementById("businessSelect");
    if (businessSelect && !businessSelect.dataset.cv41Bound) {
      businessSelect.dataset.cv41Bound = "1";
      businessSelect.addEventListener("change", () => {
        const current = document.getElementById("contractV41");
        const currentRoute = S.ctx?.route?.() || "";
        if (current && ROUTES.has(currentRoute)) renderSection(currentRoute, current);
      });
    }
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
      box.innerHTML = head + scopeNote() + (route === "employees" ? employeeTable() : contractTable());
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
    box.querySelectorAll("[data-cv41-docs]").forEach(button => button.addEventListener("click", () => {
      const employee = S.employees.find(item => employeeKey(item) === button.dataset.cv41Docs);
      if (employee) openDocs(employee);
    }));
  }
  function gapBadge(count) { return count ? ` <span class="badge warn">빈 필수값 ${count}</span>` : ""; }
  /** 작성 전·작성중 계약의 빈 필수값 수. 서명요청 이후 계약은 0(이미 서버 검증 통과). */
  function rowMissingCount(employee, contract) {
    if (contract && statusOf(contract)[0] !== "draft") return 0;
    if (!contract && !employee) return 0;
    const values = contract ? valuesFromContract(contract) : valuesFromEmployee(employee);
    const target = contract
      ? { employee_request_id: pick(contract, "employeeRequestId"), business_id: pick(contract, "businessId"), branch: pick(contract, "branch") }
      : { employee_request_id: employee.id, business_id: employee.business_id, branch: employee.branch };
    try {
      const result = checkLocally(draftFromValues(values, target));
      return result.ok ? 0 : (result.missing.length || 1);
    } catch (_error) { return 1; }
  }
  function badge([, text, tone]) { return `<span class="badge ${esc(tone)}">${esc(text)}</span>`; }
  function employeeTable() {
    const employees = visibleEmployees();
    if (!employees.length) return '<div class="empty">이 사업자에 승인된 직원이 없습니다. 위 가입요청을 승인하면 여기에 표시됩니다.</div>';
    const rows = employees.map(employee => {
      const latest = contractsOf(employee)[0];
      const [key] = latest ? statusOf(latest) : ["none"];
      const action = !latest ? "계약 작성" : key === "draft" ? "이어서 작성" : key === "requested" ? "서명 현황" : "계약 보기";
      const docs = Number(employee.onboarding_document_count || 0);
      return `<tr>
        <td data-label="직원"><b>${esc(employee.name || "이름 없음")}</b><small>${esc(employee.email_masked || "")}</small></td>
        <td data-label="사업자·지점">${esc(businessName(employee.business_id))}<small>${esc(employee.branch || "지점 미정")}</small></td>
        <td data-label="입사서류">${docs ? `${docs}건` : '<span class="badge warn">미제출</span>'}</td>
        <td data-label="계약">${latest ? `${badge(statusOf(latest))}<small>${esc(fmtTime(latest.updated_at))}</small>` : '<span class="badge bad">작성 필요</span>'}${gapBadge(rowMissingCount(employee, latest))}</td>
        <td data-label="처리"><button class="btn ${latest ? "" : "primary"}" type="button" data-cv41-employee="${esc(employeeKey(employee))}">${action}</button> <button class="btn" type="button" data-cv41-docs="${esc(employeeKey(employee))}">서류 등록</button></td>
      </tr>`;
    }).join("");
    const held = employees.map(employee => rowMissingCount(employee, contractsOf(employee)[0])).filter(Boolean).length;
    const heldNote = held ? `<p class="cv41-alert warn">계약 보류 ${held}건 — 빈 필수값이 있습니다. [계약 작성]·[이어서 작성]을 누르면 빈 칸이 빨갛게 표시되고 추천값을 넣을 수 있습니다.</p>` : "";
    return `${heldNote}<div class="table-wrap"><table class="cv41-table"><thead><tr><th>직원</th><th>사업자·지점</th><th>입사서류</th><th>계약</th><th>처리</th></tr></thead><tbody>${rows}</tbody></table></div>`;
  }
  function contractTable() {
    const counts = { all: 0, draft: 0, requested: 0, signed: 0 };
    const contracts = visibleContracts();
    contracts.forEach(item => { counts.all += 1; const key = statusOf(item)[0]; if (key in counts) counts[key] += 1; });
    const filters = [["all", "전체"], ["draft", "작성중"], ["requested", "서명요청"], ["signed", "서명완료"]]
      .map(([key, label]) => `<button class="btn ${S.filter === key ? "primary" : ""}" type="button" data-cv41-filter="${key}" aria-pressed="${S.filter === key}">${label} ${counts[key]}</button>`).join("");
    const list = contracts.filter(item => S.filter === "all" || statusOf(item)[0] === S.filter);
    const rows = list.map(contract => {
      const [key] = statusOf(contract);
      const when = key === "signed" ? `서명 ${fmtTime(contract.signed_at)}` : key === "requested" ? `요청 ${fmtTime(contract.requested_at)}` : `저장 ${fmtTime(contract.updated_at)}`;
      return `<tr>
        <td data-label="직원"><b>${esc(pick(contract, "employeeName") || "-")}</b><small>${esc(contract.employee_email_masked || "")}</small></td>
        <td data-label="사업자·지점">${esc(businessName(pick(contract, "businessId")))}<small>${esc(pick(contract, "branch") || "-")}</small></td>
        <td data-label="계약 유형">${esc(C.contractTypeLabels[pick(contract, "contractType")] || pick(contract, "contractType") || "-")}</td>
        <td data-label="상태">${badge(statusOf(contract))}<small>${esc(when)}</small>${gapBadge(rowMissingCount(null, contract))}</td>
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
    const options = visibleEmployees().map(employee => {
      const latest = contractsOf(employee)[0];
      return `<option value="${esc(employeeKey(employee))}">${esc(employee.name || "이름 없음")} · ${esc(businessName(employee.business_id))} · ${esc(employee.branch || "지점 미정")}${latest ? ` (계약 ${esc(statusOf(latest)[1])})` : ""}</option>`;
    }).join("");
    const employee = E.employee;
    const latest = employee ? contractsOf(employee)[0] : null;
    const gaps = employee ? [["연락처", employee.phone], ["생년월일", employee.birth_date], ["주소", employee.address]].filter(([, v]) => !v).map(([k]) => k) : [];
    return `
      <p class="notice">승인된 입사요청만 계약 대상이 됩니다. 한 직원이 여러 점포에서 일하면 점포(사업자)마다 따로 계약합니다.</p>
      <div class="form-field"><label for="cv41Employee">계약할 승인 직원</label><select id="cv41Employee" class="field"><option value="">직원을 고르십시오</option>${options}</select></div>
      ${visibleEmployees().length ? "" : '<div class="empty">이 사업자에 승인된 직원이 없습니다. 직원 현황에서 가입요청을 먼저 승인하거나 화면 위 사업자 선택을 바꾸십시오.</div>'}
      ${employee ? `<div class="detail-grid cv41-target">
        <div class="detail-cell"><small>직원</small><b>${esc(employee.name || "-")}</b><small>${esc(employee.email_masked || "")}</small></div>
        <div class="detail-cell"><small>사업자(사용자)</small><b>${esc(businessName(employee.business_id))}</b></div>
        <div class="detail-cell"><small>근무 지점</small><b>${esc(employee.branch || "지점 미정")}</b></div>
        <div class="detail-cell"><small>입사서류</small><b>${Number(employee.onboarding_document_count || 0)}건</b><small>${gaps.length ? `비어 있는 정보: ${esc(gaps.join(", "))} — 계약조건 단계에서 입력합니다.` : "인적사항 확인됨"}</small><button class="btn" type="button" data-cv41-docs-open>서류 등록·확인</button></div>
      </div>${latest ? `<p class="cv41-alert warn">이 직원에게 이미 ${esc(statusOf(latest)[1])} 계약서가 있습니다. <button class="btn" type="button" data-cv41-contract-open="${esc(latest.id)}">기존 계약 열기</button> 새로 작성하면 별도 계약서가 됩니다.</p>` : ""}` : ""}`;
  }
  function fieldControl(field) {
    const value = E.values[field.name] ?? "";
    const disabled = readOnly() || field.readonly ? "readonly" : "";
    const invalid = E.missing.some(label => FIELD_BY_LABEL.get(label) === field.name) || Boolean(E.live?.has(field.name));
    const suggestion = invalid && !readOnly() ? suggestionFor(field.name) : null;
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
    const suggestHtml = suggestion ? `<button class="btn cv41-suggest" type="button" data-cv41-suggest="${esc(field.name)}">추천값 넣기: ${esc(shortText(suggestion.value))}</button>` : "";
    return `<div class="form-field ${invalid ? "invalid" : ""} ${field.type === "textarea" ? "wide" : ""}"><label for="${id}">${esc(field.label)}${invalid ? ' <span class="cv41-req">필수·비어 있음</span>' : ""}</label>${control}${suggestHtml}${field.hint ? `<small id="${id}_hint">${esc(field.hint)}</small>` : ""}</div>`;
  }
  function stepConditions() {
    const type = E.values.contractType || "part_time";
    const visible = field => !field.only || field.only === type;
    const live = liveCheck();
    const liveLabels = live.labels;
    E.live = missingNames(liveLabels);
    const groupHtml = GROUPS.map(([key, title]) => {
      const fields = FIELDS.filter(field => field.g === key && visible(field));
      const hasMissing = fields.some(field => E.missing.some(label => FIELD_BY_LABEL.get(label) === field.name) || E.live.has(field.name));
      const collapsible = key === "employee" || key === "employer";
      const inner = `<div class="form-grid">${fields.map(fieldControl).join("")}</div>`;
      return collapsible
        ? `<details class="cv41-group" ${hasMissing ? "open" : ""}><summary>${esc(title)}</summary>${inner}</details>`
        : `<fieldset class="cv41-group"><legend>${esc(title)}</legend>${inner}</fieldset>`;
    }).join("");
    const lockNote = E.contract && statusOf(E.contract)[0] === "requested"
      ? '<p class="cv41-alert warn">서명요청을 보낸 계약서입니다. 조건을 바꿔 저장하면 보낸 서명 링크가 무효가 되고 서명요청을 다시 보내야 합니다.</p>' : "";
    return `${lockNote}<p class="notice">표준 조항은 초안입니다. 실제 근무표·임금으로 고쳐 쓰십시오. 금액은 자동으로 넣지 않습니다. <button class="btn" type="button" data-cv41-defaults ${readOnly() ? "disabled" : ""}>빈 칸을 표준 조항으로 채우기</button></p>${readOnly() ? "" : `<div id="cv41Missing">${missingPanelHtml(liveLabels, live.blocker)}</div>`}${groupHtml}`;
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
        const prevType = E.values.contractType || "part_time";
        const prevTax = E.values.employmentTaxType || "four_insurance";
        E.values[input.name] = input.value;
        E.dirty = true;
        E.previewed = false;
        if (input.name === "contractType") {
          switchContractType(input.value, prevType, prevTax);
          render();
        }
      };
      input.addEventListener(input.tagName === "SELECT" ? "change" : "input", handler);
      if (input.name !== "contractType") input.addEventListener("change", refreshMissing);
    });
    wireMissing(dialog);
    dialog.querySelectorAll("[data-cv41-docs-open]").forEach(button => button.addEventListener("click", () => { if (E.employee) openDocs(E.employee); }));
    dialog.querySelector("[data-cv41-defaults]")?.addEventListener("click", () => { applyDefaults(false); E.dirty = true; render(); });
    dialog.querySelectorAll("[data-cv41-go]").forEach(button => button.addEventListener("click", () => { E.step = Number(button.dataset.cv41Go); E.error = ""; render(); }));
    dialog.querySelector("[data-cv41-preview]")?.addEventListener("click", previewStep);
    dialog.querySelector("[data-cv41-save]")?.addEventListener("click", save);
    dialog.querySelector("[data-cv41-request]")?.addEventListener("click", requestSignature);
    dialog.querySelector("[data-cv41-resend]")?.addEventListener("click", resendNotice);
    dialog.querySelector("[data-cv41-copy]")?.addEventListener("click", copyLink);
    dialog.querySelector("[data-cv41-regen]")?.addEventListener("click", regeneratePdf);
    dialog.querySelector("[data-cv41-pdf-now]")?.addEventListener("click", event => downloadPdf(E.contract.id, event.target));
    const firstInvalid = E.missing.length ? dialog.querySelector('[aria-invalid="true"]') : null;
    if (firstInvalid) firstInvalid.focus();
  }

  // ---------- 빈 필수값 표시·추천값 ----------
  function shortText(value) { const text = String(value ?? ""); return text.length > 30 ? `${text.slice(0, 30)}…` : text; }
  /** 비어 있는 필수값에 넣을 추천값. 누를 때만 들어간다(자동 입력 아님). */
  function suggestionFor(name) {
    if (!E) return null;
    const employee = E.employee || {};
    const business = businessRecord(E.target?.business_id);
    const from = (value, note) => (String(value ?? "").trim() ? { value: String(value), note } : null);
    switch (name) {
      case "contractDate": return from(C.today(), "오늘");
      case "startDate": return from(E.values.contractDate || C.today(), "계약 작성일");
      case "workplace": return E.target?.branch ? from(`${businessName(E.target.business_id)} ${E.target.branch}`, "근무 지점") : null;
      case "employeeName": return from(employee.name, "가입 정보");
      case "employeePhone": return from(employee.phone, "가입 정보");
      case "employeeBirthDate": return from(employee.birth_date, "입사서류");
      case "employeeAddress": return from(employee.address, "입사서류");
      case "employeeNationality": return from("대한민국", "기본값");
      case "employerName": return from(business.name, "사업자 등록정보");
      case "employerRegistrationNo": return from(business.registration_no, "사업자 등록정보");
      case "employerRepresentative": return from(business.representative, "사업자 등록정보");
      case "employerPhone": return from(business.phone, "사업자 등록정보");
      case "employerAddress": return from(business.address, "사업자 등록정보");
      case "wage": return (E.values.wageType || "hourly") === "hourly" ? from(C.MINIMUM_HOURLY_WAGE_2026, "2026년 최저시급 — 실제 시급인지 확인") : null;
      default: break;
    }
    if (NO_DEFAULT.has(name)) return null;
    const defaults = C.defaultsFor(E.values.contractType || "part_time", E.values.employmentTaxType || "four_insurance");
    return from(defaults[name], "표준 조항");
  }
  function liveCheck() {
    if (!E || readOnly()) return { labels: [], blocker: "" };
    try {
      const result = checkLocally(currentDraft());
      if (result.ok) return { labels: [], blocker: "" };
      return { labels: result.missing, blocker: result.missing.length ? "" : (result.message || "계약 조건을 확인하십시오.") };
    } catch (error) { return { labels: [], blocker: String(error?.message || error) }; }
  }
  function liveMissing() { return liveCheck().labels; }
  const FIXED_WORK_RE = /(출퇴근|근무표|주\s*\d+\s*시간|월\/화|화\/수|수\/목|목\/금|금\/토|토\/일|상시|교대)/;
  /** 3.3% 용역계약의 유형 충돌을 한꺼번에 찾는다(검사는 첫 번째 충돌만 알려 주므로 하나 고치면 다음 것이 또 막았다). */
  function freelancerConflicts() {
    const v = E?.values || {};
    if (String(v.contractType || "") !== "freelancer") return [];
    const out = [];
    if (v.employmentTaxType !== "freelancer_33" || v.wageType !== "case_fee") out.push("wage");
    if (FIXED_WORK_RE.test([v.workTime, v.weeklyHours, v.workDays].join(" "))) out.push("schedule");
    return out;
  }
  function freelancerBlockerHtml(conflicts) {
    const reasons = [];
    if (conflicts.includes("wage")) reasons.push(`임금 방식이 '${esc(C.wageTypeLabels[E.values.wageType] || E.values.wageType || "미지정")}'입니다. 3.3% 용역계약은 건별/용역비만 쓸 수 있습니다. 바꾸면 기존 금액은 1회 용역비가 아니므로 [확정 용역비]를 비웁니다.`);
    if (conflicts.includes("schedule")) reasons.push("근무일·근무시간·주 소정근로시간에 고정 요일·시간 표현이 있습니다. 정해진 요일·시간에 일하면 근로자로 판정될 수 있습니다.");
    const fixes = conflicts.length > 1
      ? [["freelancer_all", "3.3% 조건으로 모두 고치기"]]
      : conflicts.includes("wage") ? [["case_fee", "임금 방식을 건별/용역비로 바꾸기"]] : [["clear_schedule", "근무일·근무시간·주 소정근로시간 문구 지우기"]];
    fixes.push(["to_part_time", "단시간 근로계약으로 바꾸기"]);
    return `<div class="cv41-alert bad" role="alert" id="cv41Blocker"><b>미리보기·저장이 막힌 이유 ${conflicts.length}가지</b><ul>${reasons.map(text => `<li>${text}</li>`).join("")}</ul><div class="cv41-fixes">${fixes.map(([key, label]) => `<button class="btn primary" type="button" data-cv41-fix="${key}">${esc(label)}</button>`).join(" ")}</div></div>`;
  }
  /** 빈 칸이 아닌 이유로 막힐 때의 안내와 한 번에 고치는 버튼. */
  function blockerHtml(message) {
    if (!message) return "";
    const conflicts = freelancerConflicts();
    if (conflicts.length && (message.includes("건별/용역비") || message.includes("고정 근무표"))) return freelancerBlockerHtml(conflicts);
    const fixes = [];
    if (message.includes("건별/용역비")) {
      fixes.push(['case_fee', "임금 방식을 건별/용역비로 바꾸기"]);
    }
    if (message.includes("고정 근무표")) {
      fixes.push(['clear_schedule', "근무일·근무시간·주 소정근로시간 문구 지우기"]);
      fixes.push(['to_part_time', "단시간 근로계약으로 바꾸기"]);
    }
    if (message.includes("4대보험 가입 근로자")) fixes.push(['four_insurance', "신고 구분을 4대보험 근로자로 바꾸기"]);
    const hint = message.includes("건별/용역비")
      ? "3.3% 용역계약은 시급을 쓸 수 없습니다. 바꾼 뒤 [확정 용역비]에 1회 금액(예: 시급 × 근무시간)을 넣으십시오."
      : message.includes("고정 근무표")
        ? "정해진 요일·시간에 매장에서 일하면 계약 이름과 관계없이 근로자로 판정될 수 있습니다. 실제 운영이 그렇다면 근로계약이 맞습니다."
        : "";
    return `<div class="cv41-alert bad" role="alert" id="cv41Blocker"><b>미리보기·저장이 막힌 이유</b><p>${esc(message)}</p>${hint ? `<small>${esc(hint)}</small>` : ""}${fixes.length ? `<div class="cv41-fixes">${fixes.map(([key, label]) => `<button class="btn primary" type="button" data-cv41-fix="${key}">${esc(label)}</button>`).join(" ")}</div>` : ""}</div>`;
  }
  function applyFix(key) {
    if (key === "case_fee" || key === "freelancer_all") {
      // 월급·시급 금액을 1회 용역비로 넘기지 않는다 — 비워서 직접 입력하게 한다.
      if (E.values.wageType !== "case_fee") ["wage", "baseSalary", "nonTaxMealAllowance", "taxableAllowance"].forEach(name => { E.values[name] = ""; });
      Object.assign(E.values, { wageType: "case_fee", employmentTaxType: "freelancer_33" });
    }
    if (key === "clear_schedule" || key === "freelancer_all") ["workDays", "workTime", "weeklyHours"].forEach(name => { E.values[name] = ""; });
    if (key === "to_part_time") switchContractType("part_time", E.values.contractType || "freelancer", E.values.employmentTaxType || "freelancer_33");
    if (key === "four_insurance") E.values.employmentTaxType = "four_insurance";
    E.error = ""; E.missing = []; E.dirty = true; E.previewed = false;
  }
  function missingNames(labels) { return new Set(labels.map(label => FIELD_BY_LABEL.get(label)).filter(Boolean)); }
  function missingPanelHtml(labels, blocker = "") {
    if (blocker) return blockerHtml(blocker) + (labels.length ? missingPanelHtml(labels) : "");
    if (!labels.length) return '<div class="cv41-alert good" role="status">필수값이 모두 채워졌습니다. 내용을 확인하고 미리보기를 누르십시오.</div>';
    let suggestable = 0;
    const items = labels.map(label => {
      const name = FIELD_BY_LABEL.get(label);
      const suggestion = name ? suggestionFor(name) : null;
      if (suggestion) suggestable += 1;
      const action = suggestion
        ? `<button class="btn" type="button" data-cv41-suggest="${esc(name)}">추천값 넣기</button> <small>${esc(suggestion.note)}: ${esc(shortText(suggestion.value))}</small>`
        : name
          ? `<button class="btn" type="button" data-cv41-focus="${esc(name)}">직접 입력</button> <small>${esc(NO_SUGGEST_HINT[name] || "추천값이 없습니다. 직접 입력하십시오.")}</small>${(name === "employeeBirthDate" || name === "employeeAddress") && E?.employee ? ' <button class="btn" type="button" data-cv41-docs-open>입사서류 확인</button>' : ""}`
          : "";
      return `<li><b>${esc(label)}</b> ${action}</li>`;
    }).join("");
    return `<div class="cv41-alert warn" role="status"><b>비어 있는 필수값 ${labels.length}개</b>${suggestable ? ` <button class="btn primary" type="button" data-cv41-suggest-all>추천값 ${suggestable}개 모두 넣기</button>` : ""}<ul class="cv41-missing">${items}</ul><small>추천값은 누를 때만 들어갑니다. 넣은 뒤 실제 조건과 맞는지 확인하십시오.</small></div>`;
  }
  function applySuggestion(name) {
    const suggestion = suggestionFor(name);
    if (!suggestion) return false;
    E.values[name] = suggestion.value;
    E.missing = E.missing.filter(label => FIELD_BY_LABEL.get(label) !== name);
    if (!E.missing.length) E.error = "";
    E.dirty = true;
    E.previewed = false;
    return true;
  }
  function wireMissing(root) {
    if (!root) return;
    root.querySelectorAll("[data-cv41-suggest]").forEach(button => button.addEventListener("click", () => { applySuggestion(button.dataset.cv41Suggest); render(); }));
    root.querySelector("[data-cv41-suggest-all]")?.addEventListener("click", () => {
      liveMissing().forEach(label => { const name = FIELD_BY_LABEL.get(label); if (name) applySuggestion(name); });
      render();
    });
    root.querySelectorAll("[data-cv41-fix]").forEach(button => button.addEventListener("click", () => { applyFix(button.dataset.cv41Fix); render(); }));
    root.querySelectorAll("[data-cv41-focus]").forEach(button => button.addEventListener("click", () => {
      const input = dialog.querySelector(`#cv41_${button.dataset.cv41Focus}`);
      input?.closest("details")?.setAttribute("open", "");
      input?.focus();
    }));
    if (root !== dialog) root.querySelectorAll("[data-cv41-docs-open]").forEach(button => button.addEventListener("click", () => { if (E?.employee) openDocs(E.employee); }));
  }
  /** 입력칸을 벗어날 때 요약·빨간 표시만 다시 그린다(입력 중 포커스를 빼앗지 않는다). */
  function refreshMissing() {
    const box = dialog?.querySelector("#cv41Missing");
    if (!box || !E || E.step !== 1) return;
    const { labels, blocker } = liveCheck();
    E.live = missingNames(labels);
    box.innerHTML = missingPanelHtml(labels, blocker);
    wireMissing(box);
    dialog.querySelectorAll("[data-cv41-field]").forEach(input => {
      const bad = E.live.has(input.name) || E.missing.some(label => FIELD_BY_LABEL.get(label) === input.name);
      const wrap = input.closest(".form-field");
      wrap?.classList.toggle("invalid", bad);
      if (bad) input.setAttribute("aria-invalid", "true"); else input.removeAttribute("aria-invalid");
      if (!bad) { wrap?.querySelector(".cv41-req")?.remove(); wrap?.querySelector(".cv41-suggest")?.remove(); }
    });
  }
  /** 계약 유형 변경 — 이전 유형의 표준 문구를 그대로 둔 칸만 새 유형 문구로 바꾼다(직접 고친 칸은 유지).
   *  예: 단시간 근로계약 근무일 기본값 "월/화/…" 이 3.3% 용역계약에 남아 고정 근무표 검사에 걸리던 문제. */
  function switchContractType(newType, prevType, prevTax) {
    const cls = C.classificationFor(newType);
    const before = C.defaultsFor(prevType, prevTax);
    const after = C.defaultsFor(newType, cls.employmentTaxType || prevTax);
    Object.entries(before).forEach(([key, value]) => {
      if (NO_DEFAULT.has(key)) return;
      if (String(E.values[key] ?? "").trim() === String(value ?? "").trim()) E.values[key] = after[key] !== undefined ? String(after[key]) : "";
    });
    Object.assign(E.values, cls, { contractType: newType });
    applyDefaults(false);
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

  // ---------- 직원 입사서류 등록(관리자) ----------
  // 서버 API 는 기존 것을 쓴다: GET /onboarding/document-types, GET /onboarding/documents,
  // POST /onboarding/documents(관리자는 직원 이메일로 대리 등록 가능), GET .../{id}/download.
  const DOC_MAX_BYTES = 10 * 1024 * 1024;
  const DOC_ACCEPT = ".pdf,.jpg,.jpeg,.png,.webp,.heic,.tif,.tiff,.hwp,.doc,.docx";
  const EXPIRY_REQUIRED = new Set(["health_certificate", "foreign_registration", "visa_status_certificate", "work_permission_confirmation"]);
  let docsDialog = null;
  const D = { employee: null, types: [], docs: [], busy: false, error: "", loading: false };
  function ensureDocsDialog() {
    if (docsDialog) return docsDialog;
    docsDialog = document.createElement("dialog");
    docsDialog.id = "staffDocsV41";
    docsDialog.className = "cv41-dialog cv41-docs";
    docsDialog.setAttribute("aria-labelledby", "cv41DocsTitle");
    docsDialog.addEventListener("cancel", event => { event.preventDefault(); docsDialog.close(); });
    document.body.append(docsDialog);
    return docsDialog;
  }
  function docsOfEmployee(list, employee) {
    const email = String(employee.email || "").toLowerCase();
    const requestId = String(employee.id || "");
    return (list || []).filter(item => String(item.status || "") !== "superseded" && String(item.status || "") !== "deleted")
      .filter(item => (email && String(item.employee_email || "").toLowerCase() === email) || (requestId && String(item.employee_request_id || "") === requestId))
      .filter(item => !item.business_id || !employee.business_id || String(item.business_id) === String(employee.business_id))
      .sort((a, b) => String(b.uploaded_at || b.created_at || "").localeCompare(String(a.uploaded_at || a.created_at || "")));
  }
  async function openDocs(employee) {
    D.employee = employee; D.error = ""; D.busy = false; D.loading = true; D.docs = [];
    ensureDocsDialog();
    renderDocs();
    if (!docsDialog.open) docsDialog.showModal();
    try {
      const [types, docs] = await Promise.all([
        D.types.length ? Promise.resolve({ document_types: D.types }) : api("/onboarding/document-types"),
        api(`/onboarding/documents${employee.business_id ? `?business_id=${encodeURIComponent(employee.business_id)}` : ""}`)
      ]);
      D.types = types.document_types || [];
      D.docs = docsOfEmployee(docs.documents, employee);
    } catch (error) {
      D.error = errorText(error, "입사서류 조회");
    } finally {
      D.loading = false; renderDocs();
    }
  }
  function missingRequiredDocs() {
    const have = new Set(D.docs.map(item => String(item.document_type || "")));
    return D.types.filter(item => String(item.requirement || "") === "필수" && !have.has(String(item.type)));
  }
  function renderDocs() {
    if (!docsDialog || !D.employee) return;
    const employee = D.employee;
    const missing = missingRequiredDocs();
    const options = D.types.map(item => `<option value="${esc(item.type)}">${esc(item.label)} (${esc(item.requirement || "")})${missing.some(m => m.type === item.type) ? " — 미제출" : ""}</option>`).join("");
    const rows = D.docs.map(item => `<tr>
        <td data-label="서류"><b>${esc(item.document_label || item.document_type || "-")}</b><small>${esc(item.original_filename || "")}</small></td>
        <td data-label="상태">${esc(item.status_label || item.status || "-")}${item.expiry_label ? ` <span class="badge warn">${esc(item.expiry_label)}</span>` : ""}</td>
        <td data-label="발급·만료">${esc(String(item.issue_date || "").slice(0, 10) || "-")}${item.expires_at ? ` ~ ${esc(String(item.expires_at).slice(0, 10))}` : ""}</td>
        <td data-label="처리"><button class="btn" type="button" data-cv41-doc-dl="${esc(item.id)}">내려받기</button></td>
      </tr>`).join("");
    docsDialog.innerHTML = `
      <div class="modal-head cv41-head"><div><h2 id="cv41DocsTitle">직원 입사서류 등록</h2><p>${esc(employee.name || "이름 없음")} · ${esc(businessName(employee.business_id))} · ${esc(employee.branch || "지점 미정")}</p></div><button class="close" type="button" data-cv41-docs-close aria-label="닫기">×</button></div>
      ${D.error ? `<div class="cv41-alert bad" role="alert">${esc(D.error)}</div>` : ""}
      <div class="cv41-body">
        ${D.loading ? '<div class="empty">서류 목록을 불러오는 중입니다.</div>' : `
        ${missing.length ? `<div class="cv41-alert warn" role="status"><b>미제출 필수서류 ${missing.length}개</b><ul class="cv41-missing">${missing.map(item => `<li><b>${esc(item.label)}</b> <button class="btn" type="button" data-cv41-doc-pick="${esc(item.type)}">이 서류 올리기</button>${item.notice ? ` <small>${esc(item.notice)}</small>` : ""}</li>`).join("")}</ul></div>` : (D.types.length ? '<div class="cv41-alert good" role="status">필수 입사서류가 모두 등록돼 있습니다.</div>' : "")}
        <fieldset class="cv41-group"><legend>서류 올리기 (관리자가 직원 대신 등록)</legend>
          <div class="form-grid">
            <div class="form-field"><label for="cv41DocType">서류 종류</label><select id="cv41DocType" class="field">${options}</select></div>
            <div class="form-field"><label for="cv41DocIssue">발급일</label><input id="cv41DocIssue" class="field" type="date"></div>
            <div class="form-field"><label for="cv41DocExpire">만료일 <span id="cv41DocExpireReq" class="cv41-req" hidden>필수</span></label><input id="cv41DocExpire" class="field" type="date"><small>보건증·외국인등록증·체류자격·취업허가는 만료일이 필수입니다.</small></div>
            <div class="form-field wide"><label for="cv41DocFile">파일 (PDF·이미지·한글·워드, 10MB 이하)</label><input id="cv41DocFile" class="field" type="file" accept="${DOC_ACCEPT}"></div>
            <div class="form-field wide"><label for="cv41DocMemo">메모</label><input id="cv41DocMemo" class="field" type="text" maxlength="500" placeholder="예: 원본 확인함, 주민번호 뒷자리 마스킹본"></div>
          </div>
        </fieldset>
        <h3 class="cv41-subhead">등록된 서류 ${D.docs.length}건</h3>
        ${D.docs.length ? `<div class="table-wrap contract-v41"><table class="cv41-table"><thead><tr><th>서류</th><th>상태</th><th>발급·만료</th><th>처리</th></tr></thead><tbody>${rows}</tbody></table></div>` : '<div class="empty">등록된 입사서류가 없습니다.</div>'}`}
      </div>
      <div class="cv41-foot"><button class="btn primary" type="button" data-cv41-doc-upload ${D.busy || D.loading ? "disabled" : ""}>${D.busy ? "올리는 중…" : "서류 올리기"}</button><button class="btn" type="button" data-cv41-docs-close>닫기</button></div>`;
    docsDialog.querySelectorAll("[data-cv41-docs-close]").forEach(button => button.addEventListener("click", () => docsDialog.close()));
    const typeSelect = docsDialog.querySelector("#cv41DocType");
    const syncExpiry = () => { const req = docsDialog.querySelector("#cv41DocExpireReq"); if (req && typeSelect) req.hidden = !EXPIRY_REQUIRED.has(typeSelect.value); };
    if (typeSelect && missing.length) typeSelect.value = missing[0].type;
    typeSelect?.addEventListener("change", syncExpiry);
    syncExpiry();
    docsDialog.querySelectorAll("[data-cv41-doc-pick]").forEach(button => button.addEventListener("click", () => { typeSelect.value = button.dataset.cv41DocPick; syncExpiry(); docsDialog.querySelector("#cv41DocFile")?.focus(); }));
    docsDialog.querySelector("[data-cv41-doc-upload]")?.addEventListener("click", uploadDoc);
    docsDialog.querySelectorAll("[data-cv41-doc-dl]").forEach(button => button.addEventListener("click", () => downloadDoc(button.dataset.cv41DocDl, button)));
  }
  async function uploadDoc() {
    if (D.busy || !D.employee) return;
    const type = docsDialog.querySelector("#cv41DocType")?.value || "";
    const file = docsDialog.querySelector("#cv41DocFile")?.files?.[0];
    const issue = docsDialog.querySelector("#cv41DocIssue")?.value || "";
    const expires = docsDialog.querySelector("#cv41DocExpire")?.value || "";
    const memo = docsDialog.querySelector("#cv41DocMemo")?.value || "";
    const problems = [];
    if (!type) problems.push("서류 종류");
    if (!file) problems.push("파일");
    if (EXPIRY_REQUIRED.has(type) && !expires) problems.push("만료일");
    if (!String(D.employee.email || "").trim()) problems.push("직원 이메일(가입 정보에 없음)");
    if (problems.length) { D.error = `필수값을 채우십시오: ${problems.join(", ")}`; renderDocs(); return; }
    if (file.size > DOC_MAX_BYTES) { D.error = "파일은 10MB 이하여야 합니다."; renderDocs(); return; }
    const form = new FormData();
    form.append("employee_name", D.employee.name || "");
    form.append("employee_email", D.employee.email || "");
    form.append("branch", D.employee.branch || "");
    form.append("document_type", type);
    form.append("issue_date", issue);
    form.append("expires_at", expires);
    form.append("memo", memo);
    form.append("file", file);
    D.busy = true; D.error = ""; renderDocs();
    try {
      await api("/onboarding/documents", { method: "POST", body: form });
      S.ctx?.toast?.("입사서류를 등록했습니다.");
      const employee = D.employee;
      D.busy = false;
      await openDocs(employee);
      refresh();
    } catch (error) {
      D.busy = false;
      D.error = errorText(error, "입사서류 등록");
      renderDocs();
    }
  }
  async function downloadDoc(documentId, button) {
    if (button) button.disabled = true;
    try {
      const response = await fetch(`/api/v1/yeoljeong-finance/onboarding/documents/${encodeURIComponent(documentId)}/download`, { credentials: "same-origin", headers: { ...(S.ctx?.headers?.() || {}) } });
      if (!response.ok) { const error = new Error(`서류를 받을 수 없습니다 (HTTP ${response.status})`); error.status = response.status; throw error; }
      const blob = await response.blob();
      const match = /filename\*?=(?:UTF-8'')?"?([^";]+)"?/i.exec(response.headers.get("content-disposition") || "");
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = match ? decodeURIComponent(match[1]) : `document-${documentId}`;
      document.body.append(link); link.click(); link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 30000);
    } catch (error) {
      D.error = errorText(error, "서류 내려받기"); renderDocs();
    } finally {
      if (button) button.disabled = false;
    }
  }

  window.obysContractV41 = {
    mount, refresh, open: openEditor, openDocs,
    // 단위 검증용(브라우저 밖 node 테스트)
    _test: { draftFromValues, payloadFromDraft, checkLocally, missingFromMessage, valuesFromContract, FIELD_BY_LABEL, docsOfEmployee }
  };
})();
