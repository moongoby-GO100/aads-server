"""급여 지급항목 계산: 분 → 시간 표시·생성(금액 불변)."""
import os
import shutil
import subprocess
from decimal import Decimal

import pytest

os.environ.setdefault("JWT_SECRET_KEY", "test-only-secret-key-that-is-at-least-32-bytes-long")

from app.services import yeoljeong_payroll_pdf as pdf
from tests.unit.test_obys_payroll_deduction_live_calc_screen import MODULE, PREFIX


@pytest.mark.parametrize("minutes,qty,unit", [(450, 7.5, "hour"), (240, 4, "hour"), (250, 250, "minute")])
def test_hourly_calc_unit_and_amount_unchanged(minutes, qty, unit):
    calc = pdf.hourly_calc(12500, minutes)
    assert calc["qty"] == qty and calc["unit"] == unit
    assert pdf.calc_amount(calc) == pdf.calc_amount({"rate": 12500, "qty": minutes, "unit": "minute"})
    if minutes == 450:
        assert pdf.calc_amount(calc) == 93750


def test_hourly_note_worker_and_freelancer():
    assert pdf.hourly_note(12500, 450) == "시급 12,500원 × 승인 근로 7.5시간(450분, 원 미만 반올림)"
    assert pdf.hourly_note(12500, 450, True) == "용역비(시급 12,500원 × 승인 근로 7.5시간(450분, 원 미만 반올림))"
    assert pdf.hourly_note(12500, 250) == "시급 12,500원 × 승인 근로 250분(분 단위 환산, 원 미만 반올림)"
    assert pdf.hourly_note(12500, 250, True) == "용역비(시급 12,500원 × 승인 250분) — 분 단위 환산, 원 미만 반올림"


@pytest.mark.parametrize("freelancer", [False, True])
@pytest.mark.parametrize("minutes", [450, 240, 250])
def test_auto_note_stale_recognizes_both_formats(freelancer, minutes):
    note = pdf.hourly_note(12500, minutes, freelancer)
    assert pdf._auto_note_is_stale(note, pdf.hourly_calc(12500, minutes)) is False
    assert pdf._auto_note_is_stale(note, {"rate": 12500, "qty": minutes, "unit": "minute"}) is False
    assert pdf._auto_note_is_stale(note, pdf.hourly_calc(13000, minutes)) is True
    assert pdf._auto_note_is_stale(note, {"rate": 12500, "qty": minutes + 3, "unit": "minute"}) is True
    assert pdf._auto_note_is_stale("관리자 메모", pdf.hourly_calc(13000, minutes)) is False


def test_pdf_calc_note_shows_hours_for_minute_multiple_of_3():
    assert pdf.calc_note_text({"rate": 12500, "qty": 450, "unit": "minute"}) == "시급 12,500원 × 7.5시간"
    assert pdf.calc_note_text({"rate": 12500, "qty": 250, "unit": "minute"}) == "시급 12,500원 × 250분"
    assert pdf.calc_amount({"rate": 12500, "qty": 450, "unit": "minute"}) == 93750


@pytest.mark.skipif(shutil.which("node") is None, reason="node 없음")
def test_editor_shows_minute_450_as_7_5_hours():
    script = PREFIX + r'''
row.pay_items=[{code:'base',label:'기본급',amount:93750,calc_note:'',calc:{rate:12500,qty:450,unit:'minute'}},
  {code:'other',label:'기타',amount:10417,calc_note:'',calc:{rate:12500,qty:50,unit:'minute'}}];
(async()=>{
  await window.ObysPayrollAdmin.mount(host,{api,businessId:'b1',month:'2026-11',canManage:true});
  await settle();
  const html=itemsHost.innerHTML;
  assert.ok(html.includes('value="7.5"'),'qty 7.5');
  assert.ok(/<option value="hour" selected>/.test(html),'unit hour');
  assert.ok(/<option value="minute" selected>/.test(html),'50분은 분 유지');
  assert.ok(html.includes('93,750')||html.includes('93750'));
  console.log('OK');
})().catch(e=>{console.error(e);process.exit(1)});
'''
    r = subprocess.run(["node", "-e", script, str(MODULE)], capture_output=True, text=True, timeout=30, check=False)
    assert r.returncode == 0, r.stdout + r.stderr
