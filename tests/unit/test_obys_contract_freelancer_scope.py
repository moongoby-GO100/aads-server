"""배달식당 용역 기본안과 서명 계약 보존 회귀."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

MODULES = Path(__file__).resolve().parents[2] / 'app/static/apps/obys/modules'


def test_restaurant_scope_defaults_and_editor_preservation():
    node = shutil.which('node')
    if not node:
        pytest.skip('node unavailable')
    script = r'''
    globalThis.window = globalThis;
    globalThis.location = {href:'https://example.test/', origin:'https://example.test',search:''};
    const C = require(process.argv[1] + '/contract-core.js');
    require(process.argv[1] + '/contract-autofill.js');
    require(process.argv[1] + '/contract-editor-v41.js');
    const T = window.obysContractV41._test;
    const defaults = C.DELIVERY_RESTAURANT_BUSINESS_IDS.map(id => C.defaultsFor('freelancer','freelancer_33',{id,name:'사업자',branch:'시험점'}));
    const ordinary = C.defaultsFor('freelancer','freelancer_33',{id:'other'});
    const industry = C.defaultsFor('freelancer','freelancer_33',{industry:'음식점업',name:'업종점'});
    const saved = {status:'signed',contract_type:'freelancer',freelancer_scope:'서명 원문',job_description:'원래 업무',snapshot_html:'원본 스냅샷'};
    const before = JSON.stringify(saved);
    const values = T.valuesFromContract(saved);
    const state = T.openState({contract:saved,values});
    T.initAuto('saved'); T.autoRun();
    const signed = {unchanged:before === JSON.stringify(saved),scope:state.values.freelancerScope,editable:T.scopePresetEditable(),html:T.fieldControl({name:'freelancerScope',type:'textarea',label:'범위'})};
    const draftState = T.openState({contract:{status:'draft'},values:{contractType:'freelancer',freelancerScope:'합의한 기존 초안',jobDescription:'합의한 업무'}});
    T.initAuto('saved'); T.autoRun();
    const draft = {scope:draftState.values.freelancerScope,editable:T.scopePresetEditable()};
    const state2 = T.openState({target:{business_id:'biz-junghwa',branch:'중화점'},values:{contractType:'part_time',employmentTaxType:'four_insurance'}});
    T.initAuto('new');
    T.switchContractType('freelancer','part_time','four_insurance');
    T.autoRun();
    console.log(JSON.stringify({defaults,ordinary,industry,signed,draft,newValues:state2.values,editable:T.scopePresetEditable()}));
    '''
    result = subprocess.run([node, '-e', script, str(MODULES)], text=True, capture_output=True, check=True)
    out = json.loads(result.stdout)
    editor = (MODULES / 'contract-editor-v41.js').read_text()
    core = (MODULES / 'contract-core.js').read_text()
    editor_re = re.search(r'const FIXED_WORK_RE = /(.*?)/;', editor).group(1)
    core_re = re.search(r'if \(/(.*?)/\.test\(fixedWorkSignals\)', core).group(1)
    for row in [*out['defaults'], out['industry'], out['newValues']]:
        assert row['jobDescription'] == '배달식당 운영관리 용역'
        scope = row['freelancerScope']
        for text in ['①', '②', '③', '④', '⑤', '⑥', '월간 운영보고서', '다음 달 5일', '보조인력', '독립적으로', '결과물 기준']:
            assert text in scope
        assert not re.search(editor_re.replace(r'\/', '/'), scope)
        assert not re.search(core_re.replace(r'\/', '/'), scope)
    assert '사업자' in out['defaults'][0]['freelancerScope']
    assert '시험점' in out['defaults'][0]['freelancerScope']
    assert '중화점' in out['newValues']['freelancerScope']
    assert out['newValues']['wageType'] == 'case_fee'
    assert out['ordinary']['jobDescription'] == '독립 용역 업무'
    assert '산출물을 제작하여' in out['ordinary']['freelancerScope']
    assert out['signed']['unchanged'] and out['signed']['scope'] == '서명 원문'
    assert not out['signed']['editable']
    assert 'data-cv41-restaurant-scope' not in out['signed']['html']
    assert out['draft'] == {'scope': '합의한 기존 초안', 'editable': True}
    assert out['editable']
