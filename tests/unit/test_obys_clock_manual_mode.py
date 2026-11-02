"""Execute the employee clock page, including its real event handlers and fetch calls."""
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.skipif(not shutil.which("node"), reason="node required for clock UI execution")
@pytest.mark.parametrize("scenario", ["manual", "lost_response", "stale_state"])
def test_clock_manual_mode_and_recovery(scenario):
    script = r'''
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const html = fs.readFileSync(process.argv[1], 'utf8');
const source = html.match(/<script>([\s\S]*?)<\/script>/)[1];
const scenario = process.argv[2], elements = new Map(), handlers = {}, calls = [];
const el = id => {
  if (!elements.has(id)) elements.set(id, {hidden:true, disabled:false, value:'', textContent:'',
    innerHTML:'', classList:{toggle(){}}, addEventListener(name, fn){this[name] = fn;}});
  return elements.get(id);
};
const storage = new Map([['aads_token','fixture-token']]);
const localStorage = {getItem:k=>storage.get(k)||null, setItem:(k,v)=>storage.set(k,v), removeItem:k=>storage.delete(k)};
let geoReads = 0, opened = false, releasePost, holdState = false, pendingStates = [];
const response = payload => ({ok:true,status:200,json:async()=>payload});
const clock = businessId => ({employee:{name:'시험 직원'},business:{id:businessId,name:businessId},
  server_time:'2026-11-02T12:00:00+09:00',consent:{agreed:true},branches:[],today:[],
  open_record:opened?{status:'pending',check_in_at:'2026-11-02T11:00:00+09:00'}:null});
const fetch = async (path, options) => {
  calls.push({path,options});
  if (path.endsWith('/businesses')) return response({businesses:[{id:'b1',name:'b1'},{id:'b2',name:'b2'}]});
  if (path.endsWith('/clock/state')) {
    const id = path.includes('/b2/')?'b2':'b1';
    if (holdState) return new Promise(resolve=>pendingStates.push(()=>resolve(response(clock(id)))));
    return response(clock(id));
  }
  assert.ok(path.endsWith('/check-in'),path);
  opened = true;
  if (scenario === 'lost_response') throw new Error('response lost after commit');
  await new Promise(resolve=>{releasePost=resolve});
  return response({record:{status:'pending'}});
};
const context = {console,Date,Promise,Error,encodeURIComponent,decodeURIComponent,AbortController,
  setTimeout,clearTimeout,setInterval:()=>1,clearInterval(){},localStorage,fetch,
  document:{cookie:'',getElementById:el},matchMedia:()=>({matches:true}),confirm:()=>true,
  navigator:{onLine:true,userAgent:'fixture',geolocation:{getCurrentPosition(){geoReads++;throw Error('unexpected location request')}}},
  window:{location:{replace(){}},addEventListener:(name,fn)=>{handlers[name]=fn}}};
vm.runInNewContext(source, context);
const flush = async()=>{for(let i=0;i<12;i++)await new Promise(resolve=>setImmediate(resolve));};
(async()=>{
  await flush();
  assert.equal(el('manualClockBtn').hidden,false);
  assert.ok(html.includes('관리자 확인 후 확정'));
  if(scenario==='stale_state'){
    holdState=true;
    el('refreshBtn').click();
    el('businessSelect').change({target:{value:'b2'}});
    await flush();
    assert.equal(pendingStates.length,2);
    pendingStates[1]();await flush();
    assert.equal(el('whoBusiness').textContent,'b2');
    pendingStates[0]();await flush();
    assert.equal(el('whoBusiness').textContent,'b2');
  } else {
    const saving = el('manualClockBtn').click();await flush();
    if(scenario==='manual'){
      assert.equal(el('manualClockBtn').disabled,true);
      assert.equal(el('businessSelect').disabled,true);
      await el('manualClockBtn').click();
      releasePost();
    }
    await saving;await flush();
    const posts=()=>calls.filter(c=>c.options.method==='POST');
    assert.equal(posts().length,1);
    const body=JSON.parse(posts()[0].options.body);
    assert.equal(body.location_consent,false);
    assert.equal('latitude' in body,false);
    assert.equal(geoReads,0);
    assert.equal(el('clockBtn').textContent,'퇴근');
    if(scenario==='lost_response'){
      assert.equal(el('errorView').hidden,false);
      await el('retryBtn').click();await flush();
      handlers.online();await flush();
      assert.equal(posts().length,1,'uncertain write must never automatically retry POST');
    }
  }
})().catch(error=>{console.error(error);process.exitCode=1});
'''
    result = subprocess.run(
        ["node", "-e", script, str(ROOT / "app/static/apps/obys/clock.html"), scenario],
        text=True, capture_output=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
