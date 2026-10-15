"""ACCT-OBYS-CONTRACT-COMPANY-SEAL-DESIGN-20261015 — 계약서 도장(법인/개인사업자) SVG 생성과 화면·PDF 연결."""
import importlib.util
import json
import shutil
import subprocess
from pathlib import Path
from urllib.parse import unquote

import pytest

from app.services import yeoljeong_company_seal as py_seal

ROOT = Path(__file__).parents[2]
OBYS = ROOT / "app/static/apps/obys"
SEAL_JS = OBYS / "modules/company-seal.js"
CORE_JS = OBYS / "modules/contract-core.js"
INDEX = (OBYS / "index.html").read_text(encoding="utf-8")
V41 = (OBYS / "mockup-v4-1.html").read_text(encoding="utf-8")
CORE = CORE_JS.read_text(encoding="utf-8")
EDITOR = (OBYS / "modules/contract-editor-v41.js").read_text(encoding="utf-8")

NODE = shutil.which("node")

spec = importlib.util.spec_from_file_location("obys_release_assets", ROOT / "scripts/obys_release_assets.py")
release_tool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release_tool)

CORP = {"kind": "corporation", "companyName": "주식회사 열정국밥", "representative": "오윤희"}
INDIVIDUAL = {"kind": "individual", "companyName": "열정국밥 미아점", "representative": "최미미"}


def _node(script, payload=None):
    if NODE is None:
        pytest.skip("node 미설치 런타임 (호스트 pytest 에서 실행)")
    wrapper = (
        "globalThis.window = globalThis;"
        f"require({json.dumps(str(SEAL_JS))});"
        f"const C = require({json.dumps(str(CORE_JS))});"
        "const input = JSON.parse(process.argv[1]);"
        f"{script}"
    )
    done = subprocess.run([NODE, "-e", wrapper, json.dumps(payload or {})], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def _svg_texts(markup):
    import re

    return re.findall(r"<text [^>]*>([^<]*)</text>", markup)


def test_corporation_seal_has_company_name_ring_dot_and_title():
    out = _node("console.log(JSON.stringify({svg: ObysCompanySeal.svg(input)}));", CORP)
    markup = out["svg"]
    assert markup.startswith("<svg ") and 'viewBox="0 0 200 200"' in markup
    assert "#c1272d" in markup and "<image" not in markup
    assert markup.count("<circle") == 3  # 바깥 원, 안쪽 원, 점
    texts = _svg_texts(markup)
    assert "".join(texts[:8]) == "주식회사열정국밥"
    assert "".join(texts) == "주식회사열정국밥" + "대표이사인"


def test_individual_seal_has_business_name_ring_and_representative_with_in():
    out = _node("console.log(JSON.stringify({svg: ObysCompanySeal.svg(input)}));", INDIVIDUAL)
    texts = _svg_texts(out["svg"])
    assert "".join(texts[:7]) == "열정국밥미아점"
    inner = texts[7:]
    assert sorted(inner) == sorted("최미미인") and "최" in inner and "인" in inner
    assert "대표이사인" not in "".join(texts)


def test_individual_without_business_name_falls_back_to_representative_on_ring():
    out = _node("console.log(JSON.stringify({svg: ObysCompanySeal.svg(input)}));", {"kind": "individual", "companyName": "", "representative": "홍길동"})
    assert "".join(_svg_texts(out["svg"])) == "홍길동" + "홍길동인"


def test_empty_names_return_null_and_no_stamp_uri():
    cases = [
        {"kind": "corporation", "companyName": "  ", "representative": "오윤희"},
        {"kind": "corporation", "companyName": "", "representative": ""},
        {"kind": "individual", "companyName": "열정국밥", "representative": ""},
        {"kind": "individual", "companyName": "", "representative": " "},
        {},
    ]
    out = _node(
        "console.log(JSON.stringify(input.map(o => [ObysCompanySeal.svg(o), ObysCompanySeal.dataUri(o), ObysCompanySeal.layout(o)])));",
        cases,
    )
    # individual 은 상호가 비어도 대표자만 있으면 그려지므로 빈 대표자 케이스만 null 이어야 한다.
    assert out[0] == [None, "", None] and out[1] == [None, "", None]
    assert out[2] == [None, "", None] and out[3] == [None, "", None] and out[4] == [None, "", None]


def test_long_company_name_shrinks_and_stays_inside_the_ring():
    out = _node(
        "console.log(JSON.stringify([5, 15, 30].map(n => ObysCompanySeal.layout({kind: 'corporation', companyName: '가'.repeat(n)}))));"
    )
    short, long15, long30 = out
    assert len(long15["ring"]) == 15
    assert long15["ring"][0]["fs"] < short["ring"][0]["fs"]
    assert long30["ring"][0]["fs"] < long15["ring"][0]["fs"]
    for plan in out:
        for char in plan["ring"]:
            half = char["fs"] / 2
            assert plan["ringMidR"] + half < plan["outerR"] - 1.6
            assert plan["ringMidR"] - half > plan["innerR"] + 1.2
        for char in plan["inner"]:
            assert (char["x"] - 100) ** 2 + (char["y"] - char["fs"] * 0.32 - 100) ** 2 < plan["innerR"] ** 2


def test_inner_characters_fit_inside_inner_circle_for_long_representative_names():
    out = _node(
        "console.log(JSON.stringify(['김', '김철', '김철수', '남궁민수', '독고영재호'].map(r => ObysCompanySeal.layout({kind: 'individual', companyName: '상호', representative: r}))));"
    )
    for plan in out:
        for char in plan["inner"]:
            for dx in (-char["fs"] / 2, char["fs"] / 2):
                for y in (char["y"], char["y"] - char["fs"] * 0.82):
                    assert (char["x"] + dx - 100) ** 2 + (y - 100) ** 2 < plan["innerR"] ** 2


def test_special_characters_are_escaped_in_svg():
    out = _node("console.log(JSON.stringify({svg: ObysCompanySeal.svg(input)}));", {"kind": "corporation", "companyName": "A&B<주>"})
    assert "<주>" not in out["svg"] and "&amp;" in out["svg"] and "&lt;" in out["svg"]


def test_kind_is_found_from_entity_type_then_name():
    out = _node(
        "const K = ObysCompanySeal.kindFor;"
        "console.log(JSON.stringify([K('corporation','x'), K('individual','주식회사 x'), K('', '주식회사 열정'), K('', '열정국밥'), K('법인','x'), K(undefined,'(주)가나')]));"
    )
    assert out == ["corporation", "individual", "corporation", "individual", "corporation", "corporation"]


def test_contract_values_win_over_business_record():
    out = _node(
        "console.log(JSON.stringify(ObysCompanySeal.optionsFor(input.contract, input.business)));",
        {
            "contract": {"employerName": "계약서상호", "employer_representative": "계약대표"},
            "business": {"name": "사업장상호", "representative": "사업장대표", "entity_type": "corporation"},
        },
    )
    assert out["companyName"] == "계약서상호" and out["representative"] == "계약대표" and out["kind"] == "corporation"
    fallback = _node(
        "console.log(JSON.stringify(ObysCompanySeal.optionsFor({}, input)));",
        {"name": "사업장상호", "representative": "사업장대표", "entityType": "individual"},
    )
    assert fallback["companyName"] == "사업장상호" and fallback["representative"] == "사업장대표" and fallback["kind"] == "individual"


PREVIEW_SCRIPT = r"""
C.setBusinessResolver(id => input.businesses[id] || null);
const html = input.contracts.map(c => C.contractPreviewHtml(c));
console.log(JSON.stringify(html));
"""


def _stamp_markup(html):
    import re

    match = re.search(r'<img class="contract-stamp" src="(data:image/svg\+xml;charset=utf-8,[^"]+)"', html)
    return unquote(match.group(1).split(",", 1)[1]) if match else None


def test_contract_preview_embeds_the_seal_for_corporation_and_individual():
    base = {"contractType": "part_time", "employeeName": "시험직원", "branch": "열정국밥 중화점"}
    out = _node(
        PREVIEW_SCRIPT,
        {
            "businesses": {
                "biz-a": {"id": "biz-a", "name": "주식회사 열정국밥", "representative": "오윤희", "entity_type": "corporation"},
                "biz-b": {"id": "biz-b", "name": "열정국밥 미아점", "representative": "최미미", "entity_type": "individual"},
            },
            "contracts": [
                {**base, "businessId": "biz-a", "employerName": "주식회사 열정국밥", "employerRepresentative": "오윤희"},
                {**base, "businessId": "biz-b", "employerName": "열정국밥 미아점", "employerRepresentative": "최미미"},
                {**base, "business_id": "biz-b"},
                {**base, "businessId": "없는사업장"},
            ],
        },
    )
    corp, individual, from_business, none = out
    assert "".join(_svg_texts(_stamp_markup(corp))) == "주식회사열정국밥대표이사인"
    assert "".join(_svg_texts(_stamp_markup(individual))[:7]) == "열정국밥미아점"
    assert sorted(_svg_texts(_stamp_markup(individual))[7:]) == sorted("최미미인")
    assert sorted(_svg_texts(_stamp_markup(from_business))[7:]) == sorted("최미미인")
    assert _stamp_markup(none) is None and "contract-stamp" not in none
    assert "assets/stamps/" not in "".join(out)
    assert "사용자 서명" in corp and "(서명 또는 인)" in corp


def test_legacy_pngs_stay_in_repo_but_screens_no_longer_use_them():
    for name in ("biz-junghwa", "biz-sungshin", "biz-mia"):
        assert (OBYS / "assets/stamps" / f"{name}.png").is_file()
    assert "assets/stamps/" not in INDEX and "assets/stamps/" not in CORE
    assert "contractSealUri(contract, contractBusinessId" in INDEX and "contractSealUri(contract, contractBusinessId" in CORE


def test_seal_module_loads_before_the_screens_that_render_contracts():
    assert INDEX.index("modules/company-seal.js") < INDEX.index("modules/employee-portal.js")
    assert V41.index("modules/company-seal.js") < V41.index("modules/contract-core.js")
    assert "C.setBusinessResolver(businessRecord)" in EDITOR


def test_employee_portal_reads_contracts_through_the_same_preview_function():
    assert "previewHtml: bridge?.contractPreviewHtml" in (OBYS / "modules/employee-portal.js").read_text(encoding="utf-8")
    bridge = INDEX[INDEX.index("// 포털은 아래 bridge 로"):][:2500]
    assert "contractPreviewHtml," in bridge


def test_release_assets_pick_up_company_seal_module_without_changes():
    assert "/static/apps/obys/modules/company-seal.js" in release_tool.module_refs(INDEX)
    stamped, _ = release_tool.stamp_html(INDEX, "1a2b3c4d")
    assert "/static/apps/obys/modules/company-seal.js?v=1a2b3c4d" in stamped
    stamped_v41, _ = release_tool.stamp_html(V41, "1a2b3c4d")
    assert "modules/company-seal.js?v=1a2b3c4d" in stamped_v41
    urls = release_tool.purge_urls(OBYS, "1a2b3c4d", "https://example.test")
    assert "https://example.test/static/apps/obys/modules/company-seal.js" in urls
    assert "https://example.test/static/apps/obys/modules/company-seal.js?v=1a2b3c4d" in urls


def _flatten(plan):
    return json.loads(json.dumps(plan))


@pytest.mark.parametrize("options", [CORP, INDIVIDUAL, {"kind": "corporation", "companyName": "열정국밥중화점푸드서비스", "representative": ""}])
def test_server_layout_matches_browser_layout(options):
    js = _node("console.log(JSON.stringify(ObysCompanySeal.layout(input)));", options)
    py = _flatten(py_seal.layout(options["kind"], options["companyName"], options["representative"]))
    assert js["kind"] == py["kind"]
    assert len(js["ring"]) == len(py["ring"]) and len(js["inner"]) == len(py["inner"])
    for a, b in zip(js["ring"] + js["inner"], py["ring"] + py["inner"]):
        assert a["ch"] == b["ch"]
        for key in ("x", "y", "fs", "angle"):
            assert a.get(key) == pytest.approx(b.get(key), abs=0.011), (a, b)
    assert js["dot"]["x"] == pytest.approx(py["dot"]["x"], abs=0.011) and js["dot"]["y"] == pytest.approx(py["dot"]["y"], abs=0.011)


def test_server_layout_returns_none_for_empty_names():
    assert py_seal.layout("corporation", "", "오윤희") is None
    assert py_seal.layout("individual", "상호", "") is None
    assert py_seal.layout("individual", "", "") is None


class _RecordingCanvas:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def record(*args, **kwargs):
            self.calls.append((name, args))
            if name == "stringWidth":
                return args[2] * 0.9
            if name == "beginText":
                return _RecordingText(self.calls)

        return record


class _RecordingText:
    def __init__(self, calls):
        self.calls = calls

    def __getattr__(self, name):
        def record(*args, **kwargs):
            self.calls.append((f"text.{name}", args))

        return record


def test_pdf_drawing_places_every_character_without_reportlab():
    plan = py_seal.layout("corporation", "주식회사 열정국밥", "")
    canv = _RecordingCanvas()
    py_seal.draw_seal(canv, plan, 10, 20, 100, "Font")
    drawn = [args[0] for name, args in canv.calls if name == "text.textOut"]
    assert "".join(drawn) == "주식회사열정국밥대표이사인"
    assert [name for name, _ in canv.calls].count("circle") == 3
    assert canv.calls[0][0] == "saveState" and canv.calls[-1][0] == "restoreState"
    assert sum(1 for name, _ in canv.calls if name == "saveState") == sum(1 for name, _ in canv.calls if name == "restoreState")
