#!/usr/bin/env python3
"""운영 채팅 화면의 목표 버튼·오른쪽 목표 패널 '변경 전' 실캡처 (목업 5판, 2026-10-28).

E2E 로그인 URL 은 비밀값이므로 환경변수로만 받는다(파일에 남기지 않는다).
실행: E2E_URL='<get_e2e_login_url 결과>' SID=<세션 UUID> timeout 180 python3 capture_before_live.py
출력: png/live-before_*.png, live_before_text.json(화면 글자 덤프 — 시안 대조용)
"""
from __future__ import annotations

import json
import os
import pathlib

from playwright.sync_api import sync_playwright

ROOT = pathlib.Path(__file__).resolve().parent
OUT = ROOT / "png"
TEXT_JS = "e=>e?(e.innerText||'').replace(/\\n{2,}/g,'\\n').slice(0,4000):''"


def shot(pg, name, w, h):
    path = OUT / f"live-before_{name}_{w}x{h}.png"
    pg.screenshot(path=str(path), full_page=False)
    return path.name


def save(dump):
    (ROOT / "live_before_text.json").write_text(json.dumps(dump, ensure_ascii=False, indent=1), encoding="utf-8")


VIS_BTNS = ("()=>[...document.querySelectorAll('button')].map(e=>{const r=e.getBoundingClientRect();"
            "return [Math.round(r.left),Math.round(r.top),(e.innerText||'').trim().replace(/\\s+/g,' ').slice(0,90)]})"
            ".filter(x=>x[1]>120&&x[1]<900&&x[0]<1440&&x[2])")


def capture(pg, w, h, dump):
    pg.set_viewport_size({"width": w, "height": h})
    pg.goto("https://aads.newtalk.kr/chat#" + os.environ["SID"])
    pg.reload()
    pg.wait_for_timeout(8000)
    files = [shot(pg, "chat-strip", w, h)]
    pg.locator("button", has_text="목표").filter(has=pg.locator("b")).first.click()
    pg.wait_for_timeout(2500)
    files.append(shot(pg, "chat-goal-button_open", w, h))
    before = pg.evaluate(VIS_BTNS)
    dump[f"open_buttons_{w}"] = before
    save(dump)
    # 드롭다운 안의 첫 목표 항목(목표 제목 버튼)을 눌러 오른쪽 목표 패널을 연다
    opened = False
    for x, y, t in before:
        if y < 200 or "🎯" in t or "승인:" in t or not any(k in t for k in ("P0", "P1", "P2", "M1", "M2", "M3", "%")):
            continue
        try:
            pg.mouse.click(x + 20, y + 8)
            pg.wait_for_timeout(3000)
        except Exception:
            continue
        if pg.locator("aside[aria-label='목표 진행']").count():
            opened = True
            dump[f"clicked_{w}"] = t
            break
    dump[f"panel_opened_{w}"] = opened
    if opened:
        files.append(shot(pg, "chat-goal-panel", w, h))
        dump[f"panel_{w}"] = pg.locator("aside[aria-label='목표 진행']").first.evaluate(TEXT_JS)
    save(dump)
    return files


def main():
    dump = {}
    with sync_playwright() as pw:
        b = pw.chromium.launch()
        pg = b.new_page(viewport={"width": 1440, "height": 900})
        pg.goto(os.environ["E2E_URL"])
        pg.wait_for_url("**/chat**", timeout=60000)
        files = capture(pg, 1440, 900, dump) + capture(pg, 390, 844, dump)
        b.close()
    dump["files"] = files
    save(dump)
    print(json.dumps({"files": files, "panel_1440": dump.get("panel_opened_1440"), "panel_390": dump.get("panel_opened_390")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
