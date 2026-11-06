/* 근태설정: 직원별 근무 요일(월=0 … 일=6)과 예정 시간대. 계약서 본문에는 쓰지 않고, 프리랜서 근로자성 안내와 미출근 확인에만 쓴다. */
(() => {
  'use strict';
  const mounts = new WeakMap();
  const DAYS = ['월', '화', '수', '목', '금', '토', '일'];
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

  function rowHtml(employee, state) {
    const id = esc(employee.id);
    const pattern = state.draft || state.pattern || {weekdays: [], start_time: '', end_time: ''};
    const days = DAYS.map((label, index) => `<label class="check-choice"><input type="checkbox" name="wp_day_${id}" value="${index}" ${(pattern.weekdays || []).includes(index) ? 'checked' : ''}>${label}</label>`).join('');
    const notice = state.notice ? `<p class="badge warn" role="status" data-wp-risk>${esc(state.notice)}</p>` : '';
    const gaps = state.gaps ? `<p role="status" data-wp-gaps>${state.gaps.missing_days.length ? `${esc(state.gaps.month)} 예정일 중 출근 기록이 없는 날: ${state.gaps.missing_days.map(esc).join(', ')}` : `${esc(state.gaps.month)} 예정일에 출근 기록이 모두 있습니다.`}</p>` : '';
    return `<fieldset data-wp-employee="${id}"><legend>${esc(employee.name || employee.employee_name || '직원')}${employee.branch ? ` · ${esc(employee.branch)}` : ''}</legend>
      <div class="toolbar">${days}</div>
      <div class="toolbar"><label>예정 시작<input class="field" type="time" data-wp-start="${id}" value="${esc(pattern.start_time || '')}"></label>
      <label>예정 종료<input class="field" type="time" data-wp-end="${id}" value="${esc(pattern.end_time || '')}"></label>
      <button class="btn primary" type="button" data-wp-save="${id}">저장</button>
      <button class="btn" type="button" data-wp-gaps="${id}">미출근일 확인</button></div>
      ${notice}${gaps}<p role="status" data-wp-msg="${id}"></p></fieldset>`;
  }

  async function mount(host, {api, businessId, month = ''}) {
    const identity = {};
    mounts.set(host, identity);
    const current = () => host.isConnected && mounts.get(host) === identity;
    host.innerHTML = '<p>근무 요일 설정을 불러오는 중입니다.</p>';
    let employees = [];
    try {
      const response = await api(`/employees/approved?business_id=${encodeURIComponent(businessId)}`);
      employees = (response.employees || []).filter(item => item.id);
    } catch (error) {
      if (current()) host.innerHTML = `<p role="alert">직원 목록을 불러오지 못했습니다: ${esc(error.message)}</p>`;
      return;
    }
    if (!current()) return;
    if (!employees.length) { host.innerHTML = '<p>승인된 직원이 없습니다.</p>'; return; }
    const states = new Map(employees.map(item => [String(item.id), {pattern: null, notice: item.labor_risk_notice || '', gaps: null}]));
    await Promise.all(employees.map(async item => {
      try {
        const loaded = await api(`/employees/approved/${encodeURIComponent(item.id)}/work-pattern`);
        Object.assign(states.get(String(item.id)), {pattern: loaded.work_pattern, notice: loaded.labor_risk_notice || ''});
      } catch (_error) { /* 개별 직원 조회 실패는 빈 설정으로 둔다 */ }
    }));
    if (!current()) return;

    const render = () => {
      host.innerHTML = `<p>정해진 근무 요일은 근태 확인용입니다. 프리랜서에게 요일·시간을 정해 주면 근로자로 판정될 수 있어 안내가 표시됩니다(저장은 막지 않습니다).</p>${employees.map(item => rowHtml(item, states.get(String(item.id)))).join('')}`;
    };
    render();

    const readForm = fieldset => ({
      weekdays: [...fieldset.querySelectorAll('input[type="checkbox"]:checked')].map(box => Number(box.value)),
      start_time: fieldset.querySelector('[data-wp-start]').value,
      end_time: fieldset.querySelector('[data-wp-end]').value
    });
    // 다른 직원을 저장·조회하며 화면을 다시 그려도 편집 중인 입력이 사라지지 않도록 입력 즉시 보관한다.
    const keepDraft = event => {
      const fieldset = event.target.closest?.('[data-wp-employee]');
      const state = fieldset && states.get(fieldset.dataset.wpEmployee);
      if (state) state.draft = readForm(fieldset);
    };
    host.addEventListener('input', keepDraft);
    host.addEventListener('change', keepDraft);

    host.addEventListener('click', async event => {
      const saveId = event.target.closest?.('[data-wp-save]')?.dataset.wpSave;
      const gapId = event.target.closest?.('[data-wp-gaps]')?.dataset.wpGaps;
      const id = saveId || gapId;
      if (!id) return;
      const fieldset = [...host.querySelectorAll('[data-wp-employee]')].find(node => node.dataset.wpEmployee === id);
      const message = [...host.querySelectorAll('[data-wp-msg]')].find(node => node.dataset.wpMsg === id);
      const state = states.get(id);
      event.target.disabled = true;
      try {
        if (saveId) {
          const {weekdays, start_time: start, end_time: end} = readForm(fieldset);
          const saved = await api(`/employees/approved/${encodeURIComponent(id)}/work-pattern`, {
            method: 'PUT', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({weekdays, start_time: start, end_time: end})
          });
          Object.assign(state, {pattern: saved.work_pattern, notice: saved.labor_risk_notice || '', gaps: null, draft: null});
          render();
          const refreshed = [...host.querySelectorAll('[data-wp-msg]')].find(node => node.dataset.wpMsg === id);
          if (refreshed) refreshed.textContent = '근무 요일을 저장했습니다.';
        } else {
          state.gaps = await api(`/employees/approved/${encodeURIComponent(id)}/work-pattern/gaps?payroll_month=${encodeURIComponent(month)}`);
          render();
        }
      } catch (error) {
        if (message) message.textContent = error.message || '처리하지 못했습니다.';
        event.target.disabled = false;
      }
    });
  }

  window.ObysWorkPattern = {mount};
})();
