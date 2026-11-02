/* Corrections are requests; only an administrator approval changes attendance. */
(() => {
  'use strict';
  const mounts = new WeakMap();
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const decoded = value => typeof value === 'string' ? JSON.parse(value) : (value || {});
  const day = value => {
    const date = String(value || '').slice(0,10);
    return /^\d{4}-\d{2}-\d{2}$/.test(date) ? `${date} (${'일월화수목금토'[new Date(date+'T12:00:00Z').getUTCDay()]})` : '미기록';
  };
  const summary = value => `${day(value.work_date)} · ${String(value.start_at || '미기록').slice(0,5)}–${String(value.end_at || '미기록').slice(0,5)} · 휴게 ${value.break_minutes ?? '미기록'}분`;
  const labels = {pending:'확인대기',approved:'승인',rejected:'반려',withdrawn:'철회'};

  async function mount(host, {api, businessId, manager = false, onSaved = () => {}}) {
    const identity = {};
    mounts.set(host, identity);
    const current = () => host.isConnected && mounts.get(host) === identity;
    const base = `/${encodeURIComponent(businessId)}/attendance`;
    async function request(path, options) {
      let timer;
      try {
        return await Promise.race([api(path, options), new Promise((_, reject) => {
          timer = setTimeout(() => reject(new Error('응답 대기가 15초를 초과했습니다.')), 15000);
        })]);
      } finally { clearTimeout(timer); }
    }
    let items = [], records = [], busy = false, blocked = true, supersedes = null;
    host.innerHTML = `<h2>${manager ? '정정 신청 관리자 확인' : '내 출퇴근 정정 신청'}</h2>
      <p>신청 자체는 실근태 변경이 아닙니다. 관리자 승인 후 반영됩니다. GPS 권한 없이 신청할 수 있습니다.</p>
      <p>원본 기록 없는 신규 누락은 신청할 수 없습니다. 관리자에게 직원별 상세의 ‘출근 기록 수동 등록’을 요청하세요.</p>
      <button type="button" class="btn" data-refresh>최신 상태 다시 조회</button>
      <a href="/static/apps/obys/index.html#login" data-login hidden>다시 로그인</a>
      <p role="status" aria-live="polite" data-message></p>
      ${manager ? '' : `<form data-create><label>본인 기존 기록 (최근 31건)<select name="record_id" required></select></label>
      ${[['work_date','정정일','date'],['start_at','출근','time'],['end_at','퇴근','time'],['break_minutes','휴게(분)','number']].map(([name,label,type])=>`<label>${label}<input name="${name}" aria-label="${label}" type="${type}" ${type==='number'?'min="0" max="1439" step="1"':''} required></label>`).join('')}
      <label>신청 사유<textarea name="reason" maxlength="1000" required></textarea></label><p data-parent></p><button class="btn" type="submit">정정 신청</button></form>`}
      <div data-list></div>`;
    host.style.overflowWrap = 'anywhere';
    const form = host.querySelector('form'), list = host.querySelector('[data-list]'), message = host.querySelector('[data-message]');
    host.querySelectorAll('input,select,textarea,button').forEach(el => {el.style.minHeight='44px';el.style.maxWidth='100%';el.style.boxSizing='border-box';});
    if(form) {form.style.display='grid';form.style.gap='12px';}
    const controls = () => {
      host.querySelectorAll('form input,form textarea,form select,form button,[data-action]').forEach(el=>{el.disabled=busy || blocked;el.style.minHeight="44px";el.style.maxWidth="100%";el.style.boxSizing="border-box";});
      host.querySelector('[data-refresh]').disabled=busy;
      host.setAttribute('aria-busy', String(busy));
    };
    function failure(error, writing) {
      blocked=true;
      const status = Number(error.status);
      const detail= typeof error.message === 'string' ? error.message : '';
      message.textContent = status===401 ? '세션이 만료되었습니다. 다시 로그인한 후 상태를 조회하세요.'
        : status===403 ? '권한이 없습니다. 계정·사업자 권한을 확인하고 다시 조회하세요.'
        : status===409 ? `원본변경·중복 신청·급여마감 또는 처리 버전 충돌: ${detail} 최신 상태를 조회한 후 확인하세요.`
        : `${writing ? '처리 결과를 확인할 수 없습니다. 저장되었을 수 있으므로 재전송하지 말고 최신 상태를 조회하세요.' : '조회하지 못했습니다. 연결을 확인하고 다시 조회하세요.'} ${detail}`;
      host.querySelector('[data-login]').hidden=![401,403].includes(status);
    }
    function fill(record) {
      if(!form || !record) return;
      for(const key of ['work_date','start_at','end_at','break_minutes']) form.elements[key].value = key.endsWith('_at') ? String(record[key] || '').slice(0,5) : (record[key] ?? '');
    }
    function renderList() {
      list.innerHTML = `<h3>${manager ? '확인대기 목록 · 처리 이력' : '신청 상태 · 처리 사유'}</h3><p>최신 최대 500건${manager?' · 확인대기 우선':''}</p>` + [...items].sort((a,b)=>Number(b.status==='pending')-Number(a.status==='pending')).map(row=>{
        const original=decoded(row.original), requested=decoded(row.requested);
        const actions = row.status==='pending' ? (manager ? [['approve','승인'],['reject','반려']] : [['withdraw','철회']]) : (!manager && ['rejected','withdrawn'].includes(row.status) && !items.some(next=>next.supersedes_id===row.id) ? [['reapply','재신청']] : []);
        return `<article data-id="${esc(row.id)}" style="border:1px solid #ccd3db;border-radius:12px;padding:12px;margin-top:12px"><b>${esc(labels[row.status] || row.status)} · ${esc(row.employee_email)}</b><p>원본: ${esc(summary(original))}</p><p>요청: ${esc(summary(requested))}</p><p>변경 항목: ${esc(['work_date','start_at','end_at','break_minutes'].filter(key=>String(original[key])!==String(requested[key])).map(key=>({work_date:'날짜',start_at:'출근',end_at:'퇴근',break_minutes:'휴게'}[key])).join(', ') || '사유 확인')}</p><p>신청 사유: ${esc(row.reason)}</p><p>처리 사유: ${esc(row.decision_reason || '아직 처리되지 않았습니다')}</p>${actions.some(([action])=>action!=='reapply') ? '<label>처리 사유<textarea data-reason maxlength="1000" aria-label="처리 사유"></textarea></label>' : ''}<div>${actions.map(([action,label])=>`<button type="button" class="btn" data-action="${action}">${label}</button>`).join(' ')}</div></article>`;
      }).join('');
    }
    async function refresh() {
      if(busy || !current()) return;
      busy=true;blocked=true;controls();message.textContent='최신 상태 조회 중입니다.';
      try {
        const result=await request(base+'/corrections');
        if(!Array.isArray(result.items)) throw new Error('목록 응답이 올바르지 않습니다.');
        const clock=manager ? null : await request(base+'/clock/state');
        if(!current()) return;
        items=result.items;
        if(!manager) {
          if(!Array.isArray(clock.recent)) throw new Error('본인 기록 응답이 올바르지 않습니다.');
          records=clock.recent;
          const picker=form.elements.record_id, previous=picker.value;
          picker.innerHTML='<option value="">기록을 선택하세요</option>'+records.map(row=>`<option value="${esc(row.id)}">${esc(summary(row))}</option>`).join('');
          picker.value=records.some(row=>row.id===previous)?previous:'';
        }
        renderList();blocked=false;message.textContent='최신 상태를 확인했습니다. 처리 결과와 사유를 확인하세요.';
      } catch(error) {if(current()) failure(error,false);}
      finally {busy=false;if(current()) controls();}
    }
    async function write(path,payload) {
      if(busy || blocked || !current()) return;
      busy=true;controls();message.textContent='처리 중입니다. 중복 클릭하지 마세요.';
      try {
        const result=await request(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
        if(!result?.id) throw new Error('처리 응답이 유실되었습니다.');
        if(!current()) return;
        supersedes=null;
        if(form) form.querySelector('[data-parent]').textContent='';
        busy=false;await refresh();
        // The correction list is independently refreshed even when parent attendance reload fails.
        if(!blocked) await onSaved();
      } catch(error) {if(current()) failure(error,true);}
      finally {busy=false;if(current()) controls();}
    }
    host.querySelector('[data-refresh]').onclick=refresh;
    if(form) {
      form.elements.record_id.onchange=()=>{supersedes=null;form.querySelector('[data-parent]').textContent='';fill(records.find(row=>row.id===form.elements.record_id.value));};
      form.onsubmit=event=>{
        event.preventDefault();
        if(busy || blocked || !form.reportValidity()) return;
        const payload=Object.fromEntries(new FormData(form));
        const id=payload.record_id;delete payload.record_id;
        if(!records.some(row=>row.id===id) || !payload.reason.trim()) {message.textContent='본인 기록과 신청 사유를 확인하세요.';return;}
        payload.break_minutes=Number(payload.break_minutes);
        if(supersedes) payload.supersedes_id=supersedes;
        write(base+`/records/${encodeURIComponent(id)}/corrections`,payload);
      };
    }
    list.onclick=event=>{
      const button=event.target.closest('[data-action]');
      if(!button || busy || blocked) return;
      const article=button.closest('[data-id]'), row=items.find(item=>item.id===article.dataset.id), action=button.dataset.action;
      if(!row) return;
      if(action==='reapply') {
        if(!records.some(record=>record.id===row.record_id)) {message.textContent='원본이 최근 기록에 없습니다. 관리자에게 확인을 요청하세요.';return;}
        form.elements.record_id.value=row.record_id;fill(decoded(row.requested));form.elements.reason.value=row.reason;
        supersedes=row.id;form.querySelector('[data-parent]').textContent='반려·철회 요청의 새 버전으로 재신청합니다.';form.scrollIntoView({block:'nearest'});return;
      }
      const reason=article.querySelector('[data-reason]').value.trim();
      if(!reason) {message.textContent='처리 사유를 입력하세요.';return;}
      write(base+`/corrections/${encodeURIComponent(row.id)}/${action}`,{version:row.version,reason});
    };
    await refresh();
  }
  window.ObysAttendanceCorrections={mount};
})();
