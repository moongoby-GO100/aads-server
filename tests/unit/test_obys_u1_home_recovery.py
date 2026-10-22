"""U1: 레거시 오늘 홈 5지표가 실패·미로그인·렌더 예외에서도 '불러오는 중'으로 멈추지 않는지 실제 함수를 실행해 확인한다."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
HTML = (ROOT / "app/static/apps/obys/index.html").read_text(encoding="utf-8")


def _block(start: str, end: str) -> str:
    head = HTML.index(start)
    return HTML[head:HTML.index(end, head)]


def test_home_load_has_recovery_paths():
    load = _block("async function loadTodayHome(", "function bindTodayHome(")
    assert ".catch(error => {" in load and "failUnsettledHomeCards(" in load
    assert load.index("if (isEmployeeExperience()) return;") < load.index("if (!hasServerAuth())")
    assert "homeSettledCards.clear()" in load
    for key in ("sales", "bank", "review", "payroll"):
        assert f'failHomeCard("{key}", homeErrorReason(' in load
    assert '/static/apps/obys/admin.html' in _block("function homeCardActions(", "function failHomeCard(")


def test_init_loads_today_home_when_no_saved_view():
    init = _block("async function init()", "init();")
    assert 'const restoredSavedView = restoreUiState();' in init
    assert '!restoredSavedView && currentView === "dashboard" && hasServerAuth()' in init
    assert "loadTodayHome()" in init


@pytest.mark.skipif(not shutil.which("node"), reason="node unavailable")
def test_home_cards_recover_instead_of_staying_loading():
    source = _block("const HOME_CARD_KEYS", "function sumRecords(")
    code = r"""const assert=require('node:assert/strict');
const states={};const cards={};
for(const k of ['sales','bank','review','payroll','approval']){
  cards[k]={value:{textContent:'—'},hint:{textContent:'불러오는 중'},slot:{innerHTML:''},
    querySelector(sel){return sel==='[data-kpi-value]'?this.value:sel==='[data-kpi-hint]'?this.hint:this.slot}};
}
const els={homeKpiGrid:{querySelector(sel){return cards[/"([a-z]+)"/.exec(sel)[1]]}}};
function renderState(slot,opts){states[Object.keys(cards).find(k=>cards[k].slot===slot)]=opts}
function clearTimeout(){}
let reloads=0;function loadTodayHome(){reloads++;return Promise.resolve()}
const window={location:{assign(u){window.went=u}}};
""" + source + r"""
assert.match(homeErrorReason({reason:{status:403}}),/권한/);
assert.match(homeErrorReason({reason:{status:404}}),/찾을 수 없습니다/);
assert.match(homeErrorReason({reason:{status:401}}),/로그인/);
assert.equal(homeErrorReason({reason:{status:500,message:'boom'}}),'boom');
assert.equal(homeErrorStatus({reason:{status:404}}),404);

setHomeCard('sales','1원','ok');
failHomeCard('bank',homeErrorReason({reason:{status:404}}),404);
failUnsettledHomeCards('렌더 오류');
assert.equal(cards.sales.hint.textContent,'ok');
assert.equal(states.sales,undefined);
for(const k of ['bank','review','payroll','approval']){
  assert.equal(states[k].kind,'error',k);
  assert.equal(cards[k].hint.textContent,'',k);
}
assert.deepEqual(states.bank.actions.map(a=>a.label),['다시 시도','관리자 화면 열기']);
assert.deepEqual(states.review.actions.map(a=>a.label),['다시 시도']);
states.bank.actions[1].onClick();assert.equal(window.went,'/static/apps/obys/admin.html');
states.review.actions[0].onClick();assert.equal(reloads,1);
console.log(JSON.stringify({ok:true}));
"""
    result = subprocess.run(["node", "-e", code], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip().splitlines()[-1]) == {"ok": True}
    assert re.search(r"onClick", source)
