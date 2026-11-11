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
  // 전월 차액 정산 항목: 서버가 금액을 계산한다. 화면에서는 읽기 전용이고 '이번 달 제외'만 할 수 있다.
  // 확정·지급된 명세에 이미 들어간 줄(사실)은 정정 초안에서도 서버가 그대로 복사하므로 제외할 수 없다.
  const TOPUP_LABEL = "전월 차액 추가 정산";
  const RECOVER_CODE = "prior_month_overpaid_recover";
  const isSettlement = row => Boolean(row?.settlement_of_root_id);
  const settlementRowHtml = (kind, row, fact = false) => `<tr data-settlement-row>
      <td>전월 차액 정산</td><td>${esc(row.label)}</td><td>${esc(Number(row.amount).toLocaleString("ko-KR"))}</td>${kind === "pay" ? `<td>${esc(row.calc_note || "")}</td>` : ""}
      <td>${fact ? "<small>지급 완료 사실</small>" : `<button type="button" class="btn" style="min-height:44px" data-settle-off="${esc(row.settlement_of_root_id)}">이번 달 제외</button>`}</td></tr>`;
  // 신고 상태·보류 금액: 보류 금액은 공제가 아니라 직원에게 지급할 예정인 금액이다. 비율은 원 단위 계산 보조일 뿐 저장하지 않는다.
  const UNREPORTED_WARNING = "신고 미처리 · 노무·세무 확인 필요";
  const HELD_NOTE = "보류 금액은 공제가 아니라 직원에게 지급할 예정인 금액입니다. 공제 항목·세금 항목에는 기록되지 않습니다.";
  const isHeldLine = row => Boolean(row) && (row.code === "held_release" || String(row.source || "").toLowerCase() === "held_release");
  const heldRowHtml = row => `<tr data-held-line>
      <td>보류금 지급</td><td>${esc(row.label)}</td><td>${esc(Number(row.amount).toLocaleString("ko-KR"))}</td><td>${esc(row.calc_note || "")}</td><td><small>서버 계산 · 수정 불가</small></td></tr>`;
  const heldSum = rows => (rows || []).reduce((total, row) => total + (Number(row.amount) || 0), 0);
  const heldSnapshot = (status, reason, rows, release) => JSON.stringify({s:status || "reported", r:status === "unreported" ? String(reason || "").trim() : "",
    h:(rows || []).map(row => [Number(row.amount), String(row.reason || "").trim()]), l:Number(release) || 0});
  const statusName = value => ({draft:"초안",needs_review:"검토 필요",confirmed:"확정",paid:"지급 완료"}[value] || "상태 확인 필요");
  const esc = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));

  // 지급 처리 대화상자: 지급액(기본 실수령)·지급일(기본 오늘)·출처. 명세 금액은 바꾸지 않고 실제 지급 기록만 받는다.
  const isoToday = () => { const d = new Date(Date.now() + 9 * 3600 * 1000); return d.toISOString().slice(0, 10); };
  const payoutDialogHtml = (statement, today = isoToday()) => `<form method="dialog" data-payout-form>
    <h3 style="margin:0 0 8px">지급 처리</h3>
    <p style="margin:0 0 8px">기준명세 실수령 ${Number(statement?.net_pay || 0).toLocaleString("ko-KR")}원 · 실제 지급한 금액을 입력하세요.</p>
    <label style="display:block;margin-bottom:8px">지급액(원)<input class="field" style="min-height:44px;width:100%" inputmode="numeric" data-payout-amount value="${esc(statement?.payout?.amount ?? statement?.net_pay ?? 0)}"></label>
    <label style="display:block;margin-bottom:8px">지급일<input class="field" type="date" style="min-height:44px;width:100%" data-payout-date value="${esc(today)}"></label>
    <label style="display:block;margin-bottom:8px">지급액 출처<select class="field" style="min-height:44px;width:100%" data-payout-source><option value="manual">직접 입력</option><option value="bank">계좌 내역</option><option value="sheet">급여 시트</option></select></label>
    <p role="alert" data-payout-error></p>
    <div style="display:flex;flex-wrap:wrap;gap:8px"><button type="button" class="btn" style="min-height:44px" data-payout-cancel>취소</button><button type="button" class="btn" style="min-height:44px" data-payout-ok>지급 완료로 기록</button></div></form>`;
  const parsePayoutInput = ({amount, date, source}) => {
    const text = String(amount ?? "").trim().replace(/,/g, "");
    if (!/^\d+$/.test(text) || !Number.isSafeInteger(Number(text))) return {error:"지급액은 원 단위 정수로 입력하세요."};
    if (!/^\d{4}-\d{2}-\d{2}$/.test(String(date || ""))) return {error:"지급일을 선택하세요."};
    return {body:{paid_amount:Number(text), paid_date:date, paid_source:source || "manual"}};
  };
  // 브라우저에서는 대화상자, DOM 이 없는 환경(합성 테스트)에서는 null 을 돌려 기존 확인창 흐름을 쓴다.
  const askPayout = statement => new Promise(resolve => {
    if (typeof document === "undefined" || !document.createElement || !document.body) {resolve(null); return;}
    const dialog = document.createElement("dialog");
    dialog.style.cssText = "width:min(420px,calc(100vw - 28px));padding:16px;border:1px solid var(--line);border-radius:12px";
    dialog.innerHTML = payoutDialogHtml(statement);
    const close = value => {dialog.close ? dialog.close() : null; dialog.remove(); resolve(value);};
    dialog.querySelector("[data-payout-cancel]").onclick = () => close(undefined);
    dialog.querySelector("[data-payout-ok]").onclick = () => {
      const parsed = parsePayoutInput({amount:dialog.querySelector("[data-payout-amount]").value, date:dialog.querySelector("[data-payout-date]").value, source:dialog.querySelector("[data-payout-source]").value});
      if (parsed.error) {dialog.querySelector("[data-payout-error]").textContent = parsed.error; return;}
      close(parsed.body);
    };
    dialog.addEventListener("cancel", () => close(undefined));
    document.body.appendChild(dialog);
    dialog.showModal ? dialog.showModal() : dialog.setAttribute("open", "");
  });

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
    let pay = [], ded = [], work = {}, optOut = [], planTaxType = "", familyCount = "", workMinutesBase = "";
    let reportStatus = "reported", unreportedReason = "", heldItems = [], heldRelease = "";
    const activeHeld = () => ({status:active?.report_status || "reported", reason:active?.unreported_reason || "", items:active?.held_items || [], release:active?.held_release_amount || 0});
    const heldNow = () => heldSnapshot(reportStatus, unreportedReason, heldItems, heldRelease);
    const heldBaseline = () => {const base = activeHeld(); return heldSnapshot(base.status, base.reason, base.items, base.release);};
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
      <p data-payroll-blocked role="alert"></p><p data-payroll-link-notice role="status"></p><p data-payroll-state role="status"></p><p data-payroll-basis></p><p data-payroll-labor-risk role="status"></p>
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
    const calcPayload = row => row.calc && row.calc.rate !== "" && row.calc.qty !== "" && row.calc.rate !== undefined && row.calc.qty !== undefined ? {rate:Number(row.calc.rate), qty:Number(row.calc.qty), unit:row.calc.unit || "hour"} : null;
    const snapshot = (p, d, w) => JSON.stringify({p:p.map(row => [row.code, row.label, Number(row.amount), row.calc_note || "", calcPayload(row)]), d:d.map(row => [row.code, row.label, Number(row.amount), row.source || "", Boolean(row.confirmed), row.calc_note || ""]),
      w:workFields.map(([name]) => w[name] === undefined || w[name] === "" ? null : Number(w[name]))});
    const remember = () => employee() && drafts.set(key(), {id:active?.id || "", ...inputValues(), pay_date:payDateInput.value, items:clone({pay, ded, work, optOut, familyCount, held:{status:reportStatus, reason:unreportedReason, items:heldItems, release:heldRelease}})});
    const dirty = () => amounts.some(([name]) => form.elements[name].value !== String(active?.[name] ?? ""))
      || payDateInput.value !== String(active?.pay_date ?? "")
      || snapshot(pay, ded, work) !== snapshot(active?.pay_items || [], active?.deduction_items || [], active?.work_summary || {})
      || String(familyCount ?? "") !== String(active?.tax_family_count || "")
      || JSON.stringify(optOut) !== JSON.stringify(active?.settlement_opt_out || [])
      || heldNow() !== heldBaseline();
    const heldError = () => {
      if (reportStatus === "unreported" && !unreportedReason.trim()) return "신고 미처리 사유를 입력하세요. 사유 없이는 저장할 수 없습니다.";
      for (const row of heldItems) {
        if (!/^\d+$/.test(String(row.amount ?? "").trim()) || !Number.isSafeInteger(Number(row.amount)) || Number(row.amount) <= 0) return "보류 금액은 1원 이상 원 단위 정수로 입력하세요.";
        if (!String(row.reason || "").trim()) return "보류 금액마다 사유를 입력하세요.";
      }
      if (heldRelease !== "" && (!/^\d+$/.test(String(heldRelease).trim()) || !Number.isSafeInteger(Number(heldRelease)))) return "보류금 지급 금액은 0 이상 원 단위 정수로 입력하세요.";
      return "";
    };
    const heldPayload = () => ({
      report_status:reportStatus,
      ...(reportStatus === "unreported" ? {unreported_reason:unreportedReason.trim()} : {}),
      held_items:heldItems.map(row => ({amount:Number(row.amount), reason:String(row.reason || "").trim()})),
      held_release_amount:Number(heldRelease) || 0
    });
    const itemPayload = () => ({
      // 정산 항목은 서버가 버리고 다시 계산한다. 그래도 보내는 이유는 총지급액 칸에 포함된 금액을 서버가 빼기 위해서다.
      pay_items:pay.map(row => ({code:row.code, label:row.label, amount:Number(row.amount), calc_note:row.calc_note || "", ...(calcPayload(row) ? {calc:calcPayload(row)} : {}), ...(isSettlement(row) ? {settlement_of_root_id:row.settlement_of_root_id} : {})})),
      deduction_items:ded.map(row => ({code:row.code, label:row.label, amount:Number(row.amount), ...(row.source === "auto" || row.source === "manual" ? {source:row.source} : {}), ...(row.confirmed ? {confirmed:true} : {}), ...(row.calc_note ? {calc_note:row.calc_note} : {}), ...(isSettlement(row) ? {settlement_of_root_id:row.settlement_of_root_id} : {})})),
      settlement_opt_out:optOut,
      ...(familyCount !== "" && Number.isSafeInteger(Number(familyCount)) && Number(familyCount) >= 1 ? {tax_family_count:Number(familyCount)} : {}),
      ...heldPayload(),
      work_summary:Object.fromEntries(workFields.filter(([name]) => work[name] !== undefined && work[name] !== "").map(([name]) => [name, Number(work[name])]))
    });
    const itemsComplete = () => [...pay, ...ded].every(row => row.amount !== "" && row.amount !== undefined && Number.isSafeInteger(Number(row.amount)) && Number(row.amount) >= 0);
    // 항목을 쓰면 해당 합계 칸은 항목 합계로 고정(읽기 전용)한다. 항목이 없으면 기존 간편 입력 그대로.
    const syncDerived = () => {
      const lock = (name, on, value) => {const control = form.elements[name]; control.readOnly = on; if (on) control.value = String(value);};
      lock("gross_pay", pay.length > 0, sumItems(pay));
      lock("taxable_pay", pay.length > 0, Math.max(sumItems(pay) - sumItems(pay.filter(row => isSettlement(row) || isHeldLine(row))) - (Number(form.elements.non_tax_meal_allowance.value) || 0), 0));  // 전월 정산·보류금 지급분은 과세 기준 제외
      lock("tax_withholding", ded.length > 0, sumItems(ded, taxCodes));
      lock("insurance_deduction", ded.length > 0, sumItems(ded, insuranceCodes));
      lock("other_deduction", ded.length > 0, sumItems(ded, ["other", RECOVER_CODE]));
      const insurance = host.querySelector('[data-wrap="insurance_deduction"]');
      if (insurance) insurance.style.display = isFreelancer() && !Number(form.elements.insurance_deduction.value) ? "none" : "";
      if (isFreelancer() && form.elements.insurance_deduction.value === "") form.elements.insurance_deduction.value = "0";
    };
    const isFactRow = row => Boolean(active?.supersedes_id || ["confirmed", "paid"].includes(active?.status)) && row.label !== TOPUP_LABEL;
    const AUTO_NOTE = /^(?:시급 [\d,]+원 × 승인 근로 \d+분\(분 단위 환산, 원 미만 반올림\)|용역비\(시급 [\d,]+원 × 승인 \d+분\) — 분 단위 환산, 원 미만 반올림)$/;
    const CALC_UNITS = [["hour", "시간"], ["minute", "분"], ["day", "일"], ["month", "월"], ["count", "건"]];
    const calcReady = row => {const c = row.calc; return Boolean(c) && c.rate !== "" && c.rate !== undefined && c.qty !== "" && c.qty !== undefined && Number.isFinite(Number(c.rate)) && Number(c.rate) >= 0 && Number.isFinite(Number(c.qty)) && Number(c.qty) >= 0 && CALC_UNITS.some(([unit]) => unit === c.unit);};
    const calcAmount = c => Math.round(Number(c.rate) * Number(c.qty) / (c.unit === "minute" ? 60 : 1) + 1e-9);
    const calcNote = c => `${c.unit === "hour" || c.unit === "minute" ? "시급" : "단가"} ${Number(c.rate).toLocaleString("ko-KR")}원 × ${Number(c.qty)}${(CALC_UNITS.find(([unit]) => unit === c.unit) || [])[1] || ""}`;
    const calcFlagText = row => calcReady(row) && row.amount !== "" && Number(row.amount) !== calcAmount(row.calc) ? `계산값 ${calcAmount(row.calc).toLocaleString("ko-KR")}원과 다름` : "";
    const calcRowHtml = (kind, row, index) => {
      const c = row.calc || {rate:"", qty:"", unit:"hour"}, at = `data-item="${kind}" data-i="${index}"`;
      return `<tr class="pi-calc" data-calc-row="${index}"><td colspan="5"><div style="display:flex;flex-wrap:wrap;gap:8px;align-items:center">
        <label>단가(원)<input class="field" style="min-height:44px;min-width:110px" type="number" min="0" step="1" inputmode="numeric" ${at} data-k="calc_rate" value="${esc(c.rate)}"></label>
        <label>수량<input class="field" style="min-height:44px;min-width:90px" type="number" min="0" step="0.01" inputmode="decimal" ${at} data-k="calc_qty" value="${esc(c.qty)}"></label>
        <label>단위<select class="field" style="min-height:44px" ${at} data-k="calc_unit">${CALC_UNITS.map(([unit, label]) => `<option value="${unit}"${unit === (c.unit || "hour") ? " selected" : ""}>${label}</option>`).join("")}</select></label>
        <small data-calc-flag="${index}" role="status">${esc(calcFlagText(row))}</small></div></td></tr>`;
    };
    // 공제 항목 '계산방법' 칸: 자동 문구(읽기 전용)·수동 배지·계산식 입력(기준금액·요율)·0원 확인·자동값으로 되돌리기.
    // 요율과 계산은 서버가 한다 — 화면은 서버가 돌려준 기준금액·요율(%)을 보여 주고 고친 값을 미리보기 API 로 보낸다.
    const isAutoRow = row => row.source === "auto";
    const dedBadge = row => row.needs_input && !row.confirmed ? "확인 필요" : isAutoRow(row) ? "자동" : "수동";
    const dedNoteCell = (row, index) => {
      const at = `data-item="ded" data-i="${index}"`, f = row.formula, controls = [...insuranceCodes, ...taxCodes].includes(row.code) && !isFreelancer();
      return `<td class="pi-note pi-ded"><input class="field" style="min-height:44px" aria-label="계산방법" ${at} data-k="calc_note" maxlength="200" placeholder="계산방법·메모" value="${esc(row.calc_note || "")}"${isAutoRow(row) ? " readonly" : ""}>
        ${controls ? `<div class="pi-ded-ctl" style="display:flex;flex-wrap:wrap;gap:6px;align-items:center"><small data-ded-badge="${index}" role="status">${dedBadge(row)}</small>
        ${f ? `<label>기준금액(원)<input class="field" style="min-height:44px;min-width:110px" type="number" min="0" step="1" inputmode="numeric" ${at} data-k="f_base" value="${esc(f.base)}"></label><label>요율(%)<input class="field" style="min-height:44px;min-width:80px" type="number" min="0" max="100" step="any" inputmode="decimal" ${at} data-k="f_rate" value="${esc(f.rate_pct)}"></label>` : ""}
        <label class="pi-chk" style="display:inline-flex;align-items:center;gap:4px;min-height:44px"><input type="checkbox" style="width:24px;height:24px;min-height:24px;min-width:24px" ${at} data-k="confirmed"${row.confirmed ? " checked" : ""}>0원 확인</label>
        <button type="button" class="btn" style="min-height:44px;min-width:44px" data-ded-revert="${index}"${isAutoRow(row) ? " hidden" : ""}>자동값으로 되돌리기</button></div>` : ""}</td>`;
    };
    const ITEM_CSS = `<style data-payroll-item-style>@media (max-width:480px){.payroll-item-table{display:block;white-space:normal}.payroll-item-table thead{display:none}.payroll-item-table tbody{display:block}.payroll-item-table tbody tr.pi-row,.payroll-item-table tbody tr.pi-calc{display:flex;flex-wrap:wrap;gap:6px;padding:8px 0;border-top:1px solid #eef1f5;cursor:default}.payroll-item-table tr.pi-row>td,.payroll-item-table tr.pi-calc>td{display:block;border:0;padding:0;min-width:0;box-sizing:border-box}.payroll-item-table tr.pi-row>td.pi-code{flex:1 1 30%}.payroll-item-table tr.pi-row>td.pi-label{flex:1 1 40%}.payroll-item-table tr.pi-row>td.pi-del{flex:0 0 64px;order:3}.payroll-item-table tr.pi-row>td.pi-amount{flex:1 1 35%;order:4}.payroll-item-table tr.pi-row>td.pi-note{flex:1 1 55%;order:5}.payroll-item-table tr.pi-calc>td[colspan]{flex:1 1 100%;order:6}.payroll-item-table tr.pi-row>td.pi-code select,.payroll-item-table tr.pi-row>td.pi-label input,.payroll-item-table tr.pi-row>td.pi-amount input,.payroll-item-table tr.pi-row>td.pi-note input,.payroll-item-table tr.pi-row>td.pi-del .btn,.payroll-item-table tr.pi-calc input,.payroll-item-table tr.pi-calc select{width:100%;min-height:44px;min-width:44px;box-sizing:border-box}.payroll-item-table tr.pi-calc label{flex:1 1 90px}.payroll-item-table tr.pi-row>td.pi-note.pi-ded{flex:1 1 100%;order:5}.payroll-item-table tr.pi-row>td.pi-note button,.payroll-item-table tr.pi-row>td.pi-note label.pi-chk{min-height:44px;min-width:44px;box-sizing:border-box}.payroll-item-table tr.pi-row>td.pi-note .pi-ded-ctl input[type=number]{width:auto;flex:1 1 90px}}</style>`;
    const itemTable = (kind, rows, codes, title) => `<h3>${title}</h3>${rows.length ? `${ITEM_CSS}<table class="payroll-item-table"><thead><tr><th>구분</th><th>항목명</th><th>금액(원)</th><th>계산방법</th><th></th></tr></thead><tbody>${rows.map((row, index) => isSettlement(row) ? settlementRowHtml(kind, row, isFactRow(row)) : kind === "pay" && isHeldLine(row) ? heldRowHtml(row) : `<tr class="pi-row">
      <td class="pi-code"><select class="field" style="min-height:44px" aria-label="구분" data-item="${kind}" data-i="${index}" data-k="code">${codes.map(([code, label]) => `<option value="${code}"${code === row.code ? " selected" : ""}>${label}</option>`).join("")}</select></td>
      <td class="pi-label"><input class="field" style="min-height:44px" aria-label="항목명" data-item="${kind}" data-i="${index}" data-k="label" maxlength="40" value="${esc(row.label)}"></td>
      <td class="pi-amount"><input class="field" style="min-height:44px" aria-label="금액(원)" type="number" min="0" step="1" data-item="${kind}" data-i="${index}" data-k="amount" value="${esc(row.amount)}"></td>
      ${kind === "pay" ? `<td class="pi-note"><input class="field" style="min-height:44px" aria-label="계산방법" data-item="${kind}" data-i="${index}" data-k="calc_note" maxlength="200" placeholder="예: 시급 × 시간" value="${esc(row.calc_note || "")}"></td>` : dedNoteCell(row, index)}
      <td class="pi-del"><button type="button" class="btn" style="min-height:44px;min-width:44px" aria-label="삭제" data-item-del="${kind}" data-i="${index}">삭제</button></td></tr>${kind === "pay" ? calcRowHtml(kind, row, index) : ""}`).join("")}</tbody></table>` : "<p>항목 없음 — 위 합계 칸에 간편 입력할 수 있습니다.</p>"}
      <button type="button" class="btn" style="min-height:44px" data-item-add="${kind}">${kind === "pay" ? "지급 항목 추가" : "공제 항목 추가"}</button>`;
    const heldBalanceOf = () => {
      const total = active?.held_balance_total ?? active?.statement_payload?.held_balance_total;
      const own = active?.held_balance ?? active?.statement_payload?.held_balance;
      return {total: total === undefined || total === null ? null : Number(total), own: own === undefined || own === null ? null : Number(own)};
    };
    const heldSectionHtml = () => {
      const balance = heldBalanceOf();
      const unreported = reportStatus === "unreported";
      return `<h3>신고 상태 · 보류 금액</h3>
      <div class="toolbar"><label>신고 상태<select class="field" style="min-height:44px" data-report-status><option value="reported"${unreported ? "" : " selected"}>신고 처리</option><option value="unreported"${unreported ? " selected" : ""}>신고 미처리</option></select></label>
      ${unreported ? `<label>신고 미처리 사유(필수)<input class="field" style="min-height:44px" data-report-reason maxlength="100" value="${esc(unreportedReason)}"></label>` : ""}</div>
      ${unreported ? `<p role="alert" data-unreported-warning><strong>${UNREPORTED_WARNING}</strong> · 이 명세는 신고(원천세·4대보험)가 처리되지 않은 상태로 기록됩니다.</p>` : ""}
      <p>${HELD_NOTE}</p>
      ${heldItems.length ? `<table><thead><tr><th>보류 금액(원)</th><th>보류 사유</th><th></th></tr></thead><tbody>${heldItems.map((row, index) => `<tr>
        <td><input class="field" style="min-height:44px" type="number" min="1" step="1" data-held-field="amount" data-hi="${index}" value="${esc(row.amount)}"></td>
        <td><input class="field" style="min-height:44px" maxlength="100" data-held-field="reason" data-hi="${index}" value="${esc(row.reason)}"></td>
        <td><button type="button" class="btn" style="min-height:44px" data-held-del="${index}">삭제</button></td></tr>`).join("")}</tbody></table>` : "<p>보류 금액 없음</p>"}
      <div class="toolbar"><button type="button" class="btn" style="min-height:44px" data-held-add="1">보류 금액 추가</button>
      <label>비율 계산 보조(%)<input class="field" style="min-height:44px" type="number" min="0" step="any" data-held-ratio placeholder="직접 입력"></label>
      <button type="button" class="btn" style="min-height:44px" data-held-calc="1">총지급액 기준 원 단위 계산</button></div>
      <p><small>비율은 원 단위 금액을 계산하는 보조 도구입니다. 저장되는 것은 금액과 사유뿐이며 비율은 저장하지 않습니다.</small></p>
      <label>이번 명세에서 지급할 보류금(원, 이전 달 보류 잔액 지급)<input class="field" style="min-height:44px" type="number" min="0" step="1" data-held-release value="${esc(heldRelease)}"></label>
      <p role="status" data-held-balance>${balance.total !== null ? `직원 누적 보류 잔액 ${esc(money(balance.total))}(지급 예정, 공제 아님)` : "누적 보류 잔액은 저장 후 서버가 계산합니다."}${balance.own !== null && balance.total !== null && balance.own !== balance.total ? ` · 이 명세 기준월 잔액 ${esc(money(balance.own))}` : ""}</p>`;
    };
    const renderItems = () => {
      const freelancer = isFreelancer();
      itemsHost.innerHTML = itemTable("pay", pay, payCodes, "지급 항목") + itemTable("ded", ded, freelancer ? dedCodes.filter(([code]) => !insuranceCodes.includes(code)) : dedCodes, freelancer ? "원천징수 항목" : "공제 항목")
        + heldSectionHtml()
        + (optOut.length ? `<p role="status">이번 달 제외한 전월 차액 ${optOut.length}건 · 다음 명세에 다시 반영됩니다. <button type="button" class="btn" style="min-height:44px" data-settle-restore="1">제외 해제</button></p>` : "")
        + (freelancer ? '<button type="button" class="btn" style="min-height:44px" data-item-tax="1">3.3% 자동 계산(소득세 3% · 지방소득세 0.3%)</button>' : "")
        + `<h3>근로일수 및 근로시간</h3><div class="toolbar">${workFields.map(([name, label]) => `<label>${label}<input class="field" style="min-height:44px" type="number" min="0" step="1" data-work-field="${name}" value="${esc(work[name] ?? "")}"></label>`).join("")}<label>공제대상가족(본인 포함, 명)<input class="field" style="min-height:44px" type="number" min="1" max="20" step="1" inputmode="numeric" data-family-field="1" value="${esc(familyCount)}"></label></div>`;
    };
    const refresh = () => {syncDerived(); remember(); totals(); restoreButtons();};
    // ---- 공제 자동 재계산: 저장 없이 서버 미리보기를 호출해 수동이 아닌 공제 행만 갱신한다(요율·계산식은 서버 정본). ----
    let previewTimer = null, previewSeq = 0;
    const dedAmountsValid = () => ded.every(row => row.amount !== "" && row.amount !== undefined && Number.isSafeInteger(Number(row.amount)) && Number(row.amount) >= 0);
    const previewEligible = () => Boolean(employee()) && !isFreelancer() && form.elements.taxable_pay.value !== "" && Number.isSafeInteger(Number(form.elements.taxable_pay.value)) && Number(form.elements.taxable_pay.value) >= 0;
    const previewRow = row => ({code:row.code, label:row.label, amount:Number(row.amount) || 0, source:isAutoRow(row) ? "auto" : "manual", ...(row.confirmed ? {confirmed:true} : {}), ...(row.calc_note ? {calc_note:row.calc_note} : {}), ...(row.note_manual ? {note_manual:true} : {}),
      ...(row.formula?.edited ? {formula:{base:row.formula.base, rate_pct:row.formula.rate_pct, edited:true}} : {})});
    const focusedIn = () => typeof document !== "undefined" && document.activeElement && itemsHost.contains && itemsHost.contains(document.activeElement);
    const syncDedRowUi = index => {
      const row = ded[index], at = `[data-item="ded"][data-i="${index}"]`, find = selector => itemsHost.querySelector?.(selector);
      if (!row) return;
      const badge = find(`[data-ded-badge="${index}"]`), revert = find(`[data-ded-revert="${index}"]`), note = find(`${at}[data-k="calc_note"]`);
      if (badge) badge.textContent = dedBadge(row);
      if (revert) revert.hidden = isAutoRow(row);
      if (note) {note.readOnly = isAutoRow(row); if (note !== (typeof document !== "undefined" ? document.activeElement : null)) note.value = row.calc_note || "";}
      for (const [k, value] of [["amount", row.amount], ["f_base", row.formula?.base], ["f_rate", row.formula?.rate_pct]]) {
        const box = find(`${at}[data-k="${k}"]`); if (box && value !== undefined && box !== (typeof document !== "undefined" ? document.activeElement : null)) box.value = value;
      }
      const check = find(`${at}[data-k="confirmed"]`); if (check) check.checked = Boolean(row.confirmed);
    };
    const dedSignature = rows => rows.map(row => [row.code, isSettlement(row) ? "s" : "d", Boolean(row.formula), [...insuranceCodes, ...taxCodes].includes(row.code)].join(":")).join("|");
    // 행 편집 순번: 요청 시점의 rev 와 응답 적용 시점의 rev 가 다르면 그 행은 응답으로 덮어쓰지 않는다.
    const bump = row => {row.rev = (row.rev || 0) + 1;};
    const applyPreview = (result, sentRev, formulaOnly) => {
      let stale = false;
      const unchanged = row => sentRev.has(row) && sentRev.get(row) === (row.rev || 0);
      const before = dedSignature(ded), mine = ded.filter(row => !isSettlement(row)), rest = ded.filter(isSettlement), used = new Set();
      if (formulaOnly) {
        for (const server of result.deduction_items || []) {
          const row = mine.find(item => !used.has(item) && item.code === server.code);
          if (row) {used.add(row); if (!unchanged(row)) stale = true; else if (server.formula) row.formula = server.formula;}
        }
        if (!focusedIn()) renderItems();
        if (stale) schedulePreview();
        return;
      }
      const next = (result.deduction_items || []).map(server => {
        const row = mine.find(item => !used.has(item) && item.code === server.code);
        if (!row) return {...server};
        used.add(row);
        if (!unchanged(row)) {stale = true; return row;}
        if (isAutoRow(row) || row.formula?.edited) return {...server, ...(row.note_manual ? {note_manual:true} : {})};
        // 수동 행은 덮어쓰지 않는다. 확인 대기 여부만 서버 판정을 따른다.
        row.needs_input = Boolean(server.needs_input) && !row.confirmed;
        return row;
      });
      ded = [...next, ...mine.filter(row => !used.has(row)), ...rest];
      if (dedSignature(ded) !== before) renderItems();
      else ded.forEach((_, index) => syncDedRowUi(index));
      refresh();
      if (stale) schedulePreview();
    };
    // 저장 직전에 최신 계산을 기다리기 위한 상태: 진행 중인 요청과 마지막 요청의 실패 여부.
    let previewInflight = null, previewFailed = false;
    const runPreview = (opts = {}) => {
      const promise = doPreview(opts).finally(() => {if (previewInflight === promise) previewInflight = null;});
      previewInflight = promise;
      return promise;
    };
    const doPreview = async (opts = {}) => {
      previewFailed = false;
      if (!previewEligible() || !current() || !dedAmountsValid()) return;
      const seq = ++previewSeq, gen = generation, sent = ded.filter(row => !isSettlement(row)), person = employee(), sentRev = new Map(sent.map(row => [row, row.rev || 0]));
      const body = {payroll_month:month, taxable_pay:Number(form.elements.taxable_pay.value), employee_email:email(person), employee_request_id:person.id || "",
        ...(familyCount !== "" ? {family:Number(familyCount)} : active?.tax_family_count ? {family:active.tax_family_count} : {}),
        deduction_items:sent.map(previewRow)};
      try {
        const result = await api("/payroll/deductions/preview", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(body)});
        // 늦게 도착한 이전 요청, 다른 직원·명세로 바뀐 뒤의 응답은 버린다.
        if (!current() || seq !== previewSeq || gen !== generation || !result) return;
        applyPreview(result, sentRev, opts.formulaOnly === true);
      } catch (error) {
        if (!opts.formulaOnly && current() && seq === previewSeq && gen === generation) {
          previewFailed = true;
          message.textContent = `공제 자동 계산을 하지 못했습니다: ${error.message}`;
        }
      }
    };
    // 저장 직전: 예약된 미리보기를 바로 실행하고 끝날 때까지 기다린다. 최신 계산을 얻지 못하면 저장하지 않는다.
    const settlePreview = async () => {
      for (let round = 0; round < 5; round++) {
        if (previewTimer) {clearTimeout(previewTimer); previewTimer = null; await runPreview();}
        else if (previewInflight) await previewInflight;
        else break;
      }
      if (previewTimer || previewInflight) throw new Error("공제 자동 계산이 끝나지 않았습니다. 잠시 후 다시 저장하세요.");
      if (previewFailed && previewEligible()) throw new Error("공제 자동 계산에 실패해 저장하지 않았습니다. 입력값을 확인하고 다시 시도하세요.");
    };
    const schedulePreview = (delay = 350) => {
      previewSeq++;  // 이미 날아간 요청의 응답은 더 이상 유효하지 않다
      clearTimeout(previewTimer);
      previewTimer = null; previewFailed = false;
      if (previewEligible()) previewTimer = setTimeout(() => {previewTimer = null; runPreview();}, delay);
    };
    // 공제 행의 수동 편집: 금액·계산식 입력·0원 확인은 그 행을 수동으로 바꾼다.
    const dedEdit = (row, data, control) => {
      if (data.k === "confirmed") {row.confirmed = control.checked; row.source = "manual"; if (row.confirmed) row.needs_input = false;}
      else if (data.k === "amount") {row[data.k] = control.value; row.source = "manual"; if (row.formula) row.formula.edited = false;}  // 금액 직접 입력이 계산식 수정보다 나중이면 직접 입력값을 보존한다
      else {row.formula = {...(row.formula || {}), [data.k === "f_base" ? "base" : "rate_pct"]:control.value, edited:true}; row.source = "manual";}
    };
    itemsHost.oninput = event => {
      const control = event.target, data = control.dataset || {};
      if (data.workField) work[data.workField] = control.value;
      else if (data.familyField !== undefined) familyCount = control.value;
      else if (data.heldField) {const row = heldItems[Number(data.hi)]; if (!row) return; row[data.heldField] = control.value;}
      else if (control.matches && control.matches("[data-report-reason]")) unreportedReason = control.value;
      else if (control.matches && control.matches("[data-held-release]")) heldRelease = control.value;
      else if (control.matches && control.matches("[data-held-ratio]")) return;
      else if (data.item) {
        const row = (data.item === "pay" ? pay : ded)[Number(data.i)];
        if (!row) return;
        if (data.item === "ded") bump(row);
        if (data.item === "ded" && ["f_base", "f_rate", "confirmed", "amount"].includes(data.k)) dedEdit(row, data, control);
        else if (data.item === "ded" && data.k === "calc_note") {row.calc_note = control.value; row.note_manual = control.value.trim() !== "";}
        else if (data.k.startsWith("calc_")) {
          const prev = row.calc && calcReady(row) ? calcNote(row.calc) : "";
          // 서버 자동 생성 문구("시급 N원 × 승인 근로 M분(…)")도 계산 칸에서 만든 문구로 보고 함께 갱신한다. 관리자가 직접 쓴 문구는 보존한다.
          const autoNote = note => !note || note === prev || AUTO_NOTE.test(note);
          row.calc = {rate:"", qty:"", unit:"hour", ...(row.calc || {}), [data.k.slice(5)]:control.value};
          if (calcReady(row)) {
            row.amount = String(calcAmount(row.calc));
            if (autoNote(row.calc_note)) row.calc_note = calcNote(row.calc);
          }
          const at = `[data-item="${data.item}"][data-i="${data.i}"]`;
          const amountBox = itemsHost.querySelector?.(`${at}[data-k="amount"]`), noteBox = itemsHost.querySelector?.(`${at}[data-k="calc_note"]`);
          if (amountBox) amountBox.value = row.amount; if (noteBox) noteBox.value = row.calc_note || "";
        } else row[data.k] = control.value;
        if (data.item === "pay") {const flag = itemsHost.querySelector?.(`[data-calc-flag="${data.i}"]`); if (flag) flag.textContent = calcFlagText(row);}
        if (data.k === "code" && data.item === "ded") row.source = "manual";
        if (data.k === "code") {row.label = (((data.item === "pay" ? payCodes : dedCodes).find(([code]) => code === row.code)) || [])[1] || row.label; renderItems();}
      }
      if (data.item === "ded") syncDedRowUi(Number(data.i));
      refresh();
      if (data.familyField !== undefined || data.item === "pay" || (data.item === "ded" && data.k !== "calc_note" && data.k !== "label")) schedulePreview();
    };
    itemsHost.onchange = event => {
      const control = event.target;
      if (control.matches && control.matches("[data-report-status]")) {
        reportStatus = control.value === "unreported" ? "unreported" : "reported";
        renderItems(); refresh();
      }
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
      } else if (data.heldAdd) {
        heldItems = [...heldItems, {amount:"", reason:""}];
      } else if (data.heldDel !== undefined) {
        heldItems = heldItems.filter((_, index) => index !== Number(data.heldDel));
      } else if (data.heldCalc) {
        const ratio = Number(itemsHost.querySelector("[data-held-ratio]")?.value);
        const base = Number(form.elements.gross_pay.value) - sumItems(pay.filter(isHeldLine));
        if (!Number.isFinite(ratio) || ratio <= 0 || ratio > 100 || !Number.isSafeInteger(base) || base <= 0) {message.textContent = "비율(0 초과 100 이하)과 총지급액을 먼저 입력하세요. 비율은 계산 보조일 뿐 저장되지 않습니다."; return;}
        heldItems = [...heldItems, {amount:String(Math.floor(base * ratio / 100)), reason:""}];
        message.textContent = "계산한 원 단위 금액을 보류 금액 칸에 넣었습니다. 금액과 사유를 확인하세요. 비율은 저장되지 않습니다.";
      } else if (data.settleOff) {
        // 금액은 고칠 수 없고 끄기만 한다. 합계에서 빠지도록 목록에서 내리고 제외 대상만 서버에 알린다.
        pay = pay.filter(row => row.settlement_of_root_id !== data.settleOff);
        ded = ded.filter(row => row.settlement_of_root_id !== data.settleOff);
        if (!optOut.includes(data.settleOff)) optOut = [...optOut, data.settleOff].sort();
      } else if (data.settleRestore) {
        optOut = [];
        message.textContent = "제외를 해제했습니다. 저장하면 남은 전월 차액이 다시 반영됩니다.";
      } else if (data.dedRevert !== undefined) {
        const row = ded[Number(data.dedRevert)];
        if (!row) return;
        bump(row); row.source = "auto"; row.confirmed = false; row.note_manual = false; if (row.formula) row.formula.edited = false;
        renderItems(); refresh(); schedulePreview(0); return;
      } else if (data.itemDel) {
        (data.itemDel === "pay" ? pay : ded).splice(Number(data.i), 1);
      } else if (data.itemTax) {
        const gross = Number(form.elements.gross_pay.value) - sumItems(pay.filter(row => isSettlement(row) || isHeldLine(row)));  // 전월 정산·보류금 지급분은 3.3% 기준 제외
        if (!Number.isSafeInteger(gross) || gross < 0 || form.elements.gross_pay.value === "") {message.textContent = "총지급액(용역비)을 먼저 입력하세요."; return;}
        if (!seedDeductions(true)) return;
        const tax = withholding(gross);
        ded = [...ded.filter(row => !taxCodes.includes(row.code)), {code:"income_tax", label:"사업소득세(3%)", amount:tax.income}, {code:"local_income_tax", label:"지방소득세(0.3%)", amount:tax.local}];
      } else return;
      renderItems(); refresh();
      // 행을 추가·삭제하면 날아가 있던 미리보기 응답은 낡았다 — schedulePreview 가 순번을 올려 버리게 하고 다시 예약한다.
      if (data.itemAdd || data.itemDel || data.settleOff || data.settleRestore) schedulePreview();
    };
    const totals = () => {
      const values = inputValues();
      const known = amounts.every(([name]) => values[name] !== "" && Number.isSafeInteger(Number(values[name])) && Number(values[name]) >= 0);
      const held = heldSum(heldItems.filter(row => Number.isSafeInteger(Number(row.amount)) && Number(row.amount) > 0));
      host.querySelector("[data-payroll-totals]").textContent = known
        ? `공제 합계 ${money(Number(values.tax_withholding) + Number(values.insurance_deduction) + Number(values.other_deduction))}${held ? ` · 보류 금액(지급 예정, 공제 아님) ${money(held)}` : ""} · 예상 실수령 ${money(Number(values.gross_pay) - Number(values.tax_withholding) - Number(values.insurance_deduction) - Number(values.other_deduction) - held)} · 저장 후 서버 계산값을 확인하세요.${reportStatus === "unreported" ? " · " + UNREPORTED_WARNING : ""}`
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
      const source = values.items || {pay:clone(active?.pay_items) || [], ded:clone(active?.deduction_items) || [], work:clone(active?.work_summary) || {}, optOut:clone(active?.settlement_opt_out) || []};
      pay = source.pay || []; ded = source.ded || []; work = source.work || {}; optOut = source.optOut || [];
      ded.forEach(row => {if (row.source === "manual" && String(row.calc_note || "").trim() && row.note_manual === undefined) row.note_manual = true;});  // 저장된 수동 행의 메모는 관리자 글로 보존한다
      familyCount = values.items && values.items.familyCount !== undefined ? String(values.items.familyCount ?? "") : active?.tax_family_count ? String(active.tax_family_count) : ""; workMinutesBase = work.work_minutes === undefined || work.work_minutes === "" ? "" : String(work.work_minutes);
      const held = source.held || {status:active?.report_status || "reported", reason:active?.unreported_reason || "", items:clone(active?.held_items) || [], release:active?.held_release_amount || ""};
      reportStatus = held.status === "unreported" ? "unreported" : "reported"; unreportedReason = held.reason || ""; heldItems = held.items || []; heldRelease = held.release ? String(held.release) : "";
      renderItems(); syncDerived();
      clearTimeout(previewTimer); previewSeq++;
      if (active?.id) runPreview({formulaOnly:true});
      host.querySelector("[data-payroll-state]").textContent = active
        ? `${month} · ${statusName(active.status)} · 버전 ${active.version || 1}${["confirmed", "paid"].includes(active.status) ? " · 수정 저장 시 새 초안 생성" : ""}`
        : `${month} · 새 초안`;
      host.querySelector("[data-payroll-basis]").textContent = active?.settlement_basis
        ? `${{monthly:"월정액 · 계약 금액",lump:"건별 · 확인한 지급액"}[active.settlement_basis.fee_basis] || "확인한 지급액"} ${money(active.settlement_basis.confirmed_amount)} · 용역기간 ${active.settlement_basis.period_start} ~ ${active.settlement_basis.period_end} · ${active.settlement_basis.scope || "업무범위는 계약 확인"}`
        : active?.attendance_basis
        ? `승인 근태 ${active.attendance_basis.approved_days}일 · ${active.attendance_basis.approved_minutes}분 기준`
          + (Number(active.attendance_basis.default_present_days) > 0 ? ` · 출근(기본) ${active.attendance_basis.default_present_days}일(${(active.attendance_basis.default_present_dates || []).join(", ")})` : "")
        : active?.source_contract_id ? "서명 계약서 기준값 적용" : "계약·근태 기준값을 조회하거나 금액을 확인해 입력하세요.";
      showLaborRisk();
      totals();restoreButtons();
    };
    const showNotices = () => {
      const blocked = blockNotice();
      host.querySelector("[data-payroll-blocked]").textContent = blocked;
      host.querySelector("[data-payroll-link-notice]").textContent = employee() && statements.some(row => row.payroll_month === month && statementOwner(row) === "ambiguous") ? AMBIGUOUS_STATEMENT_TEXT : "";
    };
    const showLaborRisk = () => {host.querySelector("[data-payroll-labor-risk]").textContent = employee() ? (active?.labor_risk_notice || employee()?.labor_risk_notice || "") : "";};
    const selectEmployee = () => {
      generation++;
      planTaxType = "";
      picker.innerHTML = pickerOptions();
      picker.value = employee() ? selected : "";
      showNotices();
      if (!employee()) {
        active = null; pay = []; ded = []; work = {}; optOut = []; familyCount = ""; workMinutesBase = "";
        reportStatus = "reported"; unreportedReason = ""; heldItems = []; heldRelease = "";
        statementPicker.innerHTML = '<option value="">새 초안 작성</option>';
        message.textContent = "";
        host.querySelector("[data-payroll-versions]").textContent = "";
        amounts.forEach(([name]) => {form.elements[name].value = "";});
        payDateInput.value = "";
        renderItems();
        host.querySelector("[data-payroll-state]").textContent = "직원을 선택하기 전에는 저장·확정·지급·발송할 수 없습니다.";
        host.querySelector("[data-payroll-basis]").textContent = "";
        showLaborRisk();
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
    form.oninput = event => {syncDerived();remember();totals();restoreButtons();if (event?.target && ["gross_pay", "taxable_pay", "non_tax_meal_allowance"].includes(event.target.name)) schedulePreview();};
    const execute = async operation => {
      if (busy || !employee()) return;
      busy = true;restoreButtons();message.textContent = "처리 중입니다.";
      const requestGeneration = generation;
      try {
        if (previewTimer || previewInflight || previewFailed) await settlePreview();  // 대기 중인 계산이 없으면 지연 없이 진행
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
      message.textContent = `저장 완료 · 총지급 ${money(statement.gross_pay)} · 공제 ${money(statement.totals?.total_deductions)} ${statement.held_amount ? ` · 보류 금액(지급 예정, 공제 아님) ${money(statement.held_amount)}` : ""} · 실수령 ${money(statement.net_pay)}${statement.report_status === "unreported" ? " · " + UNREPORTED_WARNING : ""}${(statement.settlement_warnings || []).length ? " · 주의: " + statement.settlement_warnings.join(" ") : ""}`;
      await onSaved?.();
    };
    form.onsubmit = event => {
      event.preventDefault();if (!form.reportValidity()) return;remember();
      execute(async () => {
        if (!itemsComplete()) throw new Error("지급·공제 항목의 금액을 0원 이상 정수로 입력하세요.");
        if (heldError()) throw new Error(heldError());
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
        {const found = result.settlement_items || {};
          const wanted = [["pay", found.pay_items], ["ded", found.deduction_items]].filter(([, rows]) => (rows || []).length);
          if (wanted.some(([kind]) => kind === "pay") && !seedPay()) return;
          if (wanted.some(([kind]) => kind === "ded") && !seedDeductions()) return;
          for (const [kind, rows] of wanted) {const list = kind === "pay" ? pay : ded;
            for (const item of rows) if (!optOut.includes(item.settlement_of_root_id) && !list.some(row => row.settlement_of_root_id === item.settlement_of_root_id)) list.push({...item});}}
        renderItems();syncDerived();
        host.querySelector("[data-payroll-basis]").textContent = [result.basis,result.reason,result.period_note].filter(Boolean).join(" · ");
        remember();totals();return;
      }
      if (action === "settlement") {
        const plan = await api(`/payroll/defaults?${new URLSearchParams({employee_email:email(employee()),employee_request_id:employee().id||"",payroll_month:month})}`);
        if (plan.contract_type !== "freelancer") throw new Error("서명 완료된 프리랜서 용역계약서가 필요합니다.");
        planTaxType = "freelancer_33";
        const feeBasis = plan.fee_basis || "lump";
        const draftHeaders = {"Content-Type":"application/json"};
        const draftBase = {business_id:businessId,employee_email:email(employee()),employee_request_id:employee().id||"",payroll_month:month};
        if (feeBasis === "hourly" || feeBasis === "daily") {
          if (!confirm(`${month} ${employee().name || employee().employee_name} · ${feeBasis === "hourly" ? "시급" : "일급"} 용역 ${money(plan.contract_terms?.wage)} × 승인 근태로 초안을 저장할까요? 금액은 승인된 근태로 계산하며 확정·지급·발송은 별도입니다.`)) return;
          const result = await api("/payroll/draft-from-attendance",{method:"POST",headers:draftHeaders,body:JSON.stringify(draftBase)});
          await saved(result.statement);return;
        }
        if (feeBasis === "monthly") {
          if (!confirm(`${month} ${employee().name || employee().employee_name} · 월정액 용역비 ${money(plan.contract_terms?.wage)}(계약 금액)으로 초안을 저장할까요? 확정·지급·발송은 별도입니다.`)) return;
          const result = await api("/payroll/draft-from-contract",{method:"POST",headers:draftHeaders,body:JSON.stringify(draftBase)});
          await saved(result.statement);return;
        }
        const gross = form.elements.gross_pay.value;
        if (!gross || !Number.isSafeInteger(Number(gross)) || Number(gross) <= 0) {
          host.querySelector("[data-payroll-basis]").textContent = `계약 용역비 ${money(plan.contract_terms?.wage)} · 용역기간 ${plan.contract_terms?.start_date || "확인 필요"} ~ ${plan.contract_terms?.end_date || "별도 합의"}. 총지급액 칸에 이번 지급의 확정금액을 입력한 후 다시 누르세요. 총계약금을 매월 자동 지급하지 않습니다.`;
          throw new Error("이번 지급의 확정 용역비를 총지급액에 입력하세요.");
        }
        if (!confirm(`${month} ${employee().name || employee().employee_name} · 이번 용역 정산금액 ${money(gross)}으로 초안을 저장할까요? 계약 총액·검수·분할 지급 여부를 확인하세요. 확정·지급·발송은 별도입니다.`)) return;
        const result = await api("/payroll/draft-from-contract",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({...draftBase,gross_pay:Number(gross),amount_confirmed:true})});
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
      let payBody = null;
      if (action === "pay") {
        const asked = await askPayout(active);
        if (asked === undefined) return;  // 대화상자에서 취소
        payBody = asked;
      }
      if (!label || (!payBody && !confirm(`${month} ${employee().name || employee().employee_name} · ${label}하시겠습니까?`))) return;
      const result = await api(path+"/"+action,payBody ? {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(payBody)} : {method:"POST"});
      if(result.statement)await saved(result.statement);else message.textContent="요청을 처리했습니다.";
    });});
    selectEmployee();
  }
  window.ObysPayrollAdmin = {mount, payoutDialogHtml, parsePayoutInput, settlementRowHtml, resolvePayrollEmployee, UNREPORTED_WARNING, isHeldLine, heldRowHtml, statementOwnership, ownsStatement, employeeKeyFor, sha256Hex};
  if (typeof module !== "undefined" && module.exports) module.exports = window.ObysPayrollAdmin;
})();
