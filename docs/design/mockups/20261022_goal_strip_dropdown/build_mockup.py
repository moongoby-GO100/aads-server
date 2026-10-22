#!/usr/bin/env python3
"""채팅 목표띠 드롭다운 전환 목업 생성기 (2026-10-22, AADS).

정적 HTML 시안(변경 후)과 현행 띠 재현(변경 전)을 만들고, 서버 Playwright chromium 으로
PNG 를 캡처한 뒤 목업 검토용 manifest.json(screens/assets/evidence)을 쓴다.

실행: cd docs/design/mockups/20261022_goal_strip_dropdown && timeout 300 python3 build_mockup.py
정본: PRD aads-goal-strip-dropdown-prd, 설계 명세 aads-goal-strip-dropdown-spec
"""
from __future__ import annotations

import datetime
import hashlib
import html
import json
import pathlib
import re

from playwright.sync_api import sync_playwright

ROOT = pathlib.Path(__file__).resolve().parent
REL = "docs/design/mockups/20261022_goal_strip_dropdown"
SOURCE_SHA = "35fe03f"  # aads-dashboard origin/main (현행 GoalStrip 기준)
E = html.escape

GROUP_LABEL = {
    "working": "🔄 작업중", "awaiting": "⏳ 대표님 대기", "blocked": "⛔ 막힘", "p0": "P0",
    "other": "P1 · P2", "paused": "⏸ 멈춤", "dormant": "💤 휴면·초안",
}
GROUP_ORDER = list(GROUP_LABEL)
NEW_ICON = {"working": "🔄", "awaiting": "⏳", "blocked": "⛔", "paused": "⏸", "dormant": "💤"}
OLD_GROUP_LABEL = {"working": "🔄 작업중", "awaiting": "⏳ 대표님 대기", "p0": "P0", "other": "P1 · P2", "dormant": "💤 휴면"}
OLD_GROUP_ORDER = list(OLD_GROUP_LABEL)


def g(t, pri, pct, grp, old, sub="", lead=False, aa=None, ms=None, st="active", kind=""):
    return {"t": t, "pri": pri, "pct": pct, "g": grp, "og": old, "sub": sub, "lead": lead,
            "aa": aa, "ms": ms, "st": st, "kind": kind}


# 이 대화(1fa84036) 연결 목표 6건 — DB 실측 2026-10-22 13:55 KST (for-session 과 같은 조인).
# 같은 그룹 안 순서는 goals.updated_at 최근순으로 대신했다(운영은 last_activity_at).
REAL = [
    g("지시서 활성화·최적화 — 설계 대화 검토부터 실행 검증까지", "P1", 10, "blocked", "other",
      "목표 상태 차단(blocked) — 사유·해제 조건은 목표 패널", st="blocked", kind="blk"),
    g("러너 검수 인프라 무응답 제거 — 승인 병목 해소", "P0", 20, "p0", "p0", lead=True),
    g("오비스 UI/UX 디자인 기획 체계 구축 및 프로젝트별 승인 디자인 적용", "P1", 0, "other", "other"),
    g("목표 승인설정 프로젝트별 분리 — 비매매 프로젝트 자동통과 차단·문구 정정·게이트 보호", "P1", 60, "other", "other",
      "M5 운영 릴리스·E2E 검증·비매매 목표 체크 정리", lead=True, ms="M5 운영 릴리스·E2E 검증·비매매 목표 체크 정리"),
    g("OHVIS 문서 저장 규칙(R-DOC) — 모든 세션의 저장 위치·파일명·정본 등록 일원화", "P1", 30, "other", "other"),
    g("목표 우선순위·휴면 정리·자동승인 표시 일치", "P1", 25, "other", "other"),
]
# 현행 띠 정렬(strip_rank → priority_rank → 제목) 그대로의 순서.
REAL_OLD = [REAL[1], REAL[4], REAL[3], REAL[5], REAL[2], REAL[0]]

# 7개 그룹을 모두 보이기 위한 예시(가상) 데이터.
ALL = [
    g("채팅 목표띠 드롭다운 전환", "P0", 40, "working", "working", "M2 화면 교체 · 🔄 러너 실행 중", lead=True,
      aa=("🟢 고위험까지 3/10", "auto"), ms="M2 화면 교체"),
    g("GO100 실매매 점검", "P1", 55, "awaiting", "awaiting", "⏳ 승인 대기 1건 — 대표님 결정 필요", aa=("⌛ 만료", "warn")),
    g("러너 검수 인프라 무응답 제거", "P0", 20, "blocked", "p0", "막힘: 담당 역할이 지정되지 않음", aa=("🔒 수동", ""), kind="blk"),
    g("지시서 활성화·최적화", "P1", 10, "blocked", "other", "목표 차단(blocked) · 3회 보냈으나 답이 확인되지 않음",
      aa=("🔒 수동", ""), st="blocked", kind="blk"),
    g("KIS 주문 검증", "P0", 64, "p0", "p0", "M3 주문 재시도 검증 · ⚠️ 8일 무활동", aa=("🟢 전체 4/20", "auto"), kind="idle"),
    g("목표 승인설정 프로젝트별 분리", "P1", 60, "other", "other", "M5 운영 릴리스·E2E 검증", lead=True, aa=("🔒 수동", "")),
    g("보고서 개편", "P2", 90, "other", "other"),
    g("디스크 확보", "P0", 22, "paused", "p0", "⏸ 멈춤 — 대표님 보류 지시 (10-21)", aa=("⏸ 정지", "paused"), kind="pz"),
    g("레거시 데이터 이관", "P1", 5, "dormant", "dormant", "💤 마지막 활동 16일 전", aa=("⏸ 정지", "paused")),
]
ALL_OLD = [ALL[0], ALL[1], ALL[4], ALL[7], ALL[2], ALL[5], ALL[3], ALL[6], ALL[8]]

CSS = """
*{box-sizing:border-box;margin:0;padding:0}
:root{--bg:#0F0F0F;--card:#1A1A1A;--border:#2A2A2A;--text:#E5E5E5;--sub:#8a8a8a;--side:#141414;--hover:#252525;--input:#1E1E1E}
body{background:var(--bg);color:var(--text);font:13px/1.45 "Noto Sans KR","WenQuanYi Zen Hei",system-ui,sans-serif}
.app{display:flex;width:1440px;height:820px;overflow:hidden}
.side{width:220px;flex:none;background:var(--side);border-right:1px solid var(--border);padding:14px 10px}
.side b{display:block;margin:0 6px 12px;font-size:14px}
.side .it{padding:8px 10px;border-radius:8px;color:var(--sub);margin-bottom:2px}
.side .it.on{background:var(--hover);color:var(--text)}
.main{flex:1;display:flex;flex-direction:column;min-width:0}
.top{height:48px;flex:none;display:flex;align-items:center;gap:10px;padding:0 18px;border-bottom:1px solid var(--border);font-weight:700}
.top small{color:var(--sub);font-weight:400}
.body{flex:1;display:flex;min-height:0}
.col{flex:1;display:flex;flex-direction:column;min-width:0;padding:0 24px}
.stripwrap{position:relative;padding:6px 0;border-bottom:1px solid var(--border);background:var(--bg);z-index:5}
.stream{flex:1;overflow:hidden;padding:16px 0;display:flex;flex-direction:column;gap:12px}
.msg{max-width:680px;padding:10px 14px;border-radius:12px;background:var(--card);border:1px solid var(--border)}
.msg.me{align-self:flex-end;background:#23263a;border-color:#33385a}
.msg small{display:block;color:var(--sub);font-size:11px;margin-bottom:3px}
.input{flex:none;margin:0 0 34px;height:52px;border:1px solid var(--border);border-radius:12px;background:var(--input);display:flex;align-items:center;padding:0 16px;color:var(--sub)}
.panel{width:380px;flex:none;border-left:1px solid var(--border);background:var(--card);padding:16px}
.panel h3{font-size:14px;display:flex;gap:6px;align-items:center}
.pmeta{color:var(--sub);font-size:11.5px;margin:6px 0 8px}
.pbar{height:6px;border-radius:3px;background:#2f3340;overflow:hidden;margin-bottom:14px}.pbar i{display:block;height:100%;background:#3b82f6}
.plab{font-size:11px;color:var(--sub);margin:4px 0 6px;font-weight:700}
.pms{padding:7px 0;border-bottom:1px solid var(--border);font-size:12.5px}
.pr{flex:none;padding:0 5px;border-radius:4px;font-size:9.5px;font-weight:800;line-height:15px;color:#fff}
.pr.p0{background:#b91c1c}.pr.p1{background:#c2410c}.pr.p2{background:#475569}
.striprow{display:flex;align-items:center;gap:8px}
.striprow .sp{flex:1}
.trig{display:inline-flex;align-items:center;gap:7px;height:32px;max-width:660px;padding:0 10px 0 12px;border-radius:999px;border:1px solid var(--border);background:var(--card);color:var(--text);font-size:12px;white-space:nowrap}
.trig.on{border-color:#3b82f6;box-shadow:0 0 0 2px rgba(59,130,246,.25)}
.cnt{flex:none;padding:0 6px;border-radius:999px;font-size:10.5px;font-weight:700;line-height:17px}
.cnt.working{background:rgba(59,130,246,.18);color:#93c5fd}.cnt.awaiting{background:rgba(245,158,11,.16);color:#fbbf24}.cnt.blocked{background:rgba(220,38,38,.18);color:#fca5a5}.cnt.link{border:1px dashed var(--border);color:var(--sub)}
.vs{flex:none;width:1px;height:16px;background:#3a3a3a}
.trig .tt{overflow:hidden;text-overflow:ellipsis;font-weight:700;min-width:0}
.bar{flex:none;display:inline-block;width:48px;height:4px;border-radius:2px;background:#2f3340;overflow:hidden}
.bar i{display:block;height:100%;background:#3b82f6}
.pct{flex:none;color:var(--sub);font-size:11px;min-width:28px;text-align:right}
.car{flex:none;color:var(--sub);font-size:11px}
.chip2{flex:none;padding:3px 10px;border-radius:999px;font-size:11px;font-weight:700;white-space:nowrap;border:1px solid var(--border);color:var(--text);background:var(--card)}
.chip2.tidy{border-color:rgba(129,140,248,.7);color:#c7d2fe;background:rgba(99,102,241,.16)}
.chip2.err{border-color:rgba(245,158,11,.7);color:#f59e0b;background:rgba(245,158,11,.10)}
.chip2.sum{font-weight:600}.chip2.more{border-style:dashed;color:var(--sub);background:transparent;font-weight:400}
.dd{position:absolute;left:0;top:44px;width:440px;max-height:520px;overflow:hidden;background:#161616;border:1px solid #363636;border-radius:10px;box-shadow:0 12px 32px rgba(0,0,0,.6);z-index:30;display:flex;flex-direction:column}
.ddb{overflow:hidden;flex:1}
.ddh{display:flex;align-items:center;gap:6px;padding:10px 12px 8px;border-bottom:1px solid var(--border);font-weight:700;font-size:12.5px}
.ddh small{color:var(--sub);font-weight:400}
.grp{display:flex;align-items:center;gap:6px;height:26px;padding:0 12px;font-size:11px;color:var(--sub);font-weight:700;background:#121212;border-bottom:1px solid var(--border)}
.grp .n{color:var(--text)}
.row{display:flex;align-items:center;gap:7px;min-height:44px;padding:6px 12px;border-bottom:1px solid #222}
.row.sel{background:rgba(37,99,235,.14);box-shadow:inset 3px 0 0 #3b82f6}
.row.focus{outline:2px solid #60a5fa;outline-offset:-2px}
.row .ic{flex:none;width:18px;text-align:center;font-size:12px;color:var(--sub)}
.row .bd{flex:1;min-width:0}
.row .t{display:flex;align-items:center;gap:5px;font-weight:600;font-size:12.5px;min-width:0}
.row .t span.x{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.row .s{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:11px;color:var(--sub);margin-top:1px}
.row .s.blk{color:#fca5a5}.row .s.pz{color:#cbd5e1}.row .s.idle{color:#f59e0b}
.lead{flex:none;font-size:10px;color:#fbbf24}
.aa{flex:none;padding:1px 7px;border-radius:999px;font-size:10.5px;font-weight:700;white-space:nowrap;border:1px solid var(--border);color:var(--sub)}
.aa.auto{color:#34d399;border-color:rgba(52,211,153,.55);background:rgba(16,185,129,.10)}
.aa.warn{color:#f59e0b;border-color:rgba(245,158,11,.65);background:rgba(245,158,11,.10)}
.aa.paused{background:rgba(148,163,184,.12)}
.ddf{flex:none;display:flex;align-items:center;gap:8px;padding:9px 12px;background:#161616;border-top:1px solid var(--border);font-size:11.5px}
.ddf a{margin-left:auto;color:#93c5fd;text-decoration:none}
.muted{color:var(--sub)}
.warnline{flex:none;padding:9px 12px;color:#f59e0b;font-size:11.5px;border-top:1px solid var(--border);display:flex;align-items:center;gap:8px}
.btn{flex:none;padding:3px 10px;border-radius:7px;border:1px solid var(--border);background:transparent;color:var(--text);font:inherit;font-size:11.5px}
.empty{padding:26px 18px;color:var(--sub);font-size:12.5px;line-height:1.7;text-align:center}
.sk{display:inline-block;height:10px;border-radius:5px;background:linear-gradient(90deg,#232323,#313131,#232323)}
.ochips{display:flex;align-items:center;gap:6px;min-width:0;flex:1;overflow:hidden}
.ochips.open{flex-wrap:wrap;max-height:290px}
.ochip{display:inline-flex;align-items:center;gap:5px;flex:none;max-width:360px;padding:3px 7px 3px 9px;border-radius:999px;background:var(--card);font-size:11px;white-space:nowrap}
.ochip b{overflow:hidden;text-overflow:ellipsis}
.oghd{flex:none;padding-left:8px;margin-left:2px;border-left:1px solid var(--border);font-size:10.5px;color:var(--sub);white-space:nowrap}
.fnote{position:fixed;left:0;right:0;bottom:0;padding:5px 12px;font-size:10.5px;color:#b5b5b5;background:rgba(24,24,24,.96);border-top:1px solid var(--border);z-index:60}
.mob{width:390px;height:844px;display:flex;flex-direction:column;position:relative;overflow:hidden}
.m-top{height:48px;flex:none;display:flex;align-items:center;gap:8px;padding:0 14px;border-bottom:1px solid var(--border);font-weight:700}
.m-strip{position:relative;flex:none;padding:6px 10px;border-bottom:1px solid var(--border)}
.m-strip .trig{width:100%;max-width:none;height:36px}
.m-strip .trig .tt{flex:1}
.m-stream{flex:1;padding:12px;display:flex;flex-direction:column;gap:10px;overflow:hidden}
.m-stream .msg{max-width:320px}
.m-input{flex:none;margin:0 10px 34px;height:46px;border:1px solid var(--border);border-radius:12px;background:var(--input);display:flex;align-items:center;padding:0 14px;color:var(--sub)}
.scrim{position:absolute;inset:0;background:rgba(0,0,0,.55);z-index:40}
.sheet{position:absolute;left:0;right:0;bottom:0;max-height:70%;overflow:hidden;background:#161616;border-top:1px solid #3a3a3a;border-radius:14px 14px 0 0;z-index:41;box-shadow:0 -8px 24px rgba(0,0,0,.45);padding-bottom:26px;display:flex;flex-direction:column}
.sheet .grab{flex:none;width:40px;height:4px;border-radius:2px;background:#444;margin:8px auto 2px}
.sheet .row{min-height:52px}
.sheet .ddh .x{margin-left:auto;font-size:16px;color:var(--sub);padding:0 4px}
.orows{max-height:390px;overflow:hidden;border-top:1px solid var(--border)}
.orow{display:flex;align-items:center;gap:6px;min-height:35px;padding:0 10px;border-bottom:1px solid var(--border);font-size:12.5px;white-space:nowrap}
.orow .x{overflow:hidden;text-overflow:ellipsis}
.oghm{height:22px;display:flex;align-items:center;padding:0 10px;font-size:10.5px;color:var(--sub)}
"""

MSGS_DESKTOP = (
    '<div class="msg me">채팅창의 목표띠를 드롭박스 버튼으로 바꾸고 진행우선순으로 보이게 바꾸면 어때?</div>'
    '<div class="msg"><small>오비스 · 13:48</small>✅ 권장합니다. 한 줄 요약 버튼 + 진행 우선순 드롭다운이 채팅 공간과 판단 속도 모두 개선됩니다.</div>'
    '<div class="msg me">기획, 설계, PRD, 목업 작성 저장하고 반영해</div>'
)
MSGS_MOBILE = (
    '<div class="msg me">목표띠를 드롭박스로 바꾸고 진행우선순으로 보이게 해줘</div>'
    '<div class="msg"><small>오비스 · 13:48</small>✅ 한 줄 버튼 + 진행 우선순 목록으로 바꿉니다.</div>'
)


def short_title(t: str, n: int = 12) -> str:
    """src/lib/goalStrip.ts shortGoalTitle 과 같은 규칙."""
    t = t.strip()
    m = re.match(r"^#\s*(\d+)", t)
    if m:
        return "#" + m.group(1)
    t2 = re.sub(r"\s*[（(][^)）]*[)）]\s*$", "", t).strip()
    return t2[:n] + "…" if len(t2) > n else t2


def badge(pri: str) -> str:
    tone = "p0" if pri.startswith("P0") else "p1" if pri == "P1" else "p2"
    return f'<span class="pr {tone}">{E(pri)}</span>'


def bar(pct: int) -> str:
    return f'<span class="bar"><i style="width:{pct}%"></i></span><span class="pct">{pct}%</span>'


def row_icon(x: dict) -> str:
    return NEW_ICON.get(x["g"], "●")


# ---------------------------------------------------------------- 변경 후

def counts_html(goals: list[dict]) -> str:
    out = []
    for key, icon in (("working", "🔄"), ("awaiting", "⏳"), ("blocked", "⛔")):
        n = sum(1 for x in goals if x["g"] == key)
        if n:
            out.append(f'<span class="cnt {key}">{icon}{n}</span>')
    return "".join(out)


def trigger(goals: list[dict], *, open_: bool, mobile: bool, extra: str = "") -> str:
    on = " on" if open_ else ""
    car = "▴" if open_ else "▾"
    label = f"🎯 {len(goals)}" if mobile else f"🎯 <b>목표 {len(goals)}</b>"
    if not goals:
        return f'<span class="trig{on}">{label}{extra}<span class="car">{car}</span></span>'
    top = goals[0]
    name = short_title(top["t"], 12) if mobile else short_title(top["t"], 26)
    pct = "" if mobile else bar(top["pct"])
    return (f'<span class="trig{on}" role="button" aria-haspopup="listbox" aria-expanded="{str(open_).lower()}">'
            f'{label}{counts_html(goals)}{extra}<span class="vs"></span><span>{row_icon(top)}</span>{badge(top["pri"])}'
            f'<span class="tt">{E(name)}</span>{pct}<span class="car">{car}</span></span>')


def dd_rows(goals: list[dict], *, selected: str | None = None, focus: str | None = None, aa: bool = True,
            mobile: bool = False) -> str:
    out = []
    for key in GROUP_ORDER:
        members = [x for x in goals if x["g"] == key]
        if not members:
            continue
        out.append(f'<div class="grp">{E(GROUP_LABEL[key])} <span class="n">{len(members)}</span></div>')
        for x in members:
            cls = "row" + (" sel" if x["t"] == selected else "") + (" focus" if x["t"] == focus else "")
            lead = '<span class="lead">👑 주도</span>' if x["lead"] else ""
            sub = x["sub"]
            if mobile and x["kind"] not in ("blk", "pz"):
                sub = ""
            sub_html = f'<div class="s {x["kind"]}">{E(sub)}</div>' if sub else ""
            aa_html = ""
            if aa and x["aa"] and not mobile:
                aa_html = f'<span class="aa {x["aa"][1]}">{E(x["aa"][0])}</span>'
            out.append(
                f'<div class="{cls}" role="option" aria-selected="{str(x["t"] == selected).lower()}">'
                f'<span class="ic">{row_icon(x)}</span>{badge(x["pri"])}'
                f'<div class="bd"><div class="t"><span class="x">{E(x["t"])}</span>{lead}</div>{sub_html}</div>'
                f'{bar(x["pct"])}{aa_html}</div>')
    return "".join(out)


def dd_head(n: int, mobile: bool) -> str:
    x = '<span class="x">✕</span>' if mobile else ""
    return (f'<div class="ddh">이 대화의 목표 {n} <small>· 전체 열린 목표 58 · P0 30</small>{x}</div>')


def dd_foot(cleanup: bool = True, unlinked: int = 0) -> str:
    parts = []
    if cleanup:
        parts.append('<span class="chip2 tidy">🧹 휴면·차단 정리 18건</span>')
    if unlinked:
        parts.append(f'<span class="muted">🔗 목표 미연결 {unlinked}건</span>')
    parts.append('<a>목표 화면 전체 보기 →</a>')
    return f'<div class="ddf">{"".join(parts)}</div>'


def desktop(strip: str, note: str, panel: str = "") -> str:
    return (
        '<div class="app"><div class="side"><b>AADS 채팅</b><div class="it on">CEO 통합지시</div>'
        '<div class="it">AADS 러너전문가</div><div class="it">GO100 장마감</div><div class="it">KIS 자동매매</div>'
        '<div class="it">NTV2 알림</div><div class="it">SF 정산</div></div>'
        '<div class="main"><div class="top">통합 지휘 세션 <small>CEO · AADS</small></div><div class="body"><div class="col">'
        f'<div class="stripwrap">{strip}</div><div class="stream">{MSGS_DESKTOP}</div>'
        f'<div class="input">메시지를 입력하세요…</div></div>{panel}</div></div></div>'
        f'<div class="fnote">{E(note)}</div>')


def mobile(strip: str, note: str, sheet: str = "") -> str:
    return (
        '<div class="mob"><div class="m-top">☰ 통합 지휘 세션</div>'
        f'<div class="m-strip">{strip}</div><div class="m-stream">{MSGS_MOBILE}</div>'
        f'<div class="m-input">메시지를 입력하세요…</div>{sheet}<div class="fnote">{E(note)}</div></div>')


def page(title: str, body: str) -> str:
    return (f'<!doctype html><html lang="ko"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1"><title>{E(title)}</title>'
            f'<style>{CSS}</style></head><body>{body}</body></html>')


NOTE_REAL = "시안 — 목표 6건·전체 58·P0 30 은 2026-10-22 13:55 KST DB 실측. 🧹 정리 18건은 예시. 같은 그룹 안 순서는 마지막 활동순."
NOTE_ALL = "시안 — 7개 그룹을 모두 보이기 위한 예시(가상) 데이터. 오른쪽은 줄을 눌러 열린 목표 패널."


def new_closed(goals, mob=False):
    if mob:
        return mobile(f'<div class="striprow">{trigger(goals, open_=False, mobile=True)}</div>', NOTE_REAL)
    strip = (f'<div class="striprow">{trigger(goals, open_=False, mobile=False)}<span class="sp"></span>'
             '<span class="chip2 tidy">🧹 휴면·차단 정리 18</span></div>')
    return desktop(strip, NOTE_REAL)


def new_open(goals, mob=False, note=NOTE_REAL, selected=None, panel="", focus=None):
    if mob:
        sheet = (f'<div class="scrim"></div><div class="sheet" role="dialog" aria-label="이 대화의 목표">'
                 f'<div class="grab"></div>{dd_head(len(goals), True)}'
                 f'<div class="ddb" role="listbox">{dd_rows(goals, mobile=True)}</div>{dd_foot()}</div>')
        return mobile(f'<div class="striprow">{trigger(goals, open_=True, mobile=True)}</div>', note, sheet)
    dd = (f'<div class="dd" role="dialog">{dd_head(len(goals), False)}'
          f'<div class="ddb" role="listbox">{dd_rows(goals, selected=selected, focus=focus)}</div>{dd_foot()}</div>')
    strip = (f'<div class="striprow">{trigger(goals, open_=True, mobile=False)}<span class="sp"></span>'
             f'<span class="chip2 tidy">🧹 휴면·차단 정리 18</span></div>{dd}')
    return desktop(strip, note, panel)


def new_loading():
    strip = ('<div class="striprow"><span class="trig" aria-busy="true">🎯 <span class="muted">목표 불러오는 중…</span>'
             '<span class="sk" style="width:90px"></span><span class="sk" style="width:48px;height:4px"></span></span></div>')
    return desktop(strip, "시안 — 처음 불러오는 동안 버튼 자리를 골격으로 잡아 대화가 아래로 밀리지 않게 한다.")


def new_empty():
    extra = '<span class="cnt link">🔗 미연결 2</span>'
    dd = (f'<div class="dd" role="dialog">{dd_head(0, False)}'
          '<div class="empty">이 대화에 연결된 목표가 없습니다.<br>목표 화면에서 이 대화를 담당으로 연결하면 여기에 보입니다.</div>'
          f'{dd_foot(cleanup=True, unlinked=2)}</div>')
    strip = (f'<div class="striprow">{trigger([], open_=True, mobile=False, extra=extra)}<span class="sp"></span>'
             f'<span class="chip2 tidy">🧹 휴면·차단 정리 18</span></div>{dd}')
    return desktop(strip, "시안 — 연결 목표 0건. 정리 후보나 미연결 러너가 있을 때만 버튼이 보인다(없으면 띠 자체를 그리지 않음, 기존 규칙).")


def new_error(goals):
    dd = (f'<div class="dd" role="dialog">{dd_head(len(goals), False)}'
          f'<div class="ddb" role="listbox">{dd_rows(goals, aa=False)}</div>'
          '<div class="warnline">⚠️ 자동승인·정리 상태를 불러오지 못했습니다 <span class="btn">다시 시도</span></div>'
          '<div class="ddf"><a>목표 화면 전체 보기 →</a></div></div>')
    strip = (f'<div class="striprow">{trigger(goals, open_=True, mobile=False)}<span class="sp"></span>'
             '<span class="chip2 err">⚠️ 자동승인·정리 상태 확인 실패</span></div>' + dd)
    return desktop(strip, "시안 — 신호(GET /goals) 실패. 목표 목록은 그대로 보이고 자동승인 칩·정리 건수만 빠진다.")


# ---------------------------------------------------------------- 변경 전(현행 GoalStrip 재현)

def old_chip(x: dict) -> str:
    color = "#d97706" if x["lead"] else ("#64748b" if x["st"] != "active" else "#2563eb")
    icon = {"working": "🔄", "awaiting": "⏳", "dormant": "💤"}.get(x["og"]) or ("👑" if x["lead"] else "●")
    style = "dashed" if x["og"] == "dormant" else "solid"
    ms = f'<span style="max-width:120px;overflow:hidden;text-overflow:ellipsis">· {E(x["ms"])}</span>' if x["lead"] and x["ms"] else ""
    aa = f'<span class="aa {x["aa"][1]}">{E(x["aa"][0])}</span>' if x["aa"] else ""
    return (f'<span class="ochip" style="border:{2 if x["lead"] else 1}px {style} {color}"><span>{icon}</span>{badge(x["pri"])}'
            f'<b style="color:{color}">{E(short_title(x["t"]))}</b><span class="muted">{x["pct"]}%</span>{ms}</span>{aa}')


def old_groups(goals: list[dict]) -> list[tuple[str, list[dict]]]:
    return [(k, [x for x in goals if x["og"] == k]) for k in OLD_GROUP_ORDER if any(x["og"] == k for x in goals)]


def old_closed(goals, mob=False):
    shown = goals[:4]
    more = f'<span class="chip2 more">목표 +{len(goals) - len(shown)}</span>' if len(goals) > 4 else ""
    chips = "".join(old_chip(x) for x in shown)
    if mob:
        strip = f'<div class="striprow"><div class="ochips"><span class="chip2 sum">열린 58 · P0 30</span>{chips}</div></div>'
        return mobile(strip, "변경 전(현행 재현) — 칩이 가로로 이어져 화면 밖으로 잘린다.")
    strip = (f'<div class="striprow"><div class="ochips"><span class="chip2 sum">열린 목표 58 · P0 <b style="color:#f59e0b">30</b></span>{chips}</div>'
             f'<span class="chip2 tidy">🧹 휴면·차단 정리 18</span>{more}</div>')
    return desktop(strip, "변경 전(현행 재현, GoalStrip.tsx 35fe03f) — 접힌 상태 칩 4개 + 목표 +N. 주도·막힘·작업중 목표는 4개를 넘어도 강제로 보인다.")


def old_open(goals, mob=False, note="변경 전(현행 재현) — 펼치면 칩이 줄바꿈되어 최대 290px 까지 대화를 밀어낸다."):
    if mob:
        rows = []
        for key, members in old_groups(goals):
            rows.append(f'<div class="oghm">{E(OLD_GROUP_LABEL[key])} {len(members)}</div>')
            for x in members:
                icon = {"working": "🔄", "awaiting": "⏳", "dormant": "💤"}.get(x["og"]) or ("👑" if x["lead"] else "●")
                rows.append(f'<div class="orow"><span>{icon}</span>{badge(x["pri"])}<span class="x">{E(short_title(x["t"]))}</span>'
                            f'<span class="muted" style="font-size:11px">{x["pct"]}%</span></div>')
        strip = ('<div class="striprow" style="flex-wrap:wrap"><span class="chip2 sum">열린 58 · P0 30</span>'
                 '<span class="sp"></span><span class="chip2 tidy">🧹 정리 18</span><span class="chip2 more">접기</span></div>'
                 f'<div class="orows">{"".join(rows)}</div>')
        return mobile(strip, "변경 전(현행 재현) — 모바일 펼침은 목록이 대화 위를 46vh 까지 밀어낸다.")
    parts = ['<span class="chip2 sum">열린 목표 58 · P0 <b style="color:#f59e0b">30</b></span>']
    for key, members in old_groups(goals):
        parts.append(f'<span class="oghd">{E(OLD_GROUP_LABEL[key])} {len(members)}</span>')
        parts.extend(old_chip(x) for x in members)
    strip = (f'<div class="striprow" style="align-items:flex-start"><div class="ochips open">{"".join(parts)}</div>'
             '<span class="chip2 tidy">🧹 휴면·차단 정리 18</span><span class="chip2 more">접기</span></div>')
    return desktop(strip, note)


PANEL_ALL = (
    '<div class="panel"><h3>채팅 목표띠 드롭다운 전환 <span class="pr p0">P0</span></h3>'
    '<div class="pmeta">active · 진행 40% · 🔄 러너 실행 중 · 👑 이 대화가 주도</div>'
    '<div class="pbar"><i style="width:40%"></i></div><div class="plab">마일스톤</div>'
    '<div class="pms">✅ M1 기획·PRD·설계·목업</div><div class="pms">🔄 M2 서버 계약·화면 교체</div>'
    '<div class="pms">⬜ M3 운영 릴리스·화면 검증</div></div>')

# (파일 이름, 뷰포트, phase, state, fixture, html, capture_source)
DESK, MOBV = (1440, 820), (390, 844)
SPECS = [
    ("goal-dropdown_closed", DESK, "mockup", "default", "session-closed", lambda: new_closed(REAL)),
    ("goal-dropdown_closed_mobile", MOBV, "mockup", "default", "session-closed", lambda: new_closed(REAL, True)),
    ("goal-dropdown_open", DESK, "mockup", "default", "session-open", lambda: new_open(REAL)),
    ("goal-dropdown_open_mobile", MOBV, "mockup", "default", "session-open", lambda: new_open(REAL, True)),
    ("goal-dropdown_all-groups", DESK, "mockup", "default", "all-groups-open",
     lambda: new_open(ALL, note=NOTE_ALL, selected=ALL[0]["t"], panel=PANEL_ALL, focus=ALL[1]["t"])),
    ("goal-dropdown_loading", DESK, "mockup", "loading", "session-loading", new_loading),
    ("goal-dropdown_empty", DESK, "mockup", "empty", "session-empty", new_empty),
    ("goal-dropdown_error", DESK, "mockup", "error", "session-error", lambda: new_error(REAL)),
    ("goal-strip_before_closed", DESK, "before", "default", "session-closed", lambda: old_closed(REAL_OLD)),
    ("goal-strip_before_closed_mobile", MOBV, "before", "default", "session-closed", lambda: old_closed(REAL_OLD, True)),
    ("goal-strip_before_open", DESK, "before", "default", "session-open", lambda: old_open(REAL_OLD)),
    ("goal-strip_before_open_mobile", MOBV, "before", "default", "session-open", lambda: old_open(REAL_OLD, True)),
    ("goal-strip_before_all-groups", DESK, "before", "default", "all-groups-open",
     lambda: old_open(ALL_OLD, note="변경 전(현행 재현) — 같은 예시 데이터. 멈춘 P0(디스크 확보)·막힌 목표가 P0·P1 사이에 섞인다.")),
]


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def main() -> None:
    (ROOT / "html").mkdir(exist_ok=True)
    (ROOT / "png").mkdir(exist_ok=True)
    captured = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
    assets, children = [], []
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        for name, (w, h), phase, state, fixture, build in SPECS:
            body = page(name, build())
            html_path = ROOT / "html" / f"{name}.html"
            html_path.write_text(body, encoding="utf-8")
            png_path = ROOT / "png" / f"{name}_{w}x{h}.png"
            pg = browser.new_page(viewport={"width": w, "height": h}, device_scale_factor=1)
            pg.goto(html_path.as_uri())
            pg.wait_for_timeout(150)
            pg.screenshot(path=str(png_path), full_page=False)
            pg.close()
            data = png_path.read_bytes()
            assets.append({
                "asset_id": f"{phase}-{name}-{w}x{h}"[:120], "role": "primary",
                "uri": f"internal://{REL}/png/{png_path.name}", "sha256": sha(data), "byte_size": len(data),
                "mime": "image/png", "screen_id": "goal-dropdown", "phase": phase, "viewport": f"{w}x{h}",
                "fixture_id": fixture, "state": state, "captured_at": captured.isoformat(),
                "capture_source": "static_html_render" if phase == "mockup" else "source_reconstruction",
                "redaction_status": "not_required",
            })
            hdata = html_path.read_bytes()
            children.append({"asset_id": f"html-{name}", "role": "child",
                             "uri": f"internal://{REL}/html/{html_path.name}", "sha256": sha(hdata),
                             "byte_size": len(hdata), "mime": "text/html"})
        browser.close()
    manifest = {
        "screens": [{
            "screen_id": "goal-dropdown",
            "title": "채팅 상단 목표띠 — 한 줄 드롭다운 버튼 + 진행 우선순 목록",
            "route": "/chat",
            "requirement_ids": ["GD-1", "GD-2", "GD-3", "GD-4", "GD-5", "GD-8", "GD-9", "GD-10"],
            "states_not_applicable": {
                "permission": "채팅 화면 안의 요소라 권한 오류 전용 화면이 없고 기존 채팅 오류 처리를 따른다",
                "session-expired": "로그인 만료는 공통 로그인 화면으로 이동하며 이 요소 고유 화면이 없다",
                "offline": "오프라인 전용 화면이 없고 기존 채팅 연결 오류 표시를 쓴다",
            },
        }],
        "assets": assets + children,
        "evidence": [{
            "evidence_id": "render-goal-dropdown", "kind": "browser_capture", "screen_id": "goal-dropdown",
            "route": "/chat", "success": True, "login_used": False,
            "detail": ("서버 Playwright chromium 으로 정적 HTML 시안 8장(1440x820·390x844, default·loading·empty·error)과 "
                       "현행 GoalStrip 재현 5장을 캡처. 운영 /chat 은 로그인이 필요해 변경 전은 소스 재현. "
                       "실측: 이 대화 목표 6·전체 열린 58·P0 30(2026-10-22 13:55 KST DB)."),
            "recorded_at": captured.isoformat(),
        }],
        "source_sha": SOURCE_SHA,
    }
    (ROOT / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({"png": len(assets), "html": len(children), "manifest": str(ROOT / "manifest.json")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
