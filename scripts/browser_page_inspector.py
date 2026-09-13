#!/usr/bin/env python3
"""페이지의 조작 가능한 요소를 한 번에 뽑아 안정적인 셀렉터로 돌려준다.

새 페이지를 자동화할 때마다 셀렉터를 시행착오로 찾느라 시간이 갔다.
2026-09-13 실측 — GenSpark 모델 선택 배지 하나를 누르는 데 네 번을 헛짚었고,
그중 한 번은 스크린샷(1280x805)과 실제 뷰포트(1920x945)의 1.5배 차이 때문에
좌표가 어긋나 엉뚱한 요소를 눌렀다. 명령 한 번 왕복이 6~25초라 시행착오
비용이 그대로 시간이 된다.

그래서 두 가지를 바꾼다.

1. **좌표를 쓰지 않는다.** 창 크기가 바뀌면 좌표는 전부 깨진다. 요소마다
   nth-of-type 까지 포함한 CSS 경로를 만들어 돌려준다. 이건 창 크기와 무관하다.
2. **왕복을 한 번으로 줄인다.** 버튼·입력·링크·역할 요소를 한 번의 eval 로
   모두 수집한다. 눌러보고 실패하고 다시 찾는 순환을 없앤다.

클릭 기록기도 함께 심는다. 사람이 누른 요소의 셀렉터가 남으므로, 자동화가
못 찾는 요소는 사람이 한 번 눌러주면 그 경로를 그대로 쓸 수 있다.
이벤트의 isTrusted 도 같이 남긴다 — 프로그램 클릭에 반응하지 않는 컴포넌트를
구분하는 유일한 근거다.

사용:
  browser_page_inspector.py --url <URL>            # 이동 후 분석
  browser_page_inspector.py --inspect              # 현재 페이지 분석
  browser_page_inspector.py --clicks               # 사람이 누른 요소 기록 조회
  browser_page_inspector.py --find "모델"           # 텍스트로 요소 찾기
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request

API = os.getenv("AADS_API_URL", "http://127.0.0.1:8100")
AGENT = os.getenv("AADS_PC_AGENT_ID", "2e9379a1-fed")
WORK_KEY = os.getenv("AADS_BROWSER_WORK_KEY", "genspark-login")
TENANT = os.getenv("AADS_TENANT_ID", "2d701a8c-9596-4757-8588-faa4f7837112")
USER_ID = os.getenv("AADS_BROWSER_USER_ID", "79ee004e-1e2e-490f-aa05-b096814f180d")
USER_EMAIL = os.getenv("AADS_BROWSER_USER_EMAIL", "moongoby@naver.com")
CONTAINER = os.getenv("AADS_API_CONTAINER", "aads-server")

# 요소마다 nth-of-type 까지 포함한 경로를 만든다. 클래스만으로는 같은 모양이
# 여러 개일 때 구분되지 않는다.
_CSS_PATH_JS = """
  const cssPath = (el) => {
    const parts = [];
    let e = el;
    while (e && e.nodeType === 1 && parts.length < 6) {
      let s = e.tagName.toLowerCase();
      if (e.id) { s += '#' + e.id; parts.unshift(s); break; }
      const cls = (typeof e.className === 'string' ? e.className : '')
        .trim().split(/\\s+/).filter(Boolean).slice(0, 3);
      if (cls.length) s += '.' + cls.join('.');
      const par = e.parentElement;
      if (par) {
        const sib = [...par.children].filter(c => c.tagName === e.tagName);
        if (sib.length > 1) s += ':nth-of-type(' + (sib.indexOf(e) + 1) + ')';
      }
      parts.unshift(s);
      e = e.parentElement;
    }
    return parts.join(' > ');
  };
"""

SPY_JS = """(() => {
  %s
  if (window.__aadsSpy) return 'already-installed';
  window.__aadsClicks = [];
  document.addEventListener('click', (ev) => {
    const el = ev.target;
    window.__aadsClicks.push({
      t: new Date().toISOString().slice(11, 19),
      tag: el.tagName,
      cls: (typeof el.className === 'string' ? el.className : '').slice(0, 90),
      text: (el.innerText || el.textContent || '').trim().slice(0, 50),
      path: cssPath(el),
      trusted: ev.isTrusted
    });
    if (window.__aadsClicks.length > 15) window.__aadsClicks.shift();
  }, true);
  window.__aadsSpy = true;
  return 'installed';
})()""" % _CSS_PATH_JS

INSPECT_JS = """(() => {
  %s
  const seen = new Set(), out = [];
  const sel = 'button, a[href], input, textarea, select, [role=button], [role=tab], [role=menuitem], [contenteditable=true]';
  document.querySelectorAll(sel).forEach(el => {
    const r = el.getBoundingClientRect();
    // 화면에 실제로 보이는 것만. 숨은 요소를 셀렉터로 주면 클릭이 조용히 실패한다.
    if (r.width < 4 || r.height < 4) return;
    const st = getComputedStyle(el);
    if (st.display === 'none' || st.visibility === 'hidden' || st.opacity === '0') return;
    const text = (el.innerText || el.value || el.placeholder || el.getAttribute('aria-label') || '').trim().slice(0, 44);
    const path = cssPath(el);
    if (seen.has(path)) return;
    seen.add(path);
    out.push({
      kind: el.tagName.toLowerCase() + (el.type ? ':' + el.type : ''),
      text: text,
      path: path,
      onScreen: r.top >= 0 && r.top < innerHeight
    });
  });
  return JSON.stringify({viewport: innerWidth + 'x' + innerHeight, count: out.length, items: out.slice(0, 120)});
})()""" % _CSS_PATH_JS


def mint_token() -> str:
    code = (
        'import sys\nsys.path.insert(0, "/app")\n'
        'from app.auth import create_token\n'
        f'print(create_token("{USER_ID}", "{USER_EMAIL}", is_admin=True, tenant_id="{TENANT}"))\n'
    )
    proc = subprocess.run(["docker", "exec", "-i", CONTAINER, "python3", "-"],
                          input=code, text=True, capture_output=True, timeout=90)
    for line in reversed((proc.stdout or "").splitlines()):
        line = line.strip()
        if line and "." in line and " " not in line:
            return line
    raise RuntimeError("token mint failed")


def cmd(token: str, ctype: str, params: dict, timeout: int = 175):
    body = json.dumps({
        "work_key": WORK_KEY, "agent_id": AGENT, "command_type": ctype,
        "params": params, "command_timeout_seconds": 150,
    }).encode()
    req = urllib.request.Request(
        f"{API}/api/v1/browser-bridge/work-sessions/route-execute", data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode())
    return (data.get("result") or {}).get("result") or {}


def ev(token: str, js: str):
    r = cmd(token, "browser_eval", {"expression": js})
    return r.get("value", r.get("error"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="")
    ap.add_argument("--inspect", action="store_true")
    ap.add_argument("--clicks", action="store_true")
    ap.add_argument("--find", default="")
    ap.add_argument("--json", action="store_true", help="원본 JSON 출력")
    args = ap.parse_args()

    token = mint_token()

    if args.url:
        t0 = time.time()
        cmd(token, "browser_navigate", {"url": args.url, "wait_seconds": 12})
        print(f"  이동 완료 ({time.time() - t0:.0f}초)")

    # 이동하면 페이지가 갈리므로 기록기를 다시 심는다.
    if args.url or args.inspect:
        print(f"  클릭 기록기: {ev(token, SPY_JS)}")

    if args.clicks:
        raw = ev(token, "JSON.stringify(window.__aadsClicks || [])")
        try:
            clicks = json.loads(raw)
        except Exception:
            print(f"  기록 없음 (기록기가 심어지지 않았거나 페이지가 새로고침됨): {str(raw)[:80]}")
            return 1
        if not clicks:
            print("  아직 클릭 기록이 없습니다.")
            return 0
        for c in clicks:
            mark = "사람" if c.get("trusted") else "프로그램"
            print(f"  [{c.get('t')}] {mark} {c.get('tag')} \"{c.get('text')}\"")
            print(f"      {c.get('path')}")
        return 0

    if args.inspect or args.find or args.url:
        raw = ev(token, INSPECT_JS)
        try:
            data = json.loads(raw)
        except Exception:
            print(f"  분석 실패: {str(raw)[:150]}")
            return 1
        items = data.get("items", [])
        if args.find:
            key = args.find.lower()
            items = [i for i in items if key in (i.get("text") or "").lower()]
        if args.json:
            print(json.dumps({**data, "items": items}, ensure_ascii=False, indent=2))
            return 0
        print(f"  뷰포트 {data.get('viewport')} · 조작 가능 요소 {data.get('count')}개"
              + (f" · '{args.find}' 일치 {len(items)}개" if args.find else ""))
        for i in items[:40]:
            where = "" if i.get("onScreen") else "  (화면 밖)"
            print(f"  - {i['kind']:14s} \"{i['text']}\"{where}")
            print(f"      {i['path']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
