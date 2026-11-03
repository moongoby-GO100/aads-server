"""Actual JS calculation: service-specific fields update; manual agreements survive."""
import json
import subprocess
from pathlib import Path


def test_freelancer_fee_period_scope_manual_and_no_labor_calculation():
    module=Path(__file__).resolve().parents[2]/'app/static/apps/obys/modules/contract-autofill.js'
    script=r'''
    const assert=require('node:assert/strict');const A=require(process.argv[1]);
    const input={contractType:'freelancer',startDate:'2026-11-01',term:'3m',wage:300000,
      jobDescription:'촬영 및 편집 3편',payPeriod:'prev',payDay:'10',payMethod:'bank'};
    const r=A.compute(input);
    assert.equal(r.applies,true);assert.equal(r.hideWork,true);assert.equal(r.time,undefined);
    assert.equal(r.period.endDate,'2027-01-31');assert.equal(r.fee,300000);
    assert.equal(r.texts.freelancerScope,input.jobDescription);
    assert.match(r.texts.wageComposition,/300,000원/);
    assert.match(r.texts.freelancerSettlementTerms,/2027-01-31/);
    assert.match(r.texts.freelancerSettlementTerms,/10일/);
    assert.equal(r.texts.overtimeTerms,undefined);assert.equal(r.texts.probationPeriod,undefined);
    const p=A.patchFor(r,new Set(['freelancerSettlementTerms','wageComposition']),'3m');
    assert.equal(p.wageComposition,undefined);assert.equal(p.freelancerSettlementTerms,undefined);
    assert.equal(p.freelancerScope,input.jobDescription);
    const changed=A.compute({...input,wage:450000,jobDescription:'편집 5편',term:'6m'});
    assert.match(changed.texts.wageComposition,/450,000원/);assert.equal(changed.texts.freelancerScope,'편집 5편');
    assert.equal(changed.period.endDate,'2027-04-30');
    const missing=A.compute({...input,wage:''});assert.equal(missing.fee,null);assert.equal(missing.texts.wageComposition,undefined);
    assert.ok(A.compute({...input,term:'custom',endDate:'2026-10-01'}).blockers.length);
    const manual=A.manualFieldsOf(r,{freelancerScope:'직접 합의한 별도 산출물',freelancerSettlementTerms:'분할 정산'});
    assert.ok(manual.includes('freelancerScope'));assert.ok(manual.includes('freelancerSettlementTerms'));
    console.log(JSON.stringify({period:r.period.endDate,fee:r.fee,scope:r.texts.freelancerScope}));
    '''
    r=subprocess.run(['node','-e',script,str(module)],capture_output=True,text=True,timeout=20)
    assert r.returncode==0,r.stderr
    assert json.loads(r.stdout)['fee']==300000
