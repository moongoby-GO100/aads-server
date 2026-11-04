/* Administrator payroll workflow. Existing server APIs own calculations and versions. */
(() => {
  "use strict";
  const drafts = new Map();
  const amounts = [
    ["gross_pay", "총지급액"], ["taxable_pay", "과세 지급액"],
    ["non_tax_meal_allowance", "비과세 식대"], ["tax_withholding", "원천세"],
    ["insurance_deduction", "보험 공제"], ["other_deduction", "기타 공제"]
  ];
  const payCodes = [["base","기본급"],["overtime","연장근로수당"],["night","야간근로수당"],["holiday","휴일근로수당"],["weekly_holiday","주휴수당"],["meal","식대"],["bonus","상여금"],["other","기타수당"]];
  const dedCodes = [["national_pension","국민연금"],["health_insurance","건강보험"],["long_term_care","노인장기요양보험"],["employment_insurance","고용보험"],["income_tax","소득세"],["local_income_tax","지방소득세"],["other","기타 공제"]];
  const insuranceCodes = ["national_pension","health_insurance","long_term_care","employment_insurance"], taxCodes = ["income_tax","local_income_tax"];
  const workFields = [["work_days","근로일수(일)"],["work_minutes","총 근로시간(분)"],["overtime_minutes","연장근로(분)"],["night_minutes","야간근로(분)"],["holiday_minutes","휴일근로(분)"]];
  const clone = value => JSON.parse(JSON.stringify(value ?? null));
  // 프리랜서 3.3%: 소득세 3%(10원 미만 절사) + 지방소득세 = 소득세의 10%(10원 미만 절사). 서버 freelancer_withholding 과 같은 식.
  const withholding = gross => {const income = Math.floor(Math.floor(gross * 3 / 100) / 10) * 10; return {income, local: Math.floor(Math.floor(income / 10) / 10) * 10};};
  const sumItems = (rows, codes) => rows.filter(row => !codes || codes.includes(row.code)).reduce((total, row) => total + (Number(row.amount) || 0), 0);
  const money = value => value === null || value === undefined || value === "" ? "미집계" : Number(value).toLocaleString("ko-KR") + "원";
  // 마스킹된 값("[MASKED]"·"*")은 이메일로 취급하지 않는다. 목록 API 는 raw.employee_email 을 마스킹해서 내려준다.
  const email = row => {
    const text = String(row?.email || row?.employee_email || "").trim().toLowerCase();
    return text && !text.includes("[masked]") && !text.includes("*") ? text : "";
  };
  const idOf = row => String(row?.id ?? "");
  const sha256Hex = async text => {
    const subtle = globalThis.crypto?.subtle;
    if (!subtle) throw new Error("이 브라우저에서는 직원 연결에 필요한 암호화 기능을 쓸 수 없습니다.");
    const digest = await subtle.digest("SHA-256", new TextEncoder().encode(text));
    return [...new Uint8Array(digest)].map(byte => byte.toString(16).padStart(2, "0")).join("");
  };
  // 서버 obys_workspaces._employee_key 와 같은 식: sha256(사업자명 + ":" + 소문자·trim 이메일).
  const employeeKeyFor = (businessName, address, hash = sha256Hex) => hash(`${businessName}:${address}`);
  // 목록 행의 employee_key 와 같은 키를 갖는 승인 직원을 찾는다. 이름으로는 절대 연결하지 않는다.
  async function resolvePayrollEmployee({employeeKey, employees, businessName, sha256 = sha256Hex}) {
    const wanted = String(employeeKey || "").trim().toLowerCase();
    if (!wanted) return {status: "unmatched", employee: null, candidates: []};
    const candidates = [];
    for (const row of employees || []) {
      const address = email(row);
      if (address && await employeeKeyFor(businessName, address, sha256) === wanted) candidates.push(row);
    }
    if (candidates.length > 1) return {status: "ambiguous", employee: null, candidates};
    if (candidates.length === 1 && idOf(candidates[0])) return {status: "matched", employee: candidates[0], candidates};
    return {status: "unmatched", employee: null, candidates: []};
  }
  // "own": 이 직원의 명세 / "ambiguous": 같은 이메일 승인 직원이 여럿이라 소속을 알 수 없음 / "other": 다른 직원 또는 판단 불가.
  function statementOwnership(statement, employee, allEmployees) {
    if (!employee) return "other";
    const stated = String(statement?.employee_request_id || statement?.statement_payload?.employee_request_id || "");
    if (stated) return stated === idOf(employee) ? "own" : "other";
    const address = email(statement);
    if (!address || address !== email(employee)) return "other";
    const sameEmail = (allEmployees || []).filter(row => email(row) === address).length;
    return sameEmail === 1 ? "own" : sameEmail > 1 ? "ambiguous" : "other";
  }
  const ownsStatement = (statement, employee, allEmployees) => statementOwnership(statement, employee, allEmployees) === "own";
  const BLOCKED_TEXT = "이 명세의 직원을 승인 직원 목록에서 찾지 못했습니다. 아래에서 직원을 직접 선택하세요.";
  const AMBIGUOUS_LINK_TEXT = "같은 이메일의 승인 직원이 여럿입니다. " + BLOCKED_TEXT;
  const OUT_OF_BRANCH_TEXT = "선택한 직원이 이 지점 목록에 없습니다. 지점 필터를 바꾸거나 아래에서 직원을 다시 선택하세요.";
  const AMBIGUOUS_STATEMENT_TEXT = "같은 이메일 직원이 여럿이라 명세를 자동 연결하지 않았습니다.";
  const statusName = value => ({draft:"초안",needs_review:"검토 필요",confirmed:"확정",paid:"지급 완료"}[value] || "상태 확인 필요");
  const esc = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));

  async function mount(host, options) {
    const {api, businessId, month, onSaved, downloadPdf} = options;
    if (options.canManage !== true) { host.innerHTML = '<p role="alert">급여 관리 권한이 없습니다.</p>'; return; }
    const current = () => host.isConnected;
    host.innerHTML = '<p role="status">직원과 급여명세를 불러오는 중입니다.</p>';
    let employees, statements;
    try {
      const [people, payroll] = await Promise.all([
        api(`/employees/approved?business_id=${encodeURIComponent(businessId)}`),
        api(`/payroll?payroll_month=${encodeURIComponent(month)}`)
      ]);
      if (!current()) return;
      employees = (people?.employees || []).filter(row => email(row) && idOf(row));
      statements = (payroll?.statements || []).filter(row =>
        String(row.business_id || "") === String(businessId) && !row.superseded_by);
    } catch (error) {
      if (!current()) return;
      host.innerHTML = `<p role="alert">급여 편집 자료를 불러오지 못했습니다: ${esc(error.message)}</p><button type="button" class="btn" data-payroll-retry>다시 조회</button>`;
      host.querySelector("button").onclick = () => mount(host, options);
      return;
    }
    if (!employees.length) {
      host.innerHTML = '<p>승인된 직원이 없습니다. 직원 현황에서 승인한 뒤 다시 조회하세요.</p>';
      return;
    }
    const allEmployees = employees.slice();
    // 선택 상태는 승인 직원 id. 이메일로 다시 찾아 id 를 바꾸지 않는다.
    const wantedId = idOf({id: options.selectedEmployeeId});
    const mustChoose = options.requireSelection === true || (wantedId !== "" && !allEmployees.some(row => idOf(row) === wantedId));
    const initialNotice = options.linkStatus === "ambiguous" ? AMBIGUOUS_LINK_TEXT : BLOCKED_TEXT;
    let selected = mustChoose ? "" : wantedId || idOf(employees[0]);
    let active = null, busy = false, generation = 0;
    let pay = [], ded = [], work = {}, planTaxType = "";
    const sessionOwner = new Map();
    const key = () => `${businessId}|${month}|${selected}`;
    const employee = () => employees.find(row => idOf(row) === selected);
    const statementOwner = row => sessionOwner.has(row.id) ? (sessionOwner.get(row.id) === selected ? "own" : "other") : statementOwnership(row, employee(), allEmployees);
    const ownStatements = () => statements.filter(row => row.payroll_month === month && statementOwner(row) === "own");
    const blockNotice = () => employee() ? "" : selected ? OUT_OF_BRANCH_TEXT : initialNotice;
    const pickerOptions = () => (employee() ? "" : '<option value="">직원을 선택하세요</option>')
      + employees.map(row => `<option value="${esc(idOf(row))}">${esc(row.name || row.employee_name || "이름 미등록")} · ${esc(row.branch || "지점 미등록")} · ${esc(email(row).replace(/^(.).*@/, "$1***@"))}</option>`).join("");
    host.innerHTML = `<h2>급여 초안 · 명세 관리</h2><p>직원과 월을 선택하고 계약·승인 근태 기준으로 작성합니다. 확정된 명세를 수정하면 기존 명세를 보존한 새 버전으로 저장합니다.</p>
      <div class="toolbar"><label>점포 선택<select class="field" data-payroll-branch><option value="">전체 점포</option>${[...new Set(allEmployees.map(row => row.branch || ""))].filter(Boolean).map(branch => `<option value="${esc(branch)}">${esc(branch)}</option>`).join("")}</select></label><label>직원 선택<select class="field" data-payroll-employee>${pickerOptions()}</select></label>
      <label>명세 선택<select class="field" data-payroll-statement></select></label></div>
      <p data-payroll-blocked role="alert"></p><p data-payroll-link-notice role="status"></p><p data-payroll-state role="status"></p><p data-payroll-basis></p>
      <form data-payroll-form class="toolbar">${amounts.map(([name, label]) => `<label data-wrap="${name}">${label}<input class="field" name="${name}" type="number" min="0" step="1" required></label>`).join("")}
      <label>임금지급일<input class="field" data-payroll-pay-date maxlength="40" placeholder="예: 2026-11-10 또는 매월 10일"></label>
      <button class="btn primary" type="submit">초안 저장</button></form>
      <div data-payroll-items></div>
      <p data-payroll-totals aria-live="polite"></p><div class="toolbar">
      <button type="button" class="btn" data-payroll-action="defaults">계약 기준값 불러오기</button>
      <button type="button" class="btn" data-payroll-action="draft">승인 근태로 초안 만들기</button>
      <button type="button" class="btn" data-payroll-action="settlement">프리랜서 용역 정산 초안</button>
      <button type="button" class="btn" data-payroll-action="confirm">명세 확정</button>
      <button type="button" class="btn" data-payroll-action="pdf">PDF 보기</button>
      <button type="button" class="btn" data-payroll-action="versions">변경 이력</button>
      <button type="button" class="btn" data-payroll-action="pay">지급 완료로 기록</button>
      <button type="button" class="btn" data-payroll-action="deliver">명세서 발송</button></div>
      <p role="status" data-payroll-message></p><div data-payroll-versions></div>`;
    host.querySelectorAll("button,select,input").forEach(control => {control.style.minHeight = "44px";});
    const form = host.querySelector("form"), picker = host.querySelector("[data-payroll-employee]");
    const statementPicker = host.querySelector("[data-payroll-statement]");
    const message = host.querySelector("[data-payroll-message]");
    const itemsHost = host.querySelector("[data-payroll-items]");
    const payDateInput = host.querySelector("[data-payroll-pay-date]");
    const isFreelancer = () => (active?.employment_tax_type || planTaxType || employee()?.current_employment?.employment_tax_type) === "freelancer_33";
    const inputValues = () => Object.fromEntries(amounts.map(([name]) => [name, form.elements[name].value]));
    const snapshot = (p, d, w) => JSON.stringify({p:p.map(row => [row.code, row.label, Number(row.amount), row.calc_note || ""]), d:d.map(row => [row.code, row.label, Number(row.amount)]),
      w:workFields.map(([name]) => w[name] === undefined || w[name] === "" ? null : Number(w[name]))});
    const remember = () => employee() && drafts.set(key(), {id:active?.id || "", ...inputValues(), pay_date:payDateInput.value, items:clone({pay, ded, work})});
    const dirty = () => amounts.some(([name]) => form.elements[name].value !== String(active?.[name] ?? ""))
      || payDateInput.value !== String(active?.pay_date ?? "")
      || snapshot(pay, ded, work) !== snapshot(active?.pay_items || [], active?.deduction_items || [], active?.work_summary || {});
    const itemPayload = () => ({
      pay_items:pay.map(row => ({code:row.code, label:row.label, amount:Number(row.amount), calc_note:row.calc_note || ""})),
      deduction_items:ded.map(row => ({code:row.code, label:row.label, amount:Number(row.amount)})),
      work_summary:Object.fromEntries(workFields.filter(([name]) => work[name] !== undefined && work[name] !== "").map(([name]) => [name, Number(work[name])]))
    });
    const itemsComplete = () => [...pay, ...ded].every(row => row.amount !== "" && row.amount !== undefined && Number.isSafeInteger(Number(row.amount)) && Number(row.amount) >= 0);
    // 항목을 쓰면 해당 합계 칸은 항목 합계로 고정(읽기 전용)한다. 항목이 없으면 기존 간편 입력 그대로.
    const syncDerived = () => {
      const lock = (name, on, value) => {const control = form.elements[name]; control.readOnly = on; if (on) control.value = String(value);};
      lock("gross_pay", pay.length > 0, sumItems(pay));
      lock("taxable_pay", pay.length > 0, Math.max(sumItems(pay) - (Number(form.elements.non_tax_meal_allowance.value) || 0), 0));
      lock("tax_withholding", ded.length > 0, sumItems(ded, taxCodes));
      lock("insurance_deduction", ded.length > 0, sumItems(ded, insuranceCodes));
      lock("other_deduction", ded.length > 0, sumItems(ded, ["other"]));
      const insurance = host.querySelector('[data-wrap="insurance_deduction"]');
      if (insurance) insurance.style.display = isFreelancer() && !Number(form.elements.insurance_deduction.value) ? "none" : "";
      if (isFreelancer() && form.elements.insurance_deduction.value === "") form.elements.insurance_deduction.value = "0";
    };
    const itemTable = (kind, rows, codes, title) => `<h3>${title}</h3>${rows.length ? `<table><thead><tr><th>구분</th><th>항목명</th><th>금액(원)</th>${kind === "pay" ? "<th>계산방법</th>" : ""}<th></th></tr></thead><tbody>${rows.map((row, index) => `<tr>
      <td><select class="field" style="min-height:44px" data-item="${kind}" data-i="${index}" data-k="code">${codes.map(([code, label]) => `<option value="${code}"${code === row.code ? " selected" : ""}>${label}</option>`).join("")}</select></td>
      <td><input class="field" style="min-height:44px" data-item="${kind}" data-i="${index}" data-k="label" maxlength="40" value="${esc(row.label)}"></td>
      <td><input class="field" style="min-height:44px" type="number" min="0" step="1" data-item="${kind}" data-i="${index}" data-k="amount" value="${esc(row.amount)}"></td>
      ${kind === "pay" ? `<td><input class="field" style="min-height:44px" data-item="${kind}" data-i="${index}" data-k="calc_note" maxlength="200" placeholder="예: 시급 × 시간" value="${esc(row.calc_note || "")}"></td>` : ""}
      <td><button type="button" class="btn" style="min-height:44px" data-item-del="${kind}" data-i="${index}">삭제</button></td></tr>`).join("")}</tbody></table>` : "<p>항목 없음 — 위 합계 칸에 간편 입력할 수 있습니다.</p>"}
      <button type="button" class="btn" style="min-height:44px" data-item-add="${kind}">${kind === "pay" ? "지급 항목 추가" : "공제 항목 추가"}</button>`;
    const renderItems = () => {
      const freelancer = isFreelancer();
      itemsHost.innerHTML = itemTable("pay", pay, payCodes, "지급 항목") + itemTable("ded", ded, freelancer ? dedCodes.filter(([code]) => !insuranceCodes.includes(code)) : dedCodes, freelancer ? "원천징수 항목" : "공제 항목")
        + (freelancer ? '<button type="button" class="btn" style="min-height:44px" data-item-tax="1">3.3% 자동 계산(소득세 3% · 지방소득세 0.3%)</button>' : "")
        + `<h3>근로일수 및 근로시간</h3><div class="toolbar">${workFields.map(([name, label]) => `<label>${label}<input class="field" style="min-height:44px" type="number" min="0" step="1" data-work-field="${name}" value="${esc(work[name] ?? "")}"></label>`).join("")}</div>`;
    };
    const refresh = () => {syncDerived(); remember(); totals(); restoreButtons();};
    itemsHost.oninput = event => {
      const control = event.target, data = control.dataset || {};
      if (data.workField) work[data.workField] = control.value;
      else if (data.item) {
        const row = (data.item === "pay" ? pay : ded)[Number(data.i)];
        if (!row) return;
        row[data.k] = control.value;
        if (data.k === "code") {row.label = (((data.item === "pay" ? payCodes : dedCodes).find(([code]) => code === row.code)) || [])[1] || row.label; renderItems();}
      }
      refresh();
    };
    // 소득세+지방소득세 합계(지방소득세 = 소득세의 10%, 10원 미만 절사)를 둘로 나눈다. 정확히 나뉘지 않으면 null.
    const splitTax = total => {
      let k = Math.max(Math.floor(total / 11) - 2, 0);
      while (k * 10 + Math.floor(k / 10) * 10 < total) k++;
      return k * 10 + Math.floor(k / 10) * 10 === total ? {income:k * 10, local:Math.floor(k / 10) * 10} : null;
    };
    // 공제 항목이 처음 생기면 syncDerived 가 세 공제 칸을 항목 합계로 덮어쓰므로, 간편 입력에 있던 금액을 먼저 항목으로 옮긴다.
    // 4대보험은 항목별로 나눌 근거가 없어 옮기지 못하므로 사용자가 직접 나누도록 막는다(조용히 0원이 되는 것 방지).
    // 원천세는 소득세·지방소득세로 정확히 나뉠 때만 옮기고, 아니면 한쪽에 몰아 넣지 않고 막는다.
    const seedDeductions = (replaceTax = false) => {
      if (ded.length) return true;
      const read = name => Number(form.elements[name].value) || 0;
      if (read("insurance_deduction") > 0) {
        message.textContent = "보험 공제 금액이 입력되어 있습니다. 항목으로 전환하면 사라지므로, 금액을 기억한 뒤 보험 공제 칸을 0으로 바꾸고 국민연금 등 항목별로 입력하세요.";
        return false;
      }
      const taxTotal = read("tax_withholding"), tax = taxTotal > 0 && !replaceTax ? splitTax(taxTotal) : null;
      if (taxTotal > 0 && !replaceTax && !tax) {
        message.textContent = "원천세 금액을 소득세와 지방소득세로 나눌 수 없습니다. 금액을 기억한 뒤 원천세 칸을 0으로 바꾸고 소득세·지방소득세 항목을 직접 입력하세요.";
        return false;
      }
      if (tax) {
        ded.push({code:"income_tax", label:isFreelancer() ? "사업소득세(3%)" : "소득세", amount:tax.income});
        if (tax.local > 0) ded.push({code:"local_income_tax", label:"지방소득세", amount:tax.local});
      }
      if (read("other_deduction") > 0) ded.push({code:"other", label:"기타 공제", amount:read("other_deduction")});
      return true;
    };
    // 지급 항목이 처음 생기면 syncDerived 가 총지급액·과세 지급액을 항목 합계로 덮어쓰므로, 간편 입력의 총지급액을 기본급(+비과세 식대) 항목으로 먼저 옮긴다.
    const seedPay = () => {
      if (pay.length) return true;
      const read = name => Number(form.elements[name].value) || 0;
      const gross = read("gross_pay"), meal = read("non_tax_meal_allowance");
      if (gross <= 0) return true;
      if (meal > gross) {
        message.textContent = "비과세 식대가 총지급액보다 큽니다. 금액을 바로잡은 뒤 항목을 추가하세요.";
        return false;
      }
      if (gross - meal > 0) pay.push({code:"base", label:"기본급", amount:gross - meal, calc_note:""});
      if (meal > 0) pay.push({code:"meal", label:"식대", amount:meal, calc_note:""});
      return true;
    };
    const nextPayCode = () => payCodes.find(([code]) => !pay.some(row => row.code === code)) || payCodes[0];
    itemsHost.onclick = event => {
      const data = (event.target.closest ? event.target.closest("button") : event.target)?.dataset || {};
      if (data.itemAdd) {
        if (data.itemAdd === "pay" && !seedPay()) return;
        if (data.itemAdd === "ded" && !seedDeductions()) return;
        if (data.itemAdd === "pay") {const [code, label] = nextPayCode(); pay.push({code, label, amount:"", calc_note:""});}
        else ded.push({code:isFreelancer() ? "income_tax" : "national_pension", label:isFreelancer() ? "소득세" : "국민연금", amount:""});
      } else if (data.itemDel) {
        (data.itemDel === "pay" ? pay : ded).splice(Number(data.i), 1);
      } else if (data.itemTax) {
        const gross = Number(form.elements.gross_pay.value);
        if (!Number.isSafeInteger(gross) || gross < 0 || form.elements.gross_pay.value === "") {message.textContent = "총지급액(용역비)을 먼저 입력하세요."; return;}
        if (!seedDeductions(true)) return;
        const tax = withholding(gross);
        ded = [...ded.filter(row => !taxCodes.includes(row.code)), {code:"income_tax", label:"사업소득세(3%)", amount:tax.income}, {code:"local_income_tax", label:"지방소득세(0.3%)", amount:tax.local}];
      } else return;
      renderItems(); refresh();
    };
    const totals = () => {
      const values = inputValues();
      const known = amounts.every(([name]) => values[name] !== "" && Number.isSafeInteger(Number(values[name])) && Number(values[name]) >= 0);
      host.querySelector("[data-payroll-totals]").textContent = known
        ? `공제 합계 ${money(Number(values.tax_withholding) + Number(values.insurance_deduction) + Number(values.other_deduction))} · 예상 실수령 ${money(Number(values.gross_pay) - Number(values.tax_withholding) - Number(values.insurance_deduction) - Number(values.other_deduction))} · 저장 후 서버 계산값을 확인하세요.`
        : "미입력 금액을 확인하세요. 확인되지 않은 금액은 0원으로 채우지 않습니다.";
    };
    const buttons = () => host.querySelectorAll("button,select,input");
    const restoreButtons = () => {
      const blocked = !employee(), branchPicker = host.querySelector("[data-payroll-branch]");
      buttons().forEach(button => {button.disabled = busy || (blocked && button !== picker && button !== branchPicker);});
      for (const action of ["confirm", "pdf", "versions", "pay", "deliver"]) {
        host.querySelector(`[data-payroll-action="${action}"]`).disabled = busy || !active?.id || dirty()
          || (action === "confirm" && !["draft", "needs_review"].includes(active.status))
          || (action === "pay" && active.status !== "confirmed")
          || (action === "deliver" && !["confirmed", "paid"].includes(active.status));
      }
    };
    const fill = row => {
      active = row || null;
      const draft = drafts.get(key());
      const values = draft && draft.id === (active?.id || "") ? draft : active || {};
      amounts.forEach(([name]) => {form.elements[name].value = values[name] ?? "";});
      payDateInput.value = values.pay_date ?? "";
      const source = values.items || {pay:clone(active?.pay_items) || [], ded:clone(active?.deduction_items) || [], work:clone(active?.work_summary) || {}};
      pay = source.pay || []; ded = source.ded || []; work = source.work || {};
      renderItems(); syncDerived();
      host.querySelector("[data-payroll-state]").textContent = active
        ? `${month} · ${statusName(active.status)} · 버전 ${active.version || 1}${["confirmed", "paid"].includes(active.status) ? " · 수정 저장 시 새 초안 생성" : ""}`
        : `${month} · 새 초안`;
      host.querySelector("[data-payroll-basis]").textContent = active?.settlement_basis
        ? `용역기간 ${active.settlement_basis.period_start} ~ ${active.settlement_basis.period_end} · 확인한 지급액 ${money(active.settlement_basis.confirmed_amount)} · ${active.settlement_basis.scope || "업무범위는 계약 확인"}`
        : active?.attendance_basis
        ? `승인 근태 ${active.attendance_basis.approved_days}일 · ${active.attendance_basis.approved_minutes}분 기준`
        : active?.source_contract_id ? "서명 계약서 기준값 적용" : "계약·근태 기준값을 조회하거나 금액을 확인해 입력하세요.";
      totals();restoreButtons();
    };
    const showNotices = () => {
      const blocked = blockNotice();
      host.querySelector("[data-payroll-blocked]").textContent = blocked;
      host.querySelector("[data-payroll-link-notice]").textContent = employee() && statements.some(row => row.payroll_month === month && statementOwner(row) === "ambiguous") ? AMBIGUOUS_STATEMENT_TEXT : "";
    };
    const selectEmployee = () => {
      generation++;
      planTaxType = "";
      picker.innerHTML = pickerOptions();
      picker.value = employee() ? selected : "";
      showNotices();
      if (!employee()) {
        active = null; pay = []; ded = []; work = {};
        statementPicker.innerHTML = '<option value="">새 초안 작성</option>';
        message.textContent = "";
        host.querySelector("[data-payroll-versions]").textContent = "";
        amounts.forEach(([name]) => {form.elements[name].value = "";});
        payDateInput.value = "";
        renderItems();
        host.querySelector("[data-payroll-state]").textContent = "직원을 선택하기 전에는 저장·확정·지급·발송할 수 없습니다.";
        host.querySelector("[data-payroll-basis]").textContent = "";
        totals(); restoreButtons();
        return;
      }
      const rows = ownStatements();
      statementPicker.innerHTML = '<option value="">새 초안 작성</option>' + rows.map(row => `<option value="${esc(row.id)}">${esc(month)} · ${statusName(row.status)} · 버전 ${row.version || 1}</option>`).join("");
      statementPicker.value = rows[0]?.id || "";
      message.textContent = "";
      host.querySelector("[data-payroll-versions]").textContent = "";
      fill(rows[0]);
    };
    host.querySelector("[data-payroll-branch]").onchange = event => {
      remember();
      employees = allEmployees.filter(row => !event.target.value || row.branch === event.target.value);
      selectEmployee();
    };
    picker.onchange = () => {remember();selected = picker.value;options.onSelect?.(selected);selectEmployee();};
    statementPicker.onchange = () => {remember();generation++;fill(ownStatements().find(row => row.id === statementPicker.value));};
    form.oninput = () => {syncDerived();remember();totals();restoreButtons();};
    const execute = async operation => {
      if (busy || !employee()) return;
      busy = true;restoreButtons();message.textContent = "처리 중입니다.";
      const requestGeneration = generation;
      try {
        await operation();
      } catch (error) {
        if (current() && requestGeneration === generation) message.textContent = error.message;
      } finally {
        busy = false;if(current()){restoreButtons();if(message.textContent === "처리 중입니다.")message.textContent = "";}
      }
    };
    const saved = async statement => {
      if (!current()) return;
      if (!statement?.id) throw new Error("저장 응답을 확인할 수 없습니다. 입력값을 보존했습니다. 다시 조회하세요.");
      // Re-read authoritative server totals before discarding any local edits.
      const result = await api(`/payroll?${new URLSearchParams({payroll_month:month})}`);
      const persisted = (result?.statements || []).find(row => row.id === statement.id
        && String(row.business_id || "") === String(businessId) && email(row) === email(employee())
        && row.payroll_month === month);
      if (!persisted) throw new Error("저장 결과를 재조회하지 못했습니다. 입력값을 보존했습니다.");
      statement = persisted;
      sessionOwner.set(statement.id, selected);
      drafts.delete(key());
      statements = [statement, ...statements.filter(row => row.id !== statement.id && row.id !== statement.supersedes_id)];
      statementPicker.innerHTML = '<option value="">새 초안 작성</option>' + ownStatements().map(row => `<option value="${esc(row.id)}">${esc(month)} · ${statusName(row.status)} · 버전 ${row.version || 1}</option>`).join("");
      statementPicker.value = statement.id;
      fill(statement);
      message.textContent = `저장 완료 · 총지급 ${money(statement.gross_pay)} · 공제 ${money(statement.totals?.total_deductions)} · 실수령 ${money(statement.net_pay)}`;
      await onSaved?.();
    };
    form.onsubmit = event => {
      event.preventDefault();if (!form.reportValidity()) return;remember();
      execute(async () => {
        if (!itemsComplete()) throw new Error("지급·공제 항목의 금액을 0원 이상 정수로 입력하세요.");
        const person = employee(), payload = {business_id:businessId, employee_email:email(person),
          employee_name:person.name || person.employee_name, employee_request_id:person.id || "", payroll_month:month,
          ...(active?.employment_tax_type ? {employment_tax_type:active.employment_tax_type} : {}), ...itemPayload(),
          ...(payDateInput.value.trim() ? {pay_date:payDateInput.value.trim()} : {}),
          ...(active?.id ? {id:active.id} : {}), ...Object.fromEntries(amounts.map(([name]) => [name, Number(form.elements[name].value)]))};
        const result = await api("/payroll", {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(payload)});
        await saved(result.statement);
      });
    };
    host.querySelectorAll("[data-payroll-action]").forEach(button => {button.onclick = () => execute(async () => {
      const action = button.dataset.payrollAction;
      if (action === "defaults") {
        const result = await api(`/payroll/defaults?${new URLSearchParams({employee_email:email(employee()),employee_request_id:employee().id||"",payroll_month:month})}`);
        if (!current()) return;
        for (const [name] of amounts) if (result.defaults?.[name] !== undefined) form.elements[name].value = result.defaults[name] ?? "";
        planTaxType = result.defaults?.employment_tax_type || "";
        if (!payDateInput.value.trim() && result.contract_terms?.pay_date) payDateInput.value = result.contract_terms.pay_date;
        renderItems();syncDerived();
        host.querySelector("[data-payroll-basis]").textContent = [result.basis,result.reason,result.period_note].filter(Boolean).join(" · ");
        remember();totals();return;
      }
      if (action === "settlement") {
        const plan = await api(`/payroll/defaults?${new URLSearchParams({employee_email:email(employee()),employee_request_id:employee().id||"",payroll_month:month})}`);
        if (plan.contract_type !== "freelancer") throw new Error("서명 완료된 프리랜서 용역계약서가 필요합니다.");
        planTaxType = "freelancer_33";
        const gross = form.elements.gross_pay.value;
        if (!gross || !Number.isSafeInteger(Number(gross)) || Number(gross) <= 0) {
          host.querySelector("[data-payroll-basis]").textContent = `계약 용역비 ${money(plan.contract_terms?.wage)} · 용역기간 ${plan.contract_terms?.start_date || "확인 필요"} ~ ${plan.contract_terms?.end_date || "별도 합의"}. 총지급액 칸에 이번 지급의 확정금액을 입력한 후 다시 누르세요. 총계약금을 매월 자동 지급하지 않습니다.`;
          throw new Error("이번 지급의 확정 용역비를 총지급액에 입력하세요.");
        }
        if (!confirm(`${month} ${employee().name || employee().employee_name} · 이번 용역 정산금액 ${money(gross)}으로 초안을 저장할까요? 계약 총액·검수·분할 지급 여부를 확인하세요. 확정·지급·발송은 별도입니다.`)) return;
        const result = await api("/payroll/draft-from-contract",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({business_id:businessId,employee_email:email(employee()),employee_request_id:employee().id||"",payroll_month:month,gross_pay:Number(gross),amount_confirmed:true})});
        await saved(result.statement);return;
      }
      if (action === "draft") {
        if (!confirm(`${month} ${employee().name || employee().employee_name} 직원의 승인 근태로 초안을 저장할까요?`)) return;
        const result = await api("/payroll/draft-from-attendance",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({business_id:businessId,employee_email:email(employee()),employee_request_id:employee().id||"",payroll_month:month})});
        await saved(result.statement);return;
      }
      if (!active?.id) throw new Error("먼저 초안을 저장하고 명세를 선택하세요.");
      if (dirty()) throw new Error("입력한 수정 내용을 먼저 저장하세요.");
      const path = `/payroll/${encodeURIComponent(active.id)}`;
      if (action === "pdf") {await downloadPdf(active.id);message.textContent="PDF를 열었습니다.";return;}
      if (action === "versions") {
        const result = await api(path+"/versions");
        if(current())host.querySelector("[data-payroll-versions]").innerHTML = `<h3>변경 이력</h3>${(result.versions||[]).map(row=>`<p>버전 ${row.version} · ${statusName(row.status)} · 총지급 ${esc(money(row.gross_pay))} · 실수령 ${esc(money(row.net_pay))}</p>`).join("")}`;
        message.textContent="변경 이력을 조회했습니다.";return;
      }
      const label = {confirm:"명세 확정",pay:"지급 완료로 기록",deliver:"명세서 발송"}[action];
      if (!label || !confirm(`${month} ${employee().name || employee().employee_name} · ${label}하시겠습니까?`)) return;
      const result = await api(path+"/"+action,{method:"POST"});
      if(result.statement)await saved(result.statement);else message.textContent="요청을 처리했습니다.";
    });});
    selectEmployee();
  }
  window.ObysPayrollAdmin = {mount, resolvePayrollEmployee, statementOwnership, ownsStatement, employeeKeyFor, sha256Hex};
  if (typeof module !== "undefined" && module.exports) module.exports = window.ObysPayrollAdmin;
})();
