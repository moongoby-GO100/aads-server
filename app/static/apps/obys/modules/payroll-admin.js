/* Administrator payroll workflow. Existing server APIs own calculations and versions. */
(() => {
  "use strict";
  const drafts = new Map();
  const amounts = [
    ["gross_pay", "총지급액"], ["taxable_pay", "과세 지급액"],
    ["non_tax_meal_allowance", "비과세 식대"], ["tax_withholding", "원천세"],
    ["insurance_deduction", "보험 공제"], ["other_deduction", "기타 공제"]
  ];
  const money = value => value === null || value === undefined || value === "" ? "미집계" : Number(value).toLocaleString("ko-KR") + "원";
  const email = row => String(row.email || row.employee_email || "").trim().toLowerCase();
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
      employees = (people?.employees || []).filter(row => email(row));
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
    let selected = email(employees.find(row => email(row) === options.selectedEmail) || employees[0]);
    let active = null, busy = false, generation = 0;
    const key = () => `${businessId}|${month}|${selected}`;
    const employee = () => employees.find(row => email(row) === selected);
    const ownStatements = () => statements.filter(row => email(row) === selected && row.payroll_month === month);
    host.innerHTML = `<h2>급여 초안 · 명세 관리</h2><p>직원과 월을 선택하고 계약·승인 근태 기준으로 작성합니다. 확정된 명세를 수정하면 기존 명세를 보존한 새 버전으로 저장합니다.</p>
      <div class="toolbar"><label>점포 선택<select class="field" data-payroll-branch><option value="">전체 점포</option>${[...new Set(allEmployees.map(row => row.branch || ""))].filter(Boolean).map(branch => `<option value="${esc(branch)}">${esc(branch)}</option>`).join("")}</select></label><label>직원 선택<select class="field" data-payroll-employee>${employees.map(row => `<option value="${esc(email(row))}">${esc(row.name || row.employee_name || "이름 미등록")} · ${esc(row.branch || "지점 미등록")} · ${esc(email(row).replace(/^(.).*@/, "$1***@"))}</option>`).join("")}</select></label>
      <label>명세 선택<select class="field" data-payroll-statement></select></label></div>
      <p data-payroll-state role="status"></p><p data-payroll-basis></p>
      <form data-payroll-form class="toolbar">${amounts.map(([name, label]) => `<label>${label}<input class="field" name="${name}" type="number" min="0" step="1" required></label>`).join("")}
      <button class="btn primary" type="submit">초안 저장</button></form>
      <p data-payroll-totals aria-live="polite"></p><div class="toolbar">
      <button type="button" class="btn" data-payroll-action="defaults">계약 기준값 불러오기</button>
      <button type="button" class="btn" data-payroll-action="draft">승인 근태로 초안 만들기</button>
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
    const inputValues = () => Object.fromEntries(amounts.map(([name]) => [name, form.elements[name].value]));
    const remember = () => drafts.set(key(), {id:active?.id || "", ...inputValues()});
    const dirty = () => amounts.some(([name]) => form.elements[name].value !== String(active?.[name] ?? ""));
    const totals = () => {
      const values = inputValues();
      const known = amounts.every(([name]) => values[name] !== "" && Number.isSafeInteger(Number(values[name])) && Number(values[name]) >= 0);
      host.querySelector("[data-payroll-totals]").textContent = known
        ? `공제 합계 ${money(Number(values.tax_withholding) + Number(values.insurance_deduction) + Number(values.other_deduction))} · 예상 실수령 ${money(Number(values.gross_pay) - Number(values.tax_withholding) - Number(values.insurance_deduction) - Number(values.other_deduction))} · 저장 후 서버 계산값을 확인하세요.`
        : "미입력 금액을 확인하세요. 확인되지 않은 금액은 0원으로 채우지 않습니다.";
    };
    const buttons = () => host.querySelectorAll("button,select,input");
    const restoreButtons = () => {
      buttons().forEach(button => {button.disabled = busy;});
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
      host.querySelector("[data-payroll-state]").textContent = active
        ? `${month} · ${statusName(active.status)} · 버전 ${active.version || 1}${["confirmed", "paid"].includes(active.status) ? " · 수정 저장 시 새 초안 생성" : ""}`
        : `${month} · 새 초안`;
      host.querySelector("[data-payroll-basis]").textContent = active?.attendance_basis
        ? `승인 근태 ${active.attendance_basis.approved_days}일 · ${active.attendance_basis.approved_minutes}분 기준`
        : active?.source_contract_id ? "서명 계약서 기준값 적용" : "계약·근태 기준값을 조회하거나 금액을 확인해 입력하세요.";
      totals();restoreButtons();
    };
    const selectEmployee = () => {
      generation++;
      picker.value = selected;
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
      picker.innerHTML = employees.map(row => `<option value="${esc(email(row))}">${esc(row.name || row.employee_name || "이름 미등록")}</option>`).join("");
      selected = email(employees[0]);options.onSelect?.(selected);selectEmployee();
    };
    picker.onchange = () => {remember();selected = picker.value;options.onSelect?.(selected);selectEmployee();};
    statementPicker.onchange = () => {remember();generation++;fill(ownStatements().find(row => row.id === statementPicker.value));};
    form.oninput = () => {remember();totals();restoreButtons();};
    const execute = async operation => {
      if (busy) return;
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
        && String(row.business_id || "") === String(businessId) && email(row) === selected
        && row.payroll_month === month);
      if (!persisted) throw new Error("저장 결과를 재조회하지 못했습니다. 입력값을 보존했습니다.");
      statement = persisted;
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
        const person = employee(), payload = {business_id:businessId, employee_email:selected,
          employee_name:person.name || person.employee_name, employee_request_id:person.id || "", payroll_month:month,
          ...(active?.id ? {id:active.id} : {}), ...Object.fromEntries(amounts.map(([name]) => [name, Number(form.elements[name].value)]))};
        const result = await api("/payroll", {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(payload)});
        await saved(result.statement);
      });
    };
    host.querySelectorAll("[data-payroll-action]").forEach(button => {button.onclick = () => execute(async () => {
      const action = button.dataset.payrollAction;
      if (action === "defaults") {
        const result = await api(`/payroll/defaults?${new URLSearchParams({employee_email:selected,payroll_month:month})}`);
        if (!current()) return;
        for (const [name] of amounts) if (result.defaults?.[name] !== undefined) form.elements[name].value = result.defaults[name] ?? "";
        host.querySelector("[data-payroll-basis]").textContent = [result.basis,result.reason,result.period_note].filter(Boolean).join(" · ");
        remember();totals();return;
      }
      if (action === "draft") {
        if (!confirm(`${month} ${employee().name || employee().employee_name} 직원의 승인 근태로 초안을 저장할까요?`)) return;
        const result = await api("/payroll/draft-from-attendance",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({business_id:businessId,employee_email:selected,employee_request_id:employee().id||"",payroll_month:month})});
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
  window.ObysPayrollAdmin = {mount};
})();
