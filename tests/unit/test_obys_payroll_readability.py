"""오비서 급여 화면 가독성 — 글자 크기·열 정렬·박스 깨짐 방지(CSS·class) 검증."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2] / "app" / "static" / "apps" / "obys"
HTML = (ROOT / "admin.html").read_text(encoding="utf-8")
JS = (ROOT / "modules" / "payroll-admin.js").read_text(encoding="utf-8")


def _payroll_css() -> str:
    return "\n".join(line for line in HTML.splitlines() if ".payroll-screen" in line)


def _all_font_sizes_in_scope() -> list[float]:
    """급여 화면 범위(.payroll-screen) 규칙의 font-size 전부(미디어쿼리 안 포함). 전역 10px 기본값은 범위 규칙이 덮어쓴다."""
    sizes = []
    for line in HTML.splitlines():
        if ".payroll-screen" in line:
            for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", line):
                sel, body = m.group(1), m.group(2)
                if ".payroll-screen" in sel:
                    sizes += [float(x) for x in re.findall(r"font-size:\s*([\d.]+)px", body)]
    return sizes


def test_source_status_and_scope_fonts_at_least_12px():
    sizes = _all_font_sizes_in_scope()
    assert sizes and min(sizes) >= 12
    assert re.search(r"\.payroll-screen \.source-status\{[^}]*font-size:12px", HTML)


def test_amount_inputs_right_aligned_directly():
    css = _payroll_css()
    m = re.search(r"\.payroll-screen td\.num input[^{]*\{[^}]*text-align:right", css)
    assert m, "input 에 직접 text-align:right 필요"
    assert "input[data-held-field=amount]" in css and ".pi-amount input" in css


def test_card_mode_resets_table_min_width():
    assert "table{min-width:760px}" in HTML  # 기존 규칙은 유지
    rules = re.findall(r"@(?:container|media)\s*\([^)]*\)\{\.payroll-screen \.table-wrap>table\.payroll-item-table\{([^}]*)\}", HTML)
    assert len(rules) == 2 and all("min-width:0" in r for r in rules)


def test_payroll_scope_css_has_no_tiny_fonts():
    css = _payroll_css()
    assert css
    assert not re.search(r"font-size:\s*(9|10|11)(\.\d+)?px", css)
    assert not re.search(r"font-size:\s*(9|10|11)(\.\d+)?px", JS)


def test_payroll_scope_table_cell_and_header_sizes():
    css = _payroll_css()
    assert re.search(r"\.payroll-screen td\{[^}]*font-size:14px", css)
    th = re.search(r"\.payroll-screen th\{[^}]*\}", css).group(0)
    assert "font-size:13px" in th and "font-weight:700" in th and "#475467" in th and "keep-all" in th
    assert "tabular-nums" in css and "overflow-wrap:anywhere" in css
    assert "2px solid" in css and "container-type:inline-size" in css
    assert re.search(r"\.payroll-screen \.metric strong\{[^}]*font-size:22px", css)
    assert re.search(r"max-width:480px\)\{[^\n]*\.metric strong\{font-size:18px", css)


def test_only_payroll_route_gets_scope_class():
    assert 'state.route==="payroll"?" payroll-screen"' in HTML


def _fn(name):
    start = HTML.index(f"    function {name}(")
    return HTML[start:HTML.index("\n", start)]


@pytest.mark.skipif(shutil.which("node") is None, reason="node 없음")
def test_amount_columns_right_aligned_and_status_centered_names_left():
    body = "console.log(JSON.stringify(Object.fromEntries(['직원','지점','총지급액','공제','실수령','상태','급여월','일자','근무시간'].map(k=>[k,payrollColClassV41(k)]))))"
    out = subprocess.run(["node", "-e", _fn("payrollColClassV41") + "\n" + body], capture_output=True, text=True, timeout=20)
    assert out.returncode == 0, out.stderr
    got = json.loads(out.stdout)
    assert got["총지급액"] == got["공제"] == got["실수령"] == "num"
    assert got["상태"] == "center"
    assert got["직원"] == got["지점"] == got["급여월"] == got["일자"] == ""


def test_class_applied_only_in_payroll_table_not_attendance():
    assert 'const colCls=k=>payroll?payrollColClassV41(k):""' in HTML
    assert "<th${clsAttr(k)}>" in HTML and "<td${clsAttr(k)}>" in HTML


def test_item_css_media_kept_and_container_added():
    css = JS[JS.index("const ITEM_CSS"):JS.index("</style>", JS.index("const ITEM_CSS"))]
    assert "@media (max-width:480px){" in css and "@container (max-width:600px){" in css
    media = css.split("@media (max-width:480px){", 1)[1].split("}@container", 1)[0] + "}"
    container = css.split("@container (max-width:600px){", 1)[1]
    assert media == container


def test_editor_has_no_fixed_min_width_inputs():
    assert "min-width:110px" not in JS and "min-width:90px" not in JS
    assert "width:100%;min-width:0;box-sizing:border-box" in JS


def test_item_tables_wrapped_and_aligned():
    assert JS.count('<div class="table-wrap"><table') >= 2
    assert "thead th:nth-child(3){text-align:right}" in HTML and 'td class="pi-amount num"' in JS and "<colgroup>" in JS
