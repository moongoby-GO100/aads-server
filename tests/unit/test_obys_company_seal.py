"""ACCT-OBYS-CONTRACT-COMPANY-SEAL-DESIGN-20261015 / ACCT-OBYS-CONTRACT-SEAL-FONT-HANJA-20261015 — 계약서 도장(법인/개인사업자) SVG 생성과 화면·PDF 연결."""
import importlib.util
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from app.services import yeoljeong_company_seal as py_seal

ROOT = Path(__file__).parents[2]
OBYS = ROOT / "app/static/apps/obys"
SEAL_JS = OBYS / "modules/company-seal.js"
CORE_JS = OBYS / "modules/contract-core.js"
INDEX = (OBYS / "index.html").read_text(encoding="utf-8")
V41 = (OBYS / "admin.html").read_text(encoding="utf-8")
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
    assert "".join(texts) == "주식회사열정국밥" + "代表理事"  # 기대값 변경: 안쪽 문구 대표이사인 → 代表理事(CEO 지시 2026-10-15)


def test_individual_seal_has_business_name_ring_and_representative_with_in():
    out = _node("console.log(JSON.stringify({svg: ObysCompanySeal.svg(input)}));", INDIVIDUAL)
    texts = _svg_texts(out["svg"])
    assert "".join(texts[:7]) == "열정국밥미아점"
    inner = texts[7:]
    assert sorted(inner) == sorted("최미미인") and "최" in inner and "인" in inner
    assert "代表理事" not in "".join(texts) and "대표이사인" not in "".join(texts)


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

    match = re.search(r'<svg [^>]*class="contract-stamp"[^>]*>.*?</svg>', html, re.S)
    return match.group(0) if match else None


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
    assert "".join(_svg_texts(_stamp_markup(corp))) == "주식회사열정국밥代表理事"
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
    # 이름 변경: contractSealUri → contractSealSvg (<img> 안 SVG 는 페이지 글꼴을 못 써서 인라인 SVG 로 바꿈)
    assert "contractSealSvg(contract, contractBusinessId" in INDEX and "contractSealSvg(contract, contractBusinessId" in CORE
    assert "contractSealUri" not in INDEX and "contractSealUri" not in CORE


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
    assert "".join(drawn) == "주식회사열정국밥代表理事"
    assert [name for name, _ in canv.calls].count("circle") == 3
    assert canv.calls[0][0] == "saveState" and canv.calls[-1][0] == "restoreState"
    assert sum(1 for name, _ in canv.calls if name == "saveState") == sum(1 for name, _ in canv.calls if name == "restoreState")


FONT_DIR = OBYS / "assets/fonts"
PDF_FONT_DIR = ROOT / "app/assets/fonts"
HANGUL_WOFF2 = FONT_DIR / "obys-seal-hangul-gugi.woff2"
HANJA_WOFF2 = FONT_DIR / "obys-seal-hanja-notoserifkr-black.woff2"


def _ksx1001_hangul():
    out = set()
    for hi in range(0xB0, 0xC9):
        for lo in range(0xA1, 0xFF):
            try:
                out.add(bytes([hi, lo]).decode("euc-kr"))
            except UnicodeDecodeError:
                pass
    return {ch for ch in out if 0xAC00 <= ord(ch) <= 0xD7A3}


def test_corporation_inner_text_is_four_hanja_in_two_columns_right_column_first():
    plan = _node("console.log(JSON.stringify(ObysCompanySeal.layout(input)));", CORP)
    inner = plan["inner"]
    assert [c["ch"] for c in inner] == list("代表理事")
    right, left = inner[:2], inner[2:]
    assert [c["ch"] for c in right] == ["代", "表"] and [c["ch"] for c in left] == ["理", "事"]
    assert right[0]["x"] == right[1]["x"] > 100 > left[0]["x"] == left[1]["x"]
    assert right[0]["y"] < right[1]["y"] and left[0]["y"] < left[1]["y"]
    assert right[0]["y"] == left[0]["y"]
    assert py_seal.CORP_TITLE == "代表理事"


def test_ring_is_the_business_name_in_hangul_and_individual_inner_text_is_unchanged():
    corp = _node("console.log(JSON.stringify(ObysCompanySeal.layout(input)));", CORP)
    assert "".join(c["ch"] for c in corp["ring"]) == "주식회사열정국밥"
    individual = _node("console.log(JSON.stringify(ObysCompanySeal.layout(input)));", INDIVIDUAL)
    assert "".join(c["ch"] for c in individual["ring"]) == "열정국밥미아점"
    assert "".join(c["ch"] for c in individual["inner"]) == "최미미인"


def test_svg_names_the_self_hosted_font_families_with_fallback_and_no_external_url():
    out = _node("console.log(JSON.stringify([ObysCompanySeal.svg(input), ObysCompanySeal.svg({...input, inline: true})]));", CORP)
    for markup in out:
        assert "font-family=\"'ObysSealHangul','ObysSealHanja','Noto Serif KR'" in markup
        assert "http://" not in markup.replace("http://www.w3.org/2000/svg", "") and "https://" not in markup and "url(" not in markup.replace("url(#seal-ink)", "")
    boxed, inline = out
    assert 'width="200" height="200"' in boxed and "contract-stamp" not in boxed
    assert 'class="contract-stamp"' in inline and 'width="200"' not in inline and 'viewBox="0 0 200 200"' in inline


def test_seal_fonts_load_only_when_a_seal_is_drawn_and_come_from_this_origin_path():
    out = _node(
        "const added = [], loaded = [];"
        "globalThis.document = {head: {appendChild: n => added.push(n)}, getElementById: () => null,"
        " createElement: () => ({}), fonts: {load: (f, t) => { loaded.push([f, t]); return Promise.resolve([]); }}};"
        "const before = [added.length, loaded.length];"
        "ObysCompanySeal.layout(input);"
        "const afterLayout = [added.length, loaded.length];"
        "ObysCompanySeal.svg(input);"
        "console.log(JSON.stringify({before, afterLayout, added: added.map(n => n.textContent), loaded}));",
        CORP,
    )
    assert out["before"] == [0, 0] and out["afterLayout"] == [0, 0]
    css = out["added"][0]
    assert css.count("@font-face") == 2 and css.count("font-display:swap") == 2
    assert "/static/apps/obys/assets/fonts/obys-seal-hangul-gugi.woff2" in css and "/static/apps/obys/assets/fonts/obys-seal-hanja-notoserifkr-black.woff2" in css
    assert "http" not in css and "googleapis" not in css and "cdn" not in css.lower()
    assert [f for f, _ in out["loaded"]] == ["34px 'ObysSealHangul'", "34px 'ObysSealHanja'"]


def test_seal_font_files_and_licenses_exist_and_stay_small():
    for name in ("OFL-Gugi.txt", "OFL-NotoSerifKR.txt"):
        text = (FONT_DIR / name).read_text(encoding="utf-8")
        assert "SIL OPEN FONT LICENSE Version 1.1" in text
    for name in ("Gugi-OFL.txt", "NotoSerifKR-OFL.txt"):
        assert "SIL OPEN FONT LICENSE Version 1.1" in (PDF_FONT_DIR / name).read_text(encoding="utf-8")
    assert HANGUL_WOFF2.read_bytes()[:4] == b"wOF2" and HANJA_WOFF2.read_bytes()[:4] == b"wOF2"
    assert HANGUL_WOFF2.stat().st_size < 150_000 and HANJA_WOFF2.stat().st_size < 10_000
    assert "Copyright" in (FONT_DIR / "OFL-Gugi.txt").read_text(encoding="utf-8").splitlines()[0]


def test_pdf_drawing_picks_a_font_per_character_when_font_for_is_given():
    plan = py_seal.layout("corporation", "주식회사 열정국밥", "")
    canv = _RecordingCanvas()
    py_seal.draw_seal(canv, plan, 0, 0, 100, "Base", lambda ch: "Han" if "一" <= ch <= "鿿" else "Hangul")
    fonts = [args[0] for name, args in canv.calls if name == "text.setFont"]
    assert fonts == ["Hangul"] * 8 + ["Han"] * 4


def test_release_assets_lists_both_seal_fonts_but_the_pages_do_not_preload_them():
    urls = release_tool.purge_urls(OBYS, "1a2b3c4d", "https://example.test")
    assert "https://example.test/static/apps/obys/assets/fonts/obys-seal-hangul-gugi.woff2" in urls
    assert "https://example.test/static/apps/obys/assets/fonts/obys-seal-hanja-notoserifkr-black.woff2" in urls
    for markup in (INDEX, V41):
        assert "assets/fonts/" not in markup and "fonts.googleapis" not in markup
    assert "assets/fonts/" not in (OBYS / "sw.js").read_text(encoding="utf-8")


# --- ACCT-OBYS-SEAL-REGISTERED-TRADE-NAME-20261016: 도장 상호 우선순위 = 사업장 trade_name → 계약서 employerName → 사업장 name ---
# 같은 표가 tests/unit/test_obys_company_seal_pdf.py 에서 서버(_contract_seal_inputs)에도 적용된다. 한쪽만 고치지 마라.
TRADE_NAME_CASES = [
    # 법인: 등록증 법인명이 계약서에 저장된 지점명보다 우선한다
    {"contract": {"employerName": "열정국밥 중화점", "employerRepresentative": "오윤희"},
     "business": {"name": "열정국밥 중화점", "trade_name": "주식회사 윤희에프엔비", "entity_type": "corporation", "representative": "오윤희"},
     "company": "주식회사 윤희에프엔비", "kind": "corporation", "inner": "代表理事"},
    # camelCase(설정 응답)도 같다
    {"contract": {"employer_name": "열정국밥 중화점", "employer_representative": "오윤희"},
     "business": {"name": "열정국밥 중화점", "tradeName": "주식회사 윤희에프엔비", "entityType": "corporation", "representative": "오윤희"},
     "company": "주식회사 윤희에프엔비", "kind": "corporation", "inner": "代表理事"},
    # 개인: 상호 "열정국밥", 대표자 김영주 → 안쪽 "김영주인"
    {"contract": {"employerName": "열정국밥 성신여대점", "employerRepresentative": "김영주"},
     "business": {"name": "열정국밥 성신여대점", "trade_name": "열정국밥", "entity_type": "individual", "representative": "김영주"},
     "company": "열정국밥", "kind": "individual", "inner": "김영주인"},
    # trade_name 이 없으면 계약서 값
    {"contract": {"employerName": "계약서상호", "employerRepresentative": "계약대표"},
     "business": {"name": "사업장상호", "trade_name": None, "entity_type": "individual", "representative": "사업장대표"},
     "company": "계약서상호", "kind": "individual", "inner": "계약대표인"},
    # 공백뿐인 trade_name 도 없는 것이다
    {"contract": {"employerName": "계약서상호", "employerRepresentative": "계약대표"},
     "business": {"name": "사업장상호", "trade_name": "   ", "entity_type": "individual", "representative": "사업장대표"},
     "company": "계약서상호", "kind": "individual", "inner": "계약대표인"},
    # 둘 다 없으면 사업장 name
    {"contract": {"employerRepresentative": "계약대표"},
     "business": {"name": "사업장상호", "entity_type": "individual", "representative": "사업장대표"},
     "company": "사업장상호", "kind": "individual", "inner": "계약대표인"},
    # entity_type 이 없으면 trade_name 기준 CORP_PATTERN (계약서·사업장 name 에는 법인 표지가 없다)
    {"contract": {"employerName": "열정국밥 중화점", "employerRepresentative": "오윤희"},
     "business": {"name": "열정국밥 중화점", "trade_name": "주식회사 윤희에프엔비", "representative": "오윤희"},
     "company": "주식회사 윤희에프엔비", "kind": "corporation", "inner": "代表理事"},
    # 기대값 변경(CEO 지시 2026-10-16 "밑줄표기는 빼줘"): 이전에는 '_' 를 원문 그대로 도장 입력에 실었다. 서명 전 도장 입력은 '_' → 공백이다.
    {"contract": {"employerName": "열정국밥_미아점", "employerRepresentative": "최미미"},
     "business": {"name": "열정국밥_미아점", "trade_name": "열정국밥_미아점", "entity_type": "individual", "representative": "최미미"},
     "company": "열정국밥 미아점", "kind": "individual", "inner": "최미미인"},
]


@pytest.mark.parametrize("case", TRADE_NAME_CASES)
def test_seal_company_name_priority_is_trade_name_then_contract_then_business_name(case):
    opts = _node("console.log(JSON.stringify(ObysCompanySeal.optionsFor(input.contract, input.business)));", case)
    assert opts["companyName"] == case["company"] and opts["kind"] == case["kind"]
    plan = _node("console.log(JSON.stringify(ObysCompanySeal.layout(input)));", opts)
    ring = "".join(c["ch"] for c in plan["ring"])
    assert ring == "".join(case["company"].split())
    inner = "".join(c["ch"] for c in plan["inner"])
    assert sorted(inner) == sorted(case["inner"])
    server = py_seal.layout(opts["kind"], opts["companyName"], opts["representative"])
    assert "".join(c["ch"] for c in server["ring"]) == ring and sorted("".join(c["ch"] for c in server["inner"])) == sorted(case["inner"])


# 기대값 변경(CEO 지시 2026-10-16 "밑줄표기는 빼줘"): 이전에는 등록증 상호의 '_' 를 띠 글자로 그대로 찍었다.
def test_underscore_in_registered_trade_name_is_not_stamped_on_the_ring_before_signing():
    opts = _node(
        "console.log(JSON.stringify(ObysCompanySeal.optionsFor(input.contract, input.business)));",
        {"contract": {}, "business": {"name": "x", "trade_name": "열정국밥_미아점", "representative": "최미미"}},
    )
    assert opts["companyName"] == "열정국밥 미아점"
    markup = _node("console.log(JSON.stringify({svg: ObysCompanySeal.svg(input)}));", opts)["svg"]
    texts = _svg_texts(markup)
    assert "".join(texts[:7]) == "열정국밥미아점" and "_" not in "".join(texts)
    assert "열정국밥 미아점 도장" in markup


DISPLAY_NAME_CASES = [
    ("열정국밥_미아점", "열정국밥 미아점"),
    ("A__B", "A B"),
    ("열정국밥＿미아점", "열정국밥 미아점"),
    ("_열정국밥_", "열정국밥"),
    ("열정국밥 _ 미아점", "열정국밥 미아점"),
    ("열정국밥 중화점", "열정국밥 중화점"),
    ("주식회사 윤희에프엔비", "주식회사 윤희에프엔비"),
    ("  ", ""),
]


@pytest.mark.parametrize(("raw", "shown"), DISPLAY_NAME_CASES)
def test_display_name_replaces_underscores_with_single_spaces_in_js_and_python(raw, shown):
    assert _node("console.log(JSON.stringify(ObysCompanySeal.displayName(input)));", raw) == shown
    assert py_seal.display_name(raw) == shown


def test_display_name_handles_missing_values():
    assert _node("console.log(JSON.stringify([ObysCompanySeal.displayName(null), ObysCompanySeal.displayName(undefined)]));") == ["", ""]
    assert py_seal.display_name(None) == ""


def test_unsigned_ring_of_underscore_name_matches_between_browser_and_server():
    for raw, ring in (("열정국밥_미아점", "열정국밥미아점"), ("A__B", "AB"), ("열정국밥", "열정국밥")):
        opts = _node("console.log(JSON.stringify(ObysCompanySeal.optionsFor(input.contract, input.business)));",
                     {"contract": {"employerName": raw, "employerRepresentative": "최미미"}, "business": {}})
        js = _node("console.log(JSON.stringify(ObysCompanySeal.layout(input)));", opts)
        py = py_seal.layout("individual", py_seal.display_name(raw), "최미미")
        assert "".join(c["ch"] for c in js["ring"]) == ring == "".join(c["ch"] for c in py["ring"])
        for a, b in zip(js["ring"], py["ring"]):
            assert a["ch"] == b["ch"] and a["angle"] == pytest.approx(b["angle"], abs=0.011) and a["fs"] == pytest.approx(b["fs"], abs=0.011)


def test_layout_itself_never_rewrites_the_name_so_a_frozen_seal_keeps_its_geometry():
    # 고정값 '열정국밥_미아점'(8글자 링, 첫 글자 -2도)은 정규화로 7글자(첫 글자 3도)가 되면 안 된다.
    js = _node("console.log(JSON.stringify(ObysCompanySeal.layout(input)));", {"kind": "individual", "companyName": "열정국밥_미아점", "representative": "최미미"})
    py = py_seal.layout("individual", "열정국밥_미아점", "최미미")
    for plan in (js, py):
        assert "".join(c["ch"] for c in plan["ring"]) == "열정국밥_미아점"
        assert plan["ring"][0]["angle"] == pytest.approx(-2.0, abs=0.011)


def test_signed_contract_keeps_underscore_in_frozen_and_legacy_seals():
    frozen = {"kind": "individual", "company_name": "열정국밥_미아점", "representative": "최미미"}
    base = {"status": "signed", "employerName": "열정국밥_미아점", "employerRepresentative": "최미미"}
    business = {"name": "열정국밥_미아점", "trade_name": "열정국밥_미아점", "representative": "최미미"}
    script = "console.log(JSON.stringify(ObysCompanySeal.optionsFor(input.contract, input.business)));"
    assert _node(script, {"contract": {**base, "signed_pdf_seal": frozen}, "business": business})["companyName"] == "열정국밥_미아점"
    assert _node(script, {"contract": base, "business": business})["companyName"] == "열정국밥_미아점"  # 고정값 없는 옛 서명본
    out = _node(PREVIEW_SCRIPT, {"businesses": {"biz-m": {"id": "biz-m", **business}},
                                 "contracts": [{"contractType": "part_time", "employeeName": "시험직원", "businessId": "biz-m", **base, "signed_pdf_seal": frozen}]})
    assert "".join(_svg_texts(_stamp_markup(out[0]))[:8]) == "열정국밥_미아점"


def test_contract_body_employer_name_is_not_rewritten_by_the_seal_rule():
    # 본문 상호는 서명 전후·서버 PDF 스냅샷이 같은 표기여야 한다 — 밑줄 정규화는 도장 입력에만 적용한다.
    base = {"contractType": "part_time", "employeeName": "시험직원", "businessId": "biz-m",
            "employerName": "열정국밥_미아점", "employerRepresentative": "최미미"}
    business = {"id": "biz-m", "name": "열정국밥_미아점", "trade_name": "열정국밥_미아점", "representative": "최미미", "entity_type": "individual"}
    out = _node(PREVIEW_SCRIPT, {"businesses": {"biz-m": business}, "contracts": [{**base, "status": "requested"}, {**base, "status": "signed"}]})
    requested, signed = out
    assert "열정국밥_미아점" in requested and "열정국밥_미아점" in signed


def test_contract_preview_stamp_uses_the_registered_trade_name_over_the_stored_employer_name():
    base = {"contractType": "part_time", "employeeName": "시험직원", "branch": "열정국밥 중화점", "businessId": "biz-j"}
    out = _node(
        PREVIEW_SCRIPT,
        {
            "businesses": {
                "biz-j": {"id": "biz-j", "name": "열정국밥 중화점", "trade_name": "주식회사 윤희에프엔비", "representative": "오윤희", "entity_type": "corporation"},
            },
            "contracts": [{**base, "employerName": "열정국밥 중화점", "employerRepresentative": "오윤희"}],
        },
    )
    assert "".join(_svg_texts(_stamp_markup(out[0]))) == "주식회사윤희에프엔비代表理事"


SIGNED_BASE = {"status": "signed", "employerName": "열정국밥 중화점", "employerRepresentative": "오윤희"}
LIVE_BUSINESS = {"name": "열정국밥 중화점", "trade_name": "주식회사 새상호", "entity_type": "corporation", "representative": "오윤희"}


def _options(contract, business=LIVE_BUSINESS):
    return _node("console.log(JSON.stringify(ObysCompanySeal.optionsFor(input.contract, input.business)));", {"contract": contract, "business": business})


def test_signed_contract_seal_uses_the_frozen_seal_not_the_current_trade_name():
    frozen = {"kind": "corporation", "company_name": "주식회사 윤희에프엔비", "representative": "오윤희"}
    opts = _options({**SIGNED_BASE, "signed_pdf_seal": frozen})
    assert (opts["companyName"], opts["kind"], opts["representative"]) == ("주식회사 윤희에프엔비", "corporation", "오윤희")
    # 서명 뒤 상호를 바꿔도 화면 도장은 서명본 PDF(고정값)와 같다
    assert _options({**SIGNED_BASE, "signed_pdf_seal": {**frozen, "kind": "individual", "company_name": "열정국밥"}})["companyName"] == "열정국밥"
    assert _options({**SIGNED_BASE, "signedPdfSeal": frozen})["companyName"] == "주식회사 윤희에프엔비"


def test_signed_contract_without_a_frozen_seal_ignores_the_current_trade_name():
    assert _options(SIGNED_BASE)["companyName"] == "열정국밥 중화점"
    assert _options({**SIGNED_BASE, "signed_pdf_seal": {"company_name": "  "}})["companyName"] == "열정국밥 중화점"


def test_unsigned_contract_ignores_a_seal_field_and_uses_the_current_trade_name():
    forged = {"kind": "individual", "company_name": "위조상호", "representative": "위조대표"}
    for status in ("draft", "requested"):
        assert _options({**SIGNED_BASE, "status": status, "signed_pdf_seal": forged})["companyName"] == "주식회사 새상호"


def test_contract_preview_of_a_signed_contract_stamps_the_frozen_seal():
    out = _node(
        PREVIEW_SCRIPT,
        {
            "businesses": {"biz-j": {"id": "biz-j", **LIVE_BUSINESS}},
            "contracts": [{"contractType": "part_time", "employeeName": "시험직원", "branch": "열정국밥 중화점", "businessId": "biz-j", **SIGNED_BASE,
                           "signed_pdf_seal": {"kind": "corporation", "company_name": "주식회사 윤희에프엔비", "representative": "오윤희"}}],
        },
    )
    assert "".join(_svg_texts(_stamp_markup(out[0]))) == "주식회사윤희에프엔비代表理事"


def test_new_contract_defaults_for_employer_name_prefer_the_registered_trade_name():
    assert "business.tradeName || business.name" in INDEX
    assert "business.trade_name || business.tradeName || business.name" in EDITOR


def test_admin_business_info_form_has_a_trade_name_input_with_help_and_sends_it_only_when_changed():
    assert 'name="trade_name"' in V41 and "사업자등록증 상호(법인명)" in V41
    assert "도장과 계약서에 이 이름이 찍힙니다. 비워 두면 사업장 이름을 씁니다." in V41
    assert "payload.trade_name=tradeName" in V41 and "dataset.initial" in V41
    # 안내 문구는 편집 화면에만 있다 — 계약서 본문 코드에는 넣지 않는다
    assert "비워 두면 사업장 이름을 씁니다" not in CORE and "비워 두면 사업장 이름을 씁니다" not in SEAL_JS.read_text(encoding="utf-8")
