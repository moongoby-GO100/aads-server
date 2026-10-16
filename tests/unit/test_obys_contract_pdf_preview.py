"""미리보기와 봉인 PDF의 버전별 조항 패리티 및 문서 불변조건."""
import base64
import hashlib
import html
import io
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from app.services.obys_contract_clauses import CONTRACT_TITLES, contract_clauses

ROOT = Path(__file__).resolve().parents[2]
CORE = ROOT / 'app/static/apps/obys/modules/contract-core.js'


def preview_cases():
    node = shutil.which('node')
    if not node:
        pytest.skip('호스트 Node 패리티 검증 필요')
    script = r'''
const C = require(process.argv[1]);
const cases = [];
for (const type of Object.keys(C.contractTypeLabels)) for (const version of ['v3', 'v4']) {
  for (const variant of ['empty', 'standard', 'foreign-minor', 'custom']) {
    const c = variant === 'empty' ? {} : {
      ...C.defaultsFor(type, type === 'freelancer' ? 'freelancer_33' : 'four_insurance'),
      ...C.classificationFor(type), employeeName:'가상 <직원>', employerName:'가상 & 상호',
      startDate:'2026-10-16', endDate:['fixed_term','daily'].includes(type) ? '2026-10-17' : '',
      branch:'가상점', wage:1234567.5, workplaceSizeCategory:variant === 'standard' ? 'five_plus' : 'under_5',
      foreignWorker:variant === 'foreign-minor', visaStatus:'F-2', foreignRegistrationNoMasked:'000000-*******',
      minorGuardianName:variant === 'foreign-minor' ? '가상 보호자' : '', minorGuardianPhone:'010-0000-0000',
      ...(variant === 'custom' ? {overtimeTerms:'별도 약정\n보존',leaveTerms:'약정 휴가'} : {})
    };
    c.contractType=type; c.templateVersion=`test-${version}`;
    const snake = Object.fromEntries(Object.entries(c).map(([k,v])=>[k.replace(/[A-Z]/g,s=>'_'+s.toLowerCase()),v]));
    cases.push({key:`${type}/${version}/${variant}`,contract:snake,html:C.contractPreviewHtml(snake)});
  }
}
console.log(JSON.stringify(cases));
'''
    out = subprocess.run([node, '-e', script, str(CORE)], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_node_python_clauses_match_all_types_and_versions():
    cases = preview_cases()
    assert len(cases) == 56
    for case in cases:
        matches = re.findall(r'<li><b>(.*?)</b><br>(.*?)</li>', case['html'], re.S)
        expected = [(html.unescape(t), html.unescape(b.replace('<br>', '\n'))) for t, b in matches]
        assert contract_clauses(case['contract']) == expected, case['contract']


def signed_fixture(snapshot):
    from PIL import Image, ImageDraw
    from app.services.yeoljeong_contract_pdf import snapshot_sha256
    stream = io.BytesIO()
    image = Image.new('RGB', (240, 70), 'white')
    ImageDraw.Draw(image).line([(10, 50), (60, 10), (100, 45), (200, 20)], fill='black', width=3)
    image.save(stream, format='PNG')
    raw = stream.getvalue()
    sealed = {
        **snapshot, 'id':'12345678-test', 'employee_email':'fiction@example.invalid',
        'employee_phone':'010-9876-5432', 'signed_at':'2026-10-16T06:00:00Z',
        'signature_sha256':hashlib.sha256(raw).hexdigest(),
        'signature_consent':{'accepted':True,'version':'test-v1'},
        'signature_audit':{'authenticated_email':'fiction@example.invalid'},
        'terms':'가상 추가 특약',
    }
    return {'signed_snapshot':sealed, 'signed_snapshot_sha256':snapshot_sha256(sealed),
            'signature_data_uri':'data:image/png;base64,' + base64.b64encode(raw).decode()}


@pytest.mark.parametrize('version', ['v3', 'v4'])
@pytest.mark.parametrize('kind', CONTRACT_TITLES)
def test_pdf_sealed_deterministic_masked_numbered_and_evidence_last(kind, version):
    import fitz
    from app.services.yeoljeong_contract_pdf import ContractPdfError, render_signed_contract_pdf
    contract = signed_fixture({'contract_type':kind,'template_version':'test-' + version,'wage':1234567,
                               'employee_name':'가상직원','employer_name':'가상상호',
                               'base_salary':1000000,'contract_date':'2026-10-16'})
    before = json.dumps(contract, sort_keys=True)
    data = render_signed_contract_pdf(contract)
    assert data == render_signed_contract_pdf(contract)
    assert json.dumps(contract, sort_keys=True) == before
    with fitz.open(stream=data, filetype='pdf') as pdf:
        texts = [p.get_text() for p in pdf]
        text = '\n'.join(texts)
        assert CONTRACT_TITLES[kind] in text
        assert '사용자 서명' in text
        assert '2026년 10월 16일 15:00:00 KST' in text
        assert 'fiction@example.invalid' not in text and '010-9876-5432' not in text
        assert contract['signed_snapshot_sha256'] not in text
        assert contract['signed_snapshot_sha256'][:12] in texts[-1]
        assert all('전자서명 확인' not in t for t in texts[:-1])
        for number, page_text in enumerate(texts, 1):
            assert f'{number} / {len(texts)} 쪽' in page_text
        assert '가상 추가 특약' in text
        if kind not in ('freelancer', 'confidentiality'):
            assert ('퇴직급여' in text) == (version == 'v4')
    contract['signed_snapshot']['wage'] = 1
    with pytest.raises(ContractPdfError, match='스냅샷 해시'):
        render_signed_contract_pdf(contract)


def test_pdf_rejects_signature_tampering():
    from app.services.yeoljeong_contract_pdf import ContractPdfError, render_signed_contract_pdf
    contract = signed_fixture({'contract_type':'regular'})
    contract['signature_data_uri'] = 'data:image/png;base64,' + base64.b64encode(b'altered').decode()
    with pytest.raises(ContractPdfError, match='서명 해시'):
        render_signed_contract_pdf(contract)


def test_daily_v4_insurance_default_empty_custom_and_v3():
    node = shutil.which('node')
    if not node:
        pytest.skip('호스트 Node 필요')
    script = r'''const C=require(process.argv[1]);
const result=[];
for(const version of ['v3','v4']) for(const input of [undefined,'','직접 약정']) {
const c={...C.defaultsFor('daily','four_insurance'),contractType:'daily',templateVersion:'test-'+version};
if(input!==undefined)c.insuranceTerms=input;
result.push({contract:c,html:C.contractPreviewHtml(c)});
}
console.log(JSON.stringify(result));'''
    cases = json.loads(subprocess.check_output([node, '-e', script, str(CORE)], text=True))
    default = '고용·산재보험 일용근로 신고 및 근로내용 확인신고를 법정 기준에 따라 처리한다.'
    for case in cases:
        expected = contract_clauses(case['contract'])
        body = dict(expected)['4대보험 및 세무']
        match = re.search(r'<li><b>4대보험 및 세무</b><br>(.*?)</li>',case['html'])
        assert html.unescape(match[1]) == body
        if case['contract']['templateVersion'] == 'test-v4':
            assert body == (case['contract'].get('insuranceTerms') or default)
            assert not body.startswith(' ')
            assert body.count('고용·산재보험') == (0 if body == '직접 약정' else 1)


@pytest.mark.parametrize('version', ['v3', 'v4'])
@pytest.mark.parametrize('old,new', [('LEGACY_MARKER', ''), ('', 'NEW_MARKER'),
                                    ('SAME_MARKER', 'SAME_MARKER'),
                                    ('LEGACY_MARKER', 'NEW_MARKER'),
                                    ('LEGACY_MARKER', '수습기간 없음.'),
                                    ('LEGACY_MARKER', '수습기간 없음. 수습을 정한 경우에도 입력 임금을 동일 적용한다.')])
@pytest.mark.parametrize('kind', ['regular', 'freelancer', 'confidentiality'])
def test_legacy_sealed_agreements_preserved_in_pdf_and_preview(version, old, new, kind):
    from app.services.yeoljeong_contract_pdf import render_signed_contract_pdf, snapshot_sha256
    import fitz
    snapshot = {'contract_type':kind, 'template_version':'test-' + version,
                'probation_terms':old, 'probation_period':new, 'special_terms':old,
                'employee_name':'가상직원', 'employer_name':'가상상호',
                'overtime_terms':'CUSTOM_OVERTIME', 'leave_terms':'CUSTOM_LEAVE',
                '식사약정':'SEALED_EXTRA_MARKER'}
    contract = signed_fixture(snapshot)
    contract['signed_snapshot']['terms'] = new if not new.startswith('수습기간 없음.') else ''
    contract['signed_snapshot_sha256'] = snapshot_sha256(contract['signed_snapshot'])
    frozen = json.dumps(contract, sort_keys=True)
    data = render_signed_contract_pdf(contract)
    assert data == render_signed_contract_pdf(contract)
    assert json.dumps(contract, sort_keys=True) == frozen
    assert snapshot_sha256(contract['signed_snapshot']) == contract['signed_snapshot_sha256']
    with fitz.open(stream=data, filetype='pdf') as pdf:
        text = '\n'.join(page.get_text() for page in pdf)
    for marker in filter(None, (old,new if not new.startswith('수습기간 없음.') else '', 'CUSTOM_OVERTIME','CUSTOM_LEAVE','SEALED_EXTRA_MARKER')):
        assert marker in text
    assert '수습기간 없음' not in text
    if old == new:
        assert text.count(old) == 2  # once in probation, once in special agreements
    node = shutil.which('node')
    assert node, 'Node required for sealed legacy preview regression'
    preview = subprocess.check_output([node, '-e',
        'const C=require(process.argv[1]); console.log(C.contractPreviewHtml(JSON.parse(process.argv[2])))',
        str(CORE),json.dumps(contract['signed_snapshot'])],text=True,timeout=15)
    for marker in filter(None, (old,new if not new.startswith('수습기간 없음.') else '')):
        assert marker in preview
    assert '수습기간 없음' not in preview


@pytest.mark.parametrize('version', ['v3', 'v4'])
@pytest.mark.parametrize('kind', ['regular', 'freelancer'])
def test_legacy_identity_and_independent_numeric_fields_survive(kind, version):
    from app.services.yeoljeong_contract_pdf import render_signed_contract_pdf, snapshot_sha256
    import fitz
    contract = signed_fixture({
        'contract_type': kind, 'template_version': 'test-' + version,
        'visa_status': 'F-2', 'foreign_registration_no_masked': '123456-*******',
        'weekly_hours': 40, 'wage': 400000,
    })  # deliberately no foreign_worker flag in this legacy seal
    frozen = json.dumps(contract, sort_keys=True)
    data = render_signed_contract_pdf(contract)
    assert data == render_signed_contract_pdf(contract)
    assert json.dumps(contract, sort_keys=True) == frozen
    assert snapshot_sha256(contract['signed_snapshot']) == contract['signed_snapshot_sha256']
    with fitz.open(stream=data, filetype='pdf') as pdf:
        text = '\n'.join(page.get_text() for page in pdf)
    assert '체류자격' in text and 'F-2' in text
    assert '외국인등록번호' in text and '123456-*******' in text
    label = '수행 회차 기준' if kind == 'freelancer' else '주 소정근로시간'
    assert re.search(re.escape(label) + r'\s+40(?:\s|$)', text)


@pytest.mark.parametrize('kind', ['regular', 'part_time', 'fixed_term', 'daily', 'freelancer'])
@pytest.mark.parametrize('version', ['v3', 'v4'])
def test_body_field_coverage_removes_only_actual_consumers(kind, version):
    from app.services.yeoljeong_contract_pdf import _body_covered_fields, render_signed_contract_pdf
    import fitz
    case = next(c for c in preview_cases() if c['key'] == f'{kind}/{version}/standard')
    snapshot = dict(case['contract'], wage=400000, weekly_hours=40,
                    bank_name='가상검증은행', bank_account_holder='가상예금주',
                    pay_method='가상지급방식', freelancer_settlement_terms='가상검수정산')
    covered = _body_covered_fields(snapshot)
    assert {'wage', 'pay_method', 'bank_name', 'start_date'} <= covered
    assert ('weekly_hours' in covered) == (kind != 'freelancer')
    contract = signed_fixture(snapshot)
    original = json.dumps(contract, sort_keys=True)
    data = render_signed_contract_pdf(contract)
    with fitz.open(stream=data, filetype='pdf') as pdf:
        text = ''.join(page.get_text() for page in pdf)
    assert text.count('가상지급방식') == 1
    assert text.count('가상검증은행') == 1
    if kind == 'freelancer':
        assert '수행 회차 기준' in text
        assert text.count('가상검수정산') == 1
    assert json.dumps(contract, sort_keys=True) == original
    assert render_signed_contract_pdf(contract) == data


@pytest.mark.parametrize('version', ['v3', 'v4'])
def test_different_legacy_conditions_remain_in_pdf(version):
    from app.services.yeoljeong_contract_pdf import render_signed_contract_pdf
    import fitz
    snapshot = dict(contract_type='regular', template_version=f'test-{version}',
                    work_time='봉인구시간', workTime='본문신시간',
                    wage='400000.25', overtime_terms='상시 5인 미만 별도 봉인수당',
                    workplace_size_category='five_plus', insurance_terms='봉인별도보험')
    with fitz.open(stream=render_signed_contract_pdf(signed_fixture(snapshot)), filetype='pdf') as pdf:
        text = ''.join(page.get_text() for page in pdf)
    assert '봉인구시간' in text and '본문신시간' in text
    assert '400000.25' in text
    assert '상시 5인 미만 별도 봉인수당' in text
    assert text.count('봉인별도보험') == 1


@pytest.mark.parametrize('wage', ['400000.0', '400000', 400000])
def test_numeric_coverage_uses_complete_money_representation(wage):
    from app.services.yeoljeong_contract_pdf import _body_covered_fields
    assert 'wage' in _body_covered_fields({'contract_type': 'regular', 'wage': wage})
    assert 'wage' not in _body_covered_fields({'contract_type': 'regular', 'wage': '400000.25'})


def test_coverage_follows_actual_renderer_and_preserves_unconsumed_fields(monkeypatch):
    from app.services import yeoljeong_contract_pdf as pdf
    snapshot = {'contract_type': 'regular', 'work_time': '동일한 조건',
                'rest_time': '동일한 조건', 'weekly_hours': 40}
    original = pdf.contract_clauses

    def changed_renderer(contract):
        # A template update stops consuming schedule fields, while another field
        # still contains exactly the same text. It must not prove coverage.
        return [('새 조항 제목', str(contract.get('rest_time', '')))]

    monkeypatch.setattr(pdf, 'contract_clauses', changed_renderer)
    covered = pdf._body_covered_fields(snapshot)
    assert 'work_time' not in covered
    assert 'weekly_hours' not in covered
    assert 'rest_time' in covered
    monkeypatch.setattr(pdf, 'contract_clauses', original)


@pytest.mark.parametrize('version', ['v3', 'v4'])
def test_leave_coverage_matches_actual_preview_title(version):
    from app.services.yeoljeong_contract_pdf import _body_covered_fields
    snapshot = {'contract_type': 'regular', 'template_version': 'test-' + version,
                'leave_terms': '봉인된 독립 휴가 약정'}
    assert 'leave_terms' in _body_covered_fields(snapshot)
    clauses = dict(contract_clauses(snapshot))
    assert clauses['휴가, 결근, 근태'] == snapshot['leave_terms']


def _assert_real_bold_font(data):
    from fontTools.ttLib import TTFont
    with TTFont(io.BytesIO(data)) as font:
        assert font['OS/2'].usWeightClass == 700
        assert 'Bold' in font['name'].getDebugName(6)


def test_bold_validator_rejects_regular_font():
    from app.services.yeoljeong_contract_pdf import BUNDLED_FONT
    with pytest.raises(AssertionError):
        _assert_real_bold_font(BUNDLED_FONT.read_bytes())


@pytest.mark.parametrize('kind', ['regular', 'part_time', 'fixed_term', 'daily', 'freelancer'])
def test_real_bold_family_and_embedded_pdf_usage(kind):
    import fitz
    from reportlab.pdfbase import pdfmetrics
    from reportlab.lib.fonts import tt2ps
    from app.services import yeoljeong_contract_pdf as renderer
    bold = renderer.BUNDLED_FONT.with_name('NanumGothic-Bold.ttf').read_bytes()
    assert hashlib.sha256(bold).hexdigest() == 'f96298f9fb18e364d2370f4c3ce948ac67a2b61af992d7234bc15c42b033c674'
    _assert_real_bold_font(bold)
    normal = renderer._register_font()
    assert tt2ps(normal, 1, 0) == normal + 'Bold'
    assert pdfmetrics.getFont(normal + 'Bold').face.name == b'NanumGothicBold'
    data = renderer.render_signed_contract_pdf(signed_fixture({'contract_type': kind, 'wage': 400000}))
    with fitz.open(stream=data, filetype='pdf') as pdf:
        used = {span['font'] for page in pdf for block in page.get_text('dict')['blocks']
                if 'lines' in block for line in block['lines'] for span in line['spans']
                if 'Bold' in span['font']}
        assert used, 'PDF contains no actual Bold text spans'
        embedded = set()
        for page in pdf:
            for entry in page.get_fonts(full=True):
                if 'Bold' in entry[3]:
                    name, ext, _, content = pdf.extract_font(entry[0])
                    assert ext == 'ttf' and content
                    _assert_real_bold_font(content)
                    embedded.add(name.split('+')[-1])
        assert used <= embedded
