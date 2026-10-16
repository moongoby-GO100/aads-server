"""계약 7종 v4 조항과 v3 원문 보존 회귀 검증."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CORE = ROOT / 'app/static/apps/obys/modules/contract-core.js'


def test_seven_types_v4_clauses_and_v3_compatibility():
    node = shutil.which('node')
    if not node:
        pytest.skip('호스트 node 검증 필요')
    script = r'''
const assert = require('assert');
require(process.argv[1]);
const C = globalThis.ObysContractCore;
assert.equal(Object.keys(C.contractTypeLabels).length, 7);
const regular = C.defaultsFor('regular', 'four_insurance');
for (const wrong of ['52시간 30분', '21:00-09:00', '3,000,000']) assert(!JSON.stringify(regular).includes(wrong));
assert.equal(C.defaultsFor('regular', 'four_insurance', {workplace_size_category:'five_plus'}).workplaceSizeCategory, 'five_plus');
const manager = C.defaultsFor('manager', 'four_insurance');
assert.equal(manager.workTime, ''); assert.equal(manager.weeklyHours, '');
const results = {};
for (const type of Object.keys(C.contractTypeLabels)) {
  const d = {...C.defaultsFor(type, type === 'freelancer' ? 'freelancer_33' : 'four_insurance'),
    ...C.classificationFor(type), contractType:type, employeeRequestId:'req', employeeName:'가상직원',
    employeeEmail:'test@example.com', employeePhone:'010-0000-0000', employeeBirthDate:'1990-01-01', employeeAddress:'서울',
    businessId:'biz', branch:'시험점', contractDate:'2026-10-16', startDate:'2026-11-01',
    endDate:['fixed_term','daily'].includes(type) ? '2026-11-02' : '', employerName:'시험상호', employerRegistrationNo:'123-45-67890',
    employerRepresentative:'대표', employerAddress:'서울', wage:120000, workplaceSizeCategory:'five_plus',
    wageComposition:'입력 임금에 따른 자동 계산 문구', workTime:type==='freelancer' ? '독립 수행' : '09:00-18:00',
    weeklyHours:type==='freelancer' ? '해당 없음' : '주 40시간', dailyWorkSchedule:'월~금 09:00-18:00 휴게 12:00-13:00'};
  const html = C.contractPreviewHtml(d);
  assert(html.includes(C.contractTypeLabels[type]));
  if (C.EMPLOYMENT_TYPES.includes(type)) {
    const headings = ['근로계약 기간','근무장소 및 업무','소정근로시간 및 휴게','근무일, 휴일, 주휴','임금, 계산방법, 지급방법','연장·야간·휴일근로','휴가, 결근, 근태','4대보험 및 세무','퇴직급여','수습 및 평가','식대, 유니폼, 보건증','퇴직 및 계약 해지','개인정보, 보안, 위생','계약서 교부 및 전자서명'];
    let pos=-1;
    for(const h of headings) {const next=html.indexOf('<b>'+h+'</b>'); assert(next>pos,h);pos=next;}
    assert(html.includes('50%')); assert(html.includes('제60조'));
    assert(C.contractPreviewHtml({...d,workplaceSizeCategory:'under_5'}).includes('가산 규정은 미적용'));
    assert(!html.includes('기성 정산'));
  }
  if(type==='regular') {
    assert.equal((html.match(/기간의 정함이 없는 근로계약으로 한다/g)||[]).length,1);
    const v3=C.contractPreviewHtml({...d,template_version:'legacy-v3'});
    assert(!v3.includes('<b>퇴직급여</b>'));
    assert(v3.includes('기간의 정함 없음까지로 한다'));
  }
  if(type==='part_time') assert.equal(html.split('근로일별 근로시간:').length-1,1);
  if(type==='manager') assert(html.includes('직함만으로'));
  if(type==='fixed_term') {assert(html.includes('2년')); assert(!C.checkDraft({...d,endDate:''}).ok);}
  if(type==='daily') {assert(html.includes('고용·산재보험'));assert(!C.checkDraft({...d,wageType:'monthly'}).ok);assert(!C.checkDraft({...d,endDate:''}).ok);}
  if(type==='freelancer') {assert(html.includes('근로자성 징표'));assert(html.includes('기성 정산'));}
  results[type] = true;
}
console.log(JSON.stringify(results));
'''
    out = subprocess.run([node, '-e', script, str(CORE)], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    assert len(json.loads(out.stdout)) == 7


@pytest.mark.parametrize('kind,title', [('fixed_term', '기간제 근로계약서'), ('daily', '일용 근로계약서')])
def test_backend_new_types_require_end_date_and_daily_wage(kind, title):
    from fastapi import HTTPException
    from app.services import yeoljeong_finance_service as svc
    assert kind in svc.EMPLOYMENT_CONTRACT_TYPES
    assert svc.CONTRACT_TEMPLATE_META[kind]['print_title'] == title
    assert svc.CONTRACT_TEMPLATE_META[kind]['template_version'].endswith('-v4')
    with pytest.raises(HTTPException) as exc:
        svc._validate_contract_payload({'contract_type':kind, 'employment_tax_type':'four_insurance', 'wage_type':'daily' if kind=='daily' else 'monthly'})
    assert '계약 종료일' in str(exc.value.detail)


@pytest.mark.parametrize('kind', ['fixed_term', 'daily'])
def test_backend_accepts_complete_new_contract_and_rejects_invalid_date(kind):
    from fastapi import HTTPException
    from app.services import yeoljeong_finance_service as svc
    payload = dict(
        contract_type=kind, employment_tax_type='four_insurance', wage_type='daily' if kind == 'daily' else 'monthly',
        wage=3000000, employee_request_id='req', business_id='biz', branch='시험점',
        employee_name='가상직원', employee_email='test@example.com', employee_phone='010-0000-0000',
        employee_birth_date='1990-01-01', employee_address='서울', employer_name='가상상호',
        employer_registration_no='123-45-67890', employer_representative='대표', employer_address='서울',
        contract_date='2026-10-16', start_date='2026-11-01', end_date='2026-11-02', workplace='시험점',
        job_description='매장 운영', work_time='09:00-18:00', rest_time='12:00-13:00', weekly_hours='주 40시간',
        work_days='월~금', holidays='일요일', pay_date='매월 5일', pay_method='계좌이체',
        wage_composition='입력 임금 기준', overtime_terms='법정 가산', leave_terms='법정 휴가', insurance_terms='법정 신고',
    )
    assert svc._validate_contract_payload(payload)['contract_type'] == kind
    with pytest.raises(HTTPException) as exc:
        svc._validate_contract_payload({**payload, 'end_date':'invalid'})
    assert '종료일 형식' in str(exc.value.detail)
    if kind == 'daily':
        with pytest.raises(HTTPException) as exc:
            svc._validate_contract_payload({**payload, 'wage_type':'monthly'})
        assert '일급' in str(exc.value.detail)


# 기준 HTML은 a7c36907 패치를 적용한 뒤 리팩터링 전에 생성했다.
# 압축한 출력만 보관하여 이전 렌더러 소스의 중복 복사를 피한다.
PREVIEW_REGRESSION_SCRIPT = r'''
const assert = require('node:assert/strict');
const fs = require('node:fs');
const C = require(process.argv[1]);
const outputs = {};
for (const type of Object.keys(C.contractTypeLabels)) {
  for (const version of ['v3', 'v4']) {
    for (const variant of ['empty', 'standard', 'signed', 'snake']) {
      const draft = variant === 'empty' ? {} : {
        ...C.defaultsFor(type, type === 'freelancer' ? 'freelancer_33' : 'four_insurance'),
        ...C.classificationFor(type),
        employeeName: '가상 <직원>', employerName: '시험 & 상호',
        branch: '시험점', businessId: 'test-business', contractDate: '2026-10-16',
        startDate: '2026-11-01', endDate: variant === 'standard' ? '' : '2026-11-02',
        wage: 2500000, baseSalary: 2200000, nonTaxMealAllowance: 200000, taxableAllowance: 100000,
        mealProvision: 'cash_no_meal', workTime: '09:00-18:00', restTime: '12:00-13:00',
        weeklyHours: '주 40시간', dailyWorkSchedule: '월~금 09:00-18:00\n휴게 12:00-13:00',
        workplaceSizeCategory: variant === 'standard' ? 'five_plus' : 'under_5',
        foreignWorker: variant !== 'standard', visaStatus: 'F-2', foreignRegistrationNoMasked: '000000-*******',
        minorGuardianName: variant === 'standard' ? '' : '가상 보호자', minorGuardianPhone: '010-0000-0000',
        signatureDataUri: variant === 'standard' ? '' : 'data:image/png;base64,dGVzdA==',
        signedAt: '2026-10-16T10:00:00', signerName: '가상 서명자',
        bankName: '시험은행', bankAccountNumber: '000-000', bankAccountHolder: '가상직원'
      };
      draft.contractType = type;
      draft.templateVersion = `regression-${version}`;
      const contract = variant === 'snake'
        ? Object.fromEntries(Object.entries(draft).map(([key, value]) => [key.replace(/[A-Z]/g, c => '_'+c.toLowerCase()), value]))
        : draft;
      outputs[`${type}/${version}/${variant}`] = C.contractPreviewHtml(contract);
    }
  }
}
if (process.argv[2] === 'capture') {
  process.stdout.write(JSON.stringify(outputs));
} else {
  const before = JSON.parse(fs.readFileSync(0, 'utf8'));
  assert.deepEqual(Object.keys(outputs), Object.keys(before));
  for (const [name, html] of Object.entries(outputs)) {
    assert(Buffer.from(html, 'utf8').equals(Buffer.from(before[name], 'utf8')), name);
  }
  console.log(Object.keys(outputs).length);
}
'''


def test_preview_html_bytes_match_before_refactor():
    import gzip

    node = shutil.which('node')
    if not node:
        pytest.skip('호스트 node 검증 필요')
    baseline = Path(__file__).with_name('fixtures') / 'obys_contract_preview_before_r2.json.gz'
    # R2: daily v4만 중복 문장 제거 및 빈 보험값 기본 문구 적용으로 갱신했다.
    # 나머지 52개 미리보기는 이전 fixture 바이트를 그대로 보존한다.
    with gzip.open(baseline, 'rt', encoding='utf-8') as source:
        before = source.read()
    out = subprocess.run(
        [node, '-e', PREVIEW_REGRESSION_SCRIPT, str(CORE)], input=before,
        capture_output=True, text=True, timeout=30,
    )
    assert out.returncode == 0, out.stderr
    assert int(out.stdout) == 56
