"""오비서 V4.1 admin.html 계약 — 조회 실패·원천 미구성 화면에 가짜 0건/0원 KPI 금지.

genericMetricsV41 를 node 로 실제 실행해 상태별 결과를 검증한다(네트워크·DB 없음).
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ADMIN = Path(__file__).resolve().parents[2] / "app/static/apps/obys/admin.html"
NODE = shutil.which("node")


def _html():
    return ADMIN.read_text(encoding="utf-8")


def _func_src():
    m = re.search(r"    function genericMetricsV41\(c,data\)\{.*?\n    \}\n", _html(), re.S)
    assert m, "genericMetricsV41 not found"
    return m.group(0)


def _run(state, c, data):
    if not NODE:
        pytest.skip("node 없음: 함수 실행 검증 생략")
    base = {"liveLoading": False, "liveError": "", "liveMetrics": [], "liveSource": None}
    base.update(state)
    js = f"const state={json.dumps(base)};{_func_src()}console.log(JSON.stringify(genericMetricsV41({json.dumps(c)},{json.dumps(data)})));"
    r = subprocess.run([NODE, "-e", js], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


API = {"source": {"api": "/x"}}
NONE = {"source": {"api": None}}
ZERO_LIVE_METRICS = [{"label": "DB 건수", "value": "0건"}, {"label": "합계", "value": "0원"}]


def test_no_fixed_zero_won_literal_and_wired():
    html = _html()
    assert "합계|0원" not in html
    # 선언 외에 실제 호출이 있어야 한다
    assert html.count("genericMetricsV41(c,data)") >= 2


def test_loading_shows_dash_even_with_stale_live_metrics():
    out = _run({"liveLoading": True, "liveMetrics": ZERO_LIVE_METRICS, "liveSource": {"live": True}}, API, [])
    assert out[0] == "DB 건수|—" and out[3] == "데이터 원천|불러오는 중"
    assert not any("0건" in x or "0원" in x for x in out)


def test_error_shows_failure():
    out = _run({"liveError": "boom", "liveMetrics": ZERO_LIVE_METRICS}, API, [])
    assert out[3] == "데이터 원천|조회 실패" and not any("0건" in x or "0원" in x for x in out)


def _real_configs():
    """admin.html 의 실제 rows/src/none/C/configs 선언을 node 로 평가해 configs 를 JSON 으로 얻는다."""
    if not NODE:
        pytest.skip("node 없음: 실제 configs 검증 생략")
    h = _html()
    i = h.index("    const rows=()=>[];")
    j = h.index("    const dateParts=")
    js = h[i:j] + "console.log(JSON.stringify(Object.fromEntries(Object.entries(configs).map(([k,v])=>[k,{source:v.source===undefined?null:v.source}]))));"
    r = subprocess.run([NODE, "-e", js], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def _real_run(route, state):
    """실제 configs[route] 와 실제 genericMetricsV41 을 같은 node 프로세스에서 실행한다."""
    if not NODE:
        pytest.skip("node 없음")
    h = _html()
    i = h.index("    const rows=()=>[];")
    j = h.index("    const dateParts=")
    base = {"liveLoading": False, "liveError": "", "liveMetrics": [], "liveSource": None}
    base.update(state[0])
    js = (f"const state={json.dumps(base)};" + h[i:j] + _func_src()
          + f"console.log(JSON.stringify(genericMetricsV41(configs[{json.dumps(route)}],{json.dumps(state[1])})));")
    r = subprocess.run([NODE, "-e", js], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def test_case1_margins_none_live_false_zero_metrics_all_dash():
    out = _real_run("margins", ({"liveSource": {"live": False}, "liveMetrics": ZERO_LIVE_METRICS}, []))
    assert out == ["DB 건수|—", "합계|—", "확인 필요|—", "데이터 원천|원천 미연동"]


def test_case2_margins_none_live_true_real_metrics_shown():
    m = [{"label": "DB 건수", "value": "5건"}, {"label": "합계", "value": "12000원"}]
    out = _real_run("margins", ({"liveSource": {"live": True}, "liveMetrics": m}, []))
    assert out == ["DB 건수|5건", "합계|12000원"]


def test_case3_sales_live_true_no_metrics_record_count():
    out = _real_run("sales", ({"liveSource": {"live": True, "record_count": 5}}, []))
    assert out[0] == "DB 건수|5건" and out[1] == "합계|—"


def test_case4_loading_hides_previous_live_metrics():
    m = [{"label": "DB 건수", "value": "5건"}]
    out = _real_run("sales", ({"liveLoading": True, "liveMetrics": m, "liveSource": {"live": True}}, []))
    assert out == ["DB 건수|—", "합계|—", "확인 필요|—", "데이터 원천|불러오는 중"]


def test_case5_error_shows_failure():
    out = _real_run("sales", ({"liveError": "boom", "liveMetrics": ZERO_LIVE_METRICS}, []))
    assert out[0] == "DB 건수|—" and out[3] == "데이터 원천|조회 실패"


def test_case6_real_configs_source_defined_at_runtime():
    cfg = _real_configs()
    assert cfg["margins"]["source"] is not None and cfg["margins"]["source"]["api"] is None
    assert cfg["sales"]["source"] is not None and cfg["sales"]["source"]["api"]
    assert all(v["source"] is not None for v in cfg.values()), [k for k, v in cfg.items() if v["source"] is None]


def test_static_none_not_used_for_judgement():
    fn = _func_src()
    assert "noSource" not in fn and "api==null" not in fn and "c.source" not in fn


def test_live_zero_rows_shows_zero_count_but_no_zero_won():
    out = _run({"liveSource": {"live": True, "record_count": 0}}, API, [])
    assert out[0] == "DB 건수|0건" and out[1] == "합계|—"


def test_live_record_count_missing_falls_back_to_data_length():
    assert _run({"liveSource": {"live": True}}, API, [{}, {}, {}])[0] == "DB 건수|3건"


def test_live_false_ignores_zero_metrics():
    out = _run({"liveSource": {"live": False}, "liveMetrics": ZERO_LIVE_METRICS}, API, [])
    assert out[3] == "데이터 원천|원천 미연동" and not any("0건" in x or "0원" in x for x in out)


def test_inline_scripts_node_check(tmp_path):
    if not NODE:
        pytest.skip("node 없음: 인라인 script 문법 검사 생략")
    scripts = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", _html(), re.S)
    assert scripts
    for i, code in enumerate(scripts):
        f = tmp_path / f"inline{i}.js"
        f.write_text(code, encoding="utf-8")
        r = subprocess.run([NODE, "--check", str(f)], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
