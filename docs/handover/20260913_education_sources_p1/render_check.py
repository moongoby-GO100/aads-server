#!/usr/bin/env python3
"""교육자료 8건 렌더링 검증 (sources_p1, 2026-09-13 KST).

검사 항목 (작업지시서의 완료 조건):
  · 데스크톱(1440x900) / 모바일(390x844) 렌더링
  · 콘솔 에러 0건
  · 가로 스크롤(문서 폭 초과) 0건  — 긴 URL을 링크로 바꾸면서 생길 수 있는 회귀
  · id 중복 0건
  · 내부 앵커(#...) 대상 존재 여부
  · 부록 섹션(#source-audit-20260913) 실제 렌더 여부

사용법:
    python3 render_check.py <base-url> <출력 json> [스크린샷 디렉터리]
예:
    python3 render_check.py http://127.0.0.1:8791 render_check_20260913.json shots/
"""
import json
import pathlib
import sys

from playwright.sync_api import sync_playwright

FILES = [
    "20260912_rag_vectordb_embedding_education.html",
    "20260912_mcp_tool_calling_education.html",
    "20260912_finetuning_lora_practice_education.html",
    "20260912_llmops_observability_education.html",
    "20260912_multimodal_ai_education.html",
    "20260912_ai_coding_agent_practice_education.html",
    "20260912_ai_product_prd_ax_education.html",
    "20260912_ai_cost_token_economics_education.html",
]

VIEWPORTS = {"desktop": (1440, 900), "mobile": (390, 844)}

# 문서 안에서 실제로 검사할 자바스크립트.
# scrollWidth 가 clientWidth 보다 크면 가로 스크롤이 생긴 것이다.
PROBE = """() => {
  const ids = [...document.querySelectorAll('[id]')].map(e => e.id);
  const dup = ids.filter((v, i) => ids.indexOf(v) !== i);
  const anchors = [...document.querySelectorAll('a[href^="#"]')].map(a => a.getAttribute('href').slice(1));
  const missing = anchors.filter(a => a && !document.getElementById(a));
  // 문서 폭을 넘기는 요소를 범인으로 지목한다
  const docW = document.documentElement.clientWidth;
  const wide = [...document.querySelectorAll('body *')]
      .filter(e => e.getBoundingClientRect().width > docW + 1)
      .slice(0, 5)
      .map(e => (e.tagName + '.' + (e.className || '')).slice(0, 80));
  const audit = document.getElementById('source-audit-20260913');
  return {
    ids: ids.length,
    dup_ids: [...new Set(dup)],
    anchors: anchors.length,
    broken_anchors: missing,
    scroll_w: document.documentElement.scrollWidth,
    client_w: docW,
    overflow: document.documentElement.scrollWidth > docW + 1,
    wide_elements: wide,
    audit_present: !!audit,
    audit_links: audit ? audit.querySelectorAll('a[href^="http"]').length : 0,
    ext_links: document.querySelectorAll('a[href^="http"]').length,
  };
}"""


def main(argv):
    base = argv[0].rstrip("/")
    out = pathlib.Path(argv[1])
    shots = pathlib.Path(argv[2]) if len(argv) > 2 else None
    if shots:
        shots.mkdir(parents=True, exist_ok=True)

    report, failed = {}, False
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        for name in FILES:
            entry = {}
            for vp, (w, h) in VIEWPORTS.items():
                ctx = browser.new_context(viewport={"width": w, "height": h})
                page = ctx.new_page()
                errors, warnings = [], []
                page.on("console", lambda m: (errors if m.type == "error" else warnings).append(m.text))
                page.on("pageerror", lambda e: errors.append(str(e)))
                page.goto(f"{base}/{name}", wait_until="load")
                page.wait_for_timeout(400)
                data = page.evaluate(PROBE)
                data["console_errors"] = errors
                entry[vp] = data
                if shots:
                    page.screenshot(path=str(shots / f"{name[:-5]}__{vp}.png"))
                    # 부록 구간도 따로 남긴다 (검수 결과가 실제로 보이는지)
                    page.goto(f"{base}/{name}#source-audit-20260913", wait_until="load")
                    page.wait_for_timeout(300)
                    page.screenshot(path=str(shots / f"{name[:-5]}__{vp}_audit.png"))
                ok = (not errors and not data["overflow"]
                      and not data["dup_ids"] and not data["broken_anchors"]
                      and data["audit_present"])
                entry[vp]["pass"] = ok
                failed |= not ok
                ctx.close()
            report[name] = entry
            d, m = entry["desktop"], entry["mobile"]
            print(f"{'PASS' if d['pass'] and m['pass'] else 'FAIL'} {name}")
            print(f"   데스크톱 {d['scroll_w']}/{d['client_w']}px 가로넘침={d['overflow']} · "
                  f"모바일 {m['scroll_w']}/{m['client_w']}px 가로넘침={m['overflow']}")
            print(f"   id {d['ids']}개(중복 {len(d['dup_ids'])}) · 내부앵커 {d['anchors']}개"
                  f"(깨짐 {len(d['broken_anchors'])}) · 외부링크 {d['ext_links']}개"
                  f"(부록 {d['audit_links']}개) · 콘솔에러 {len(d['console_errors'])}건")
            for bad in d["dup_ids"] + d["broken_anchors"] + d["console_errors"] + d["wide_elements"]:
                print(f"   - {bad}")
        browser.close()
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"\n리포트 저장: {out}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
