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
