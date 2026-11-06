"""배달식당 운영관리 용역 문구가 작성화면(v41·구 화면)·미리보기·PDF 조항에서 같게 나오는지 검사한다.

2026-11-06 대표님 지적: 업무범위 기본안을 v41 편집기에만 넣으면 구 화면(index.html)과
계약 목적 조항(미리보기·PDF)에는 계속 "독립 용역"이 남는다.
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from app.services.obys_contract_clauses import contract_clauses

ROOT = Path(__file__).resolve().parents[2]
OBYS = ROOT / 'app/static/apps/obys'
JOB = '배달식당 운영관리 용역'


def _purpose(contract):
    return dict(contract_clauses(contract))['계약 목적']


def test_pdf_purpose_clause_names_delivery_service():
    base = {'contract_type': 'freelancer', 'employment_tax_type': 'freelancer_33',
            'employer_name': '열정국밥 중화점', 'employee_name': '홍길동'}
    assert JOB + '을 의뢰' in _purpose({**base, 'job_description': JOB})
    # 다른 업무·빈값은 기존 문구 그대로
    assert '독립 용역을 의뢰' in _purpose({**base, 'job_description': '홍보 콘텐츠 제작'})
    assert '독립 용역을 의뢰' in _purpose(base)


def _preset_block(text):
    m = re.search(r'const DELIVERY_RESTAURANT_BUSINESS_IDS = .*?\n    function deliveryRestaurantDefaults\(.*?\n    \}\n', text, re.S)
    assert m, 'delivery preset block missing'
    return m.group(0)


def test_legacy_screen_uses_same_preset_as_v41():
    core = (OBYS / 'modules/contract-core.js').read_text(encoding='utf-8')
    index = (OBYS / 'index.html').read_text(encoding='utf-8')
    assert _preset_block(index) == _preset_block(core)
    body = index[index.index('function buildContractDefaults()'):]
    body = body[:body.index('function syncFreelancerWageOptions')]
    assert 'isDeliveryRestaurant(currentContractBusiness())' in body
    assert 'deliveryRestaurantDefaults(' in body


def test_preview_purpose_clause_matches_pdf():
    node = shutil.which('node')
    if not node:
        pytest.skip('node unavailable')
    script = r'''
    globalThis.window = globalThis;
    const C = require(process.argv[1] + '/contract-core.js');
    const d = C.defaultsFor('freelancer', 'freelancer_33', {id: 'biz-mia', name: '열정국밥_미아점', branch: '미아점'});
    const base = {contractType: 'freelancer', employmentTaxType: 'freelancer_33', wageType: 'case_fee', employerName: '열정국밥_미아점', employeeName: '홍길동'};
    const html = C.contractPreviewHtml({...base, jobDescription: d.jobDescription, freelancerScope: d.freelancerScope});
    const plain = C.contractPreviewHtml({...base, jobDescription: '홍보 콘텐츠 제작'});
    console.log(JSON.stringify({job: d.jobDescription, html, plain}));
    '''
    out = subprocess.run([node, '-e', script, str(OBYS / 'modules')], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    data = json.loads(out.stdout.strip().splitlines()[-1])
    assert data['job'] == JOB
    assert JOB + '을 의뢰' in data['html']
    assert '배달앱' in data['html']
    assert '독립 용역을 의뢰' in data['plain']
