#!/usr/bin/env python3
"""목표 카드 현재 단계 진행률·진행 상태 표시 목업 (2026-10-28, AADS).

정적 HTML 시안을 만들고 서버 Playwright chromium 으로 PNG 를 캡처해 manifest.json 을 쓴다.
예시 값은 2026-10-28 08:2x KST 운영 DB 의 실제 진행 중 목표에서 가져왔다(제목은 줄임).

실행: cd docs/design/mockups/20261028_goal_current_milestone && timeout 300 python3 build_mockup.py
정본: PRD aads-goal-card-current-milestone-prd
"""
from __future__ import annotations

import datetime
import hashlib
import html
import json
import pathlib

from playwright.sync_api import sync_playwright

ROOT = pathlib.Path(__file__).resolve().parent
REL = "mockup_assets/20261028_goal_current_milestone"  # 영구 위치 app/data/mockup_assets (재배포에도 유지)
SOURCE_SHA = "7c58254"  # aads-dashboard origin/main
E = html.escape

STATE = {
    "working": ("진행중", "#2563eb"), "no_owner": ("담당 없음", "#d97706"),
    "awaiting_reply": ("답 대기", "#d97706"), "answered_no_progress": ("답했으나 진행 없음", "#dc2626"),
    "blocked": ("막힘", "#dc2626"), "paused": ("멈춤", "#64748b"), "idle": ("대기", "#64748b"),
}

GOALS = [
    {"order": 1, "pri": "P0", "proj": "FOOD", "title": "오비서 인사관리(HR) 완성 — 노무 리스크 방어", "pct": 0,
     "ms": {"seq": 1, "total": 20, "title": "근태 서버화", "done": 0, "prog": 0, "items": 0, "state": "blocked",
            "reason": "3회 보냈으나 답이 확인되지 않음", "at": "10-27 21:40", "other": 0}},
    {"order": 2, "pri": "P1", "proj": "AADS", "title": "오비서 직원 계약서 확인·서명 포털", "pct": 67,
     "ms": {"seq": 1, "total": 3, "title": "직원 진입 분리", "done": 2, "prog": 2, "items": 6, "state": "no_owner",
            "reason": "담당 세션 없음 — 목표 주도 세션으로 대체 발송", "at": "", "other": 0}},
    {"order": 3, "pri": "P1", "proj": "AADS", "title": "업무 자동화 대화형 등록", "pct": 44,
     "ms": {"seq": 4, "total": 9, "title": "대신 시연(저장 단계 정지·실패 6분류)", "done": 0, "prog": 0, "items": 7,
            "state": "awaiting_reply", "reason": "1회 보냄 — 답 대기", "at": "", "other": 0}},
    {"order": 4, "pri": "P1", "proj": "AADS", "title": "오비서 계정·조직 삭제 방지 및 로그인 회귀 검증", "pct": 50,
     "ms": {"seq": 2, "total": 2, "title": "실사용 로그인·조직·권한 회귀 인증", "done": 0, "prog": 0, "items": 0,
            "state": "working", "reason": "러너 작업 진행 중", "at": "", "other": 0}},
    {"order": 5, "pri": "P1", "proj": "GO100", "title": "일봉 파동 추세 스윙 전략카드", "pct": 38,
     "ms": {"seq": 4, "total": 6, "title": "백테스트 합격 판정", "done": 3, "prog": 1, "items": 5,
            "state": "answered_no_progress", "reason": "담당이 답했으나 진행(러너·완료 보고)이 없음 — 확인된 답 07:12 KST",
            "at": "10-28 07:58", "other": 2}},
]

CSS = """
*{box-sizing:border-box;margin:0;padding:0;font-family:-apple-system,'Pretendard','Noto Sans KR',sans-serif}
body{background:#f1f5f9;color:#0f172a;padding:20px}
h1{font-size:16px;margin-bottom:12px} .sub{font-size:12px;color:#64748b;margin-bottom:14px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(400px,1fr));gap:12px}
.card{background:#fff;border:1px solid #e2e8f0;border-radius:12px;padding:14px;display:flex;gap:10px}
.main{flex:1;min-width:0}.meta{display:flex;justify-content:space-between;font-size:11px;font-weight:800;color:#475569}
.title{font-size:14px;font-weight:700;margin:6px 0}.bar{height:6px;background:#e2e8f0;border-radius:3px;overflow:hidden}
.bar i{display:block;height:100%;background:#2563eb}.pct{font-size:11px;color:#64748b;margin-top:4px}
.cm{margin-top:10px;padding-top:10px;border-top:1px dashed #e2e8f0;font-size:12px;display:flex;flex-wrap:wrap;gap:6px;align-items:center}
.cm b{font-weight:700}.mini{width:60px;height:4px;background:#e2e8f0;border-radius:2px;overflow:hidden;display:inline-block}
.mini i{display:block;height:100%;background:#0ea5e9}.chip{border:1px solid;border-radius:999px;padding:2px 8px;font-weight:700;cursor:pointer;min-height:24px}
.ord{display:flex;flex-direction:column;gap:6px}.ord button{width:44px;height:44px;border:1px solid #cbd5e1;border-radius:10px;background:#fff;font-size:16px}
.open{margin-top:8px;padding:8px 10px;border:1px solid #dc2626;border-radius:8px;font-size:11.5px;color:#7f1d1d;background:#fef2f2}
.btns{display:flex;gap:8px;margin-top:8px;flex-wrap:wrap}.btns button{min-height:44px;padding:0 14px;border-radius:8px;font-weight:700;font-size:12px}
.p{background:#2563eb;color:#fff;border:0}.s{background:#fff;border:1px solid #cbd5e1}
.sk{background:linear-gradient(90deg,#e2e8f0,#f1f5f9,#e2e8f0);border-radius:6px;height:14px}
.note{font-size:11px;color:#64748b;margin-top:14px;line-height:1.6}
@media(max-width:480px){.grid{grid-template-columns:1fr}.cm{font-size:11.5px}}
"""


CM_OPEN = '<div class="cm">'
CM_INLINE = '<div class="cm" style="display:inline-flex;margin:0;padding:0;border:0">'


def cm_line(ms, expanded=False):
    label, color = STATE[ms["state"]]
    head = f'<b>M{ms["seq"]}</b> {E(ms["title"])}'
    if ms["items"]:
        pct = round(100 * ms["done"] / ms["items"])
        prog = f' · 진행 {ms["prog"]}' if ms["prog"] else ""
        rate = (f'<span>작업 {ms["done"]}/{ms["items"]} ({pct}%){prog}</span>'
                f'<span class="mini"><i style="width:{pct}%"></i></span>')
    else:
        rate = f'<span>단계 {ms["seq"]}/{ms["total"]}</span>'
    other = f'<span style="color:#64748b">외 {ms["other"]}개</span>' if ms["other"] else ""
    chip = f'<span class="chip" style="color:{color};border-color:{color}">진행: {label} ▾</span>'
    out = f'<div class="cm">{head}<span>·</span>{rate}{other}<span>·</span>{chip}</div>'
    if expanded:
        at = f' · 막힌 시각 {E(ms["at"])} KST' if ms["at"] else ""
        out += (f'<div class="open">{E(ms["reason"])}{at}<div class="btns">'
                '<button class="p">다시 보내기</button><button class="s">담당 지정</button></div></div>')
    return out


def card(gl, expanded=False, before=False):
    line = "" if before else cm_line(gl["ms"], expanded)
    return (f'<div class="card"><div class="main"><div class="meta"><span>순서 {gl["order"]} · {gl["proj"]} · {gl["pri"]}</span>'
            f'<span>진행 중</span></div><div class="title">{E(gl["title"])}</div>'
            f'<div class="bar"><i style="width:{gl["pct"]}%"></i></div><div class="pct">목표 전체 {gl["pct"]}%</div>'
            f'{line}</div><div class="ord"><button>▲</button><button>▼</button></div></div>')


def page(title, body):
    return f'<!doctype html><html lang="ko"><head><meta charset="utf-8"><title>{E(title)}</title><style>{CSS}</style></head><body>{body}</body></html>'


def default_view():
    cards = "".join(card(gl, expanded=(i == 4)) for i, gl in enumerate(GOALS))
    return (f'<h1>목표 — 진행 중 {len(GOALS)}</h1><div class="sub">카드 하단: 현재 단계 · 진행률(하위 작업 기준) · 진행 상태. '
            f'하위 작업이 없으면 % 대신 단계 순번.</div><div class="grid">{cards}</div>'
            '<div class="note">예시 값: 2026-10-28 08:2x KST 운영 DB 진행 중 목표(제목 줄임). 다섯 번째 카드는 진행 상태를 눌러 펼친 모습.</div>')


def before_view():
    cards = "".join(card(gl, before=True) for gl in GOALS)
    return (f'<h1>목표 — 진행 중 {len(GOALS)} (현행)</h1><div class="sub">현행 카드: 목표 전체 진행률만 보이고 현재 단계·진행 상태가 없다 '
            '(aads-dashboard 7c58254 src/app/goals/page.tsx 카드 재현).</div>'
            f'<div class="grid">{cards}</div>')


def loading_view():
    sk = '<div class="card"><div class="main"><div class="sk" style="width:40%"></div><div class="sk" style="margin:8px 0;width:80%"></div><div class="sk" style="width:100%;height:6px"></div><div class="cm"><div class="sk" style="width:70%"></div></div></div></div>'
    return f'<h1>목표 — 불러오는 중</h1><div class="grid">{sk * 4}</div>'


def error_view():
    gl = dict(GOALS[2])
    body = (f'<div class="card"><div class="main"><div class="meta"><span>순서 3 · AADS · P1</span><span>진행 중</span></div>'
            f'<div class="title">{E(gl["title"])}</div><div class="bar"><i style="width:44%"></i></div>'
            '<div class="pct">목표 전체 44%</div></div><div class="ord"><button>▲</button><button>▼</button></div></div>')
    return ('<h1>목표 — 현재 단계 정보를 못 받은 경우</h1><div class="sub">현재 단계 줄만 숨기고 기존 카드는 그대로 보인다(빈칸·0% 표시 금지).</div>'
            f'<div class="grid">{body}</div>')


def empty_view():
    return ('<h1>목표 — 진행 중 0</h1><div class="card"><div class="main" style="font-size:13px;color:#475569">'
            '진행 중인 목표가 없습니다. 초안 목표를 시작하면 여기에 현재 단계가 보입니다.</div></div>')


CHAT_CSS = """
.chat{display:flex;height:860px;background:#fff;border:1px solid #e2e8f0;border-radius:12px;overflow:hidden}
.left{flex:1;display:flex;flex-direction:column}.strip{position:relative;padding:8px 12px;border-bottom:1px solid #e2e8f0}
.sbtn{display:flex;gap:8px;align-items:center;border:1px solid #cbd5e1;border-radius:10px;padding:8px 12px;font-size:12.5px;min-height:44px;background:#f8fafc}
.dd{position:absolute;left:12px;top:60px;width:560px;background:#fff;border:1px solid #cbd5e1;border-radius:12px;box-shadow:0 10px 30px rgba(0,0,0,.12);padding:8px;z-index:2}
.grp{font-size:11px;font-weight:800;color:#64748b;margin:8px 6px 4px}.it{padding:8px 10px;border-radius:8px}.it:hover{background:#f1f5f9}
.it .tt{font-size:13px;font-weight:700}.it .cm{margin-top:4px;padding-top:0;border-top:0}
.msgs{flex:1;padding:16px;color:#94a3b8;font-size:12px}
.panel{width:430px;border-left:1px solid #e2e8f0;padding:14px;overflow:auto;background:#f8fafc}
.now{background:#fff;border:1px solid #e2e8f0;border-radius:12px;padding:12px}.now h3{font-size:12px;color:#64748b;margin-bottom:6px}
.big{height:8px;background:#e2e8f0;border-radius:4px;overflow:hidden;margin:8px 0}.big i{display:block;height:100%;background:#0ea5e9}
.ms{display:flex;gap:8px;align-items:center;padding:8px 4px;border-bottom:1px solid #e2e8f0;font-size:12px}
.dot{width:10px;height:10px;border-radius:50%;flex-shrink:0}.ms .r{margin-left:auto;color:#64748b;white-space:nowrap}
.rv{color:#7c3aed;font-weight:700}
"""

PANEL_MS = [  # 2026-10-28 운영 DB 실측(GO100 실매매 파이프라인 개선, 제목 줄임)
    ("M1", "긴급 주문 안전 트랙(E1-a/E1-b)", "#7c3aed", "검토 대기", "작업 0/9"),
    ("M2", "0-D 서브디스크 이관 완결", "#7c3aed", "검토 대기", "작업 0/8"),
    ("M3", "0단계 자원 통제·관측·CI 기반", "#7c3aed", "검토 대기", "작업 0/10"),
    ("M4", "A단계 기준선 측정·카드별 예산", "#7c3aed", "검토 대기", "작업 0/7"),
    ("M5", "B~E 메모리/버스 신호 경로", "#94a3b8", "대기", "작업 0/7"),
    ("M6", "F 레인별 전환과 G 정리", "#94a3b8", "대기", "작업 0/6"),
]


def goal_rate(pct, review):
    rv = f' · <span class="rv">검토 대기 {review}단계</span>' if review else ""
    return f'확정 {pct}%{rv}'


NEW_CSS = """
.new{outline:2px dashed #16a34a;outline-offset:2px;border-radius:6px;position:relative}
.nb{display:inline-block;background:#16a34a;color:#fff;font-size:10px;font-weight:800;border-radius:4px;padding:1px 5px;margin-right:4px}
.fix{background:#fef9c3;border-radius:4px;padding:0 3px}
.ap{font-size:11px;color:#64748b;margin-left:22px}
"""

# 2026-10-28 13:2x KST 운영 /chat 실캡처(live_before_text.json)와 운영 DB 를 그대로 옮긴 목표 6개
STRIP = [
    ("P0", "러너 검수 인프라 무응답 제거 — 승인 병목 해소", True, 20, "수동", None,
     {"seq": 2, "total": 5, "title": "리뷰 입력 축소 — 대용량 diff 분할 전송", "items": 0, "done": 0, "prog": 0, "state": "blocked", "other": 0}, 0),
    ("P1", "지시서 활성화·최적화 — 설계 대화 검토부터 실행 검증까지", False, 10, "멈춤", "막힘",
     {"seq": 7, "total": 11, "title": "대화 맥락·설계 초안 버전 관리", "items": 0, "done": 0, "prog": 0, "state": "working", "other": 0}, 0),
    ("P1", "OHVIS 문서 저장 규칙(R-DOC) — 저장 위치·파일명·정본 등록 일원화", False, 30, "수동", "막힘",
     {"seq": 1, "total": 10, "title": "R-DOC 기획·설계·PRD 정본 등록과 CEO 승인", "items": 0, "done": 0, "prog": 0, "state": "blocked", "other": 0}, 0),
    ("P1", "목표 우선순위·휴면 정리·자동승인 표시 일치", False, 0, "수동", "막힘",
     {"seq": 1, "total": 4, "title": "PRD 확정·목업 2장(띠 칩, 정리 카드)", "items": 1, "done": 0, "prog": 0, "state": "blocked", "other": 0}, 0),
    ("P1", "목표 승인설정 프로젝트별 분리 — 비매매 자동통과 차단", True, 60, "수동", "막힘",
     {"seq": 3, "total": 5, "title": "프로젝트별 승인 프로필·화면 문구", "items": 0, "done": 0, "prog": 0, "state": "no_owner", "other": 0}, 1),
    ("P1", "오비스 UI/UX 디자인 기획 체계 구축", False, 0, "전체 자동 0/2000", None,
     {"seq": 2, "total": 6, "title": "공통 디자인 원칙과 프로젝트 프로필 계약", "items": 9, "done": 0, "prog": 0, "state": "blocked", "other": 0}, 0),
]


def chat_view():
    rows = ""
    for pri, title, lead, pct, ap, gstate, ms, review in STRIP:
        lead_b = ' <span style="color:#b45309">👑 주도</span>' if lead else ""
        st = (f' <span class="fix">목표 상태 막힘 — 사유·해제 조건은 목표 패널</span>' if gstate else "")
        rate = (f'확정 {pct}% <span class="new rv"><span class="nb">새로</span>검토 대기 {review}단계</span>' if review else f'{pct}%')
        rows += (f'<div class="it"><div class="tt">🔄 {pri} {E(title)}{lead_b}{st} <span style="font-weight:400;color:#64748b">{rate}</span></div>'
                 f'<div class="new" style="margin:4px 0 2px 18px"><span class="nb">새로</span>{cm_line(ms).replace(CM_OPEN, CM_INLINE)}</div>'
                 f'<div class="ap">승인: {E(ap)}</div></div>')
    btn = ('<div class="sbtn">🎯 <b>목표 6</b> 🔄6 │ 🔄 P0 러너 검수 인프라 무응답 제거 — 승인 병목 해소 20% '
           '<span class="new" style="padding:0 4px"><span class="nb">새로</span>M2 · <span style="color:#dc2626;font-weight:700">진행: 막힘</span></span> ▴</div>')
    return (f'<style>{CHAT_CSS}{NEW_CSS}</style><h1>채팅 — 목표 버튼(펼침) · 5판</h1>'
            '<div class="sub">초록 점선 + "새로" = 이번에 추가되는 것. 나머지는 지금 운영 화면 그대로(2026-10-28 13:2x KST 실캡처 기준). '
            '노란 칠 = 영문 상태값(blocked)을 한글로 바꾸는 곳(CM-13).</div>'
            f'<div class="chat"><div class="left"><div class="strip">{btn}<div class="dd" style="width:760px">{rows}</div></div>'
            '<div class="msgs">(대화 영역)</div></div></div>')


PANEL_ROWS = [  # 러너 검수 인프라 무응답 제거 — 운영 DB·실캡처
    ("1", "검수 모델별 응답시간·timeout 원인 실측", "#16a34a", "완료", "업무 없음"),
    ("2", "리뷰 입력 축소 — 대용량 diff 분할 전송", "#dc2626", "실패", "업무 없음"),
    ("3", "무응답 시 대체 판정 경로 표준화", "#dc2626", "막힘", "업무 없음"),
    ("4", "무응답률 경보 추가", "#dc2626", "막힘", "업무 없음"),
    ("5", "목표 생성 시 주도담당 자동 지정·누락 차단", "#dc2626", "막힘", "업무 없음"),
]


def panel_view():
    rows = "".join(
        f'<div class="ms"><span class="dot" style="background:{c}"></span><b>M{n}</b> {E(t)}'
        f'<span class="r"><span class="fix" style="color:{c}">상태 {st}</span> · {r} · '
        f'<span class="new" style="padding:0 3px"><span class="nb">새로</span>단계 {n}/5</span></span></div>' for n, t, c, st, r in PANEL_ROWS)
    head = ('<div style="font-size:15px;font-weight:800">🎯 러너 검수 인프라 무응답 제거 — 승인 병목 해소</div>'
            '<div style="font-size:12px;margin:4px 0 10px;color:#334155">진행 중 · 마일스톤 1/5 완료 · ⏸ 멈춤 · 20% · 승인: 수동 · 진행: 대기 '
            '<span class="ap" style="margin:0">(지금 운영 그대로)</span></div>')
    now = ('<div class="now new"><h3><span class="nb">새로</span>지금 단계</h3>'
           '<div style="font-size:14px;font-weight:700">M2 리뷰 입력 축소 — 대용량 diff 분할 전송</div>'
           '<div style="font-size:12px;margin-top:6px">단계 2/5 · 하위 작업 없음(진행률 대신 단계 순번) · '
           '<span style="color:#dc2626;font-weight:700">진행: 막힘</span></div>'
           '<div style="font-size:11.5px;color:#7f1d1d;margin-top:6px">실패 작업 runner-3cd2acc8 · 재작업 한도 소진 · 담당 역할 미지정</div>'
           '<div class="btns"><button class="p">다시 보내기</button><button class="s">담당 지정</button>'
           '<span class="ap" style="margin:auto 0">← 지금은 단계 안쪽에 조건부로만 있는 버튼을 위로 끌어올림</span></div></div>')
    return (f'<style>{CHAT_CSS}{NEW_CSS}</style><h1>채팅 — 오른쪽 목표 패널 · 5판</h1>'
            '<div class="sub">초록 점선 = 새로 추가. 노란 칠 = 영문 상태값(completed/failed/blocked) 한글화(CM-13). 나머지(업무 계층·완료 기준·승인 설정)는 지금 그대로 아래에 남는다.</div>'
            f'<div class="chat"><div class="left"><div class="strip"><div class="sbtn">🎯 목표 6 ▾</div></div>'
            f'<div class="msgs">(대화 영역)</div></div><div class="panel" style="width:560px">{head}{now}'
            f'<div style="margin-top:12px;font-size:12px;font-weight:800;color:#64748b">단계 5 (업무 계층·완료 기준 펼침은 현행 유지)</div>{rows}</div></div>')


def review_view():
    """검토 대기 단계가 있는 목표 — 0% 로만 보이던 문제(CM-12)."""
    rows = "".join(
        f'<div class="ms"><span class="dot" style="background:{c}"></span><b>{n}</b> {E(t)}'
        f'<span class="r" style="color:{c}">{st} · {r}</span></div>' for n, t, c, st, r in PANEL_MS)
    head = ('<div style="font-size:15px;font-weight:800">GO100 실매매 파이프라인 개선</div>'
            '<div style="font-size:12px;margin:4px 0 10px">지금 운영: <b>0%</b> 만 보임 → 시안: '
            '<span class="new" style="padding:0 4px"><span class="nb">새로</span>확정 0% · <span class="rv">검토 대기 4단계</span></span></div>')
    now = ('<div class="now new"><h3><span class="nb">새로</span>지금 단계</h3><div style="font-size:14px;font-weight:700">진행 중 단계 없음 '
           '<span class="rv">· 검토 대기 4단계</span></div>'
           '<div style="font-size:12px;margin-top:6px">담당이 M1~M4 완료를 보고했지만 아직 확인되지 않았습니다. 확인하면 확정 진행률이 오르고 M5 가 시작됩니다.</div>'
           '<div class="btns"><button class="p">검토하기</button><button class="s">담당에게 다시 묻기</button></div></div>')
    return (f'<style>{CHAT_CSS}{NEW_CSS}</style><h1>채팅 — 오른쪽 목표 패널(검토 대기 목표) · 5판</h1>'
            '<div class="sub">대표님 질문 "주도 세션은 25% 라는데 왜 0%?" 에 대한 화면 답. 예시: 운영 DB 실측(2026-10-28).</div>'
            f'<div class="chat"><div class="left"><div class="strip"><div class="sbtn">🎯 목표 ▾</div></div>'
            f'<div class="msgs">(대화 영역)</div></div><div class="panel" style="width:560px">{head}{now}'
            f'<div style="margin-top:12px;font-size:12px;font-weight:800;color:#64748b">단계 6</div>{rows}</div></div>')


TREE = [  # 운영 DB 실측 — "오비스 UI/UX 디자인 기획 체계 구축" M2 의 업무 계층 9건(모두 초안)
    ("디자인 브리프와 공통 시각 기준", [
        "원하는 인상·정보 밀도·핵심 행동 우선순위를 브리프로 작성한다",
        "레퍼런스의 출처·확인일·채택 및 제외 이유를 기록한다",
        "색·글꼴·여백·버튼·입력·카드·포커스 토큰과 상태 규칙을 명세한다"]),
    ("프로젝트 프로필과 승인 버전 계약", [
        "프로젝트별 사용자·route·기존 컴포넌트·뷰포트를 프로필에 정의한다",
        "문서와 목업 revision·hash 및 추적 식별자 스키마를 확정한다",
        "개인정보·테넌트 격리·승인 철회·이전 버전 호환 정책을 명세한다"]),
]

TREE_CSS = """
.msr{display:flex;gap:8px;align-items:center;padding:10px;border:1px solid #e2e8f0;border-radius:9px;background:#fff;margin-top:8px;cursor:pointer;min-height:44px;font-size:12px}
.msr.on{border-color:#16a34a;border-left:3px solid #dc2626}.car{width:14px;font-size:11px;color:#475569}
.tree{margin:4px 0 0 14px;padding-left:10px;border-left:2px solid #e2e8f0;font-size:12px}
.ep{font-weight:800;margin:8px 0 4px}.st{font-weight:700;margin:6px 0 3px 10px}.tk{display:flex;gap:6px;margin:3px 0 3px 22px;line-height:1.45}
.pill{font-size:10.5px;border:1px solid #94a3b8;color:#475569;border-radius:999px;padding:0 6px;white-space:nowrap;height:18px}
.emp{margin:6px 0 0 14px;padding:10px;border:1px dashed #cbd5e1;border-radius:8px;font-size:12px;color:#475569}
"""


def expanded_view():
    tree = '<div class="tree new"><span class="nb">새로</span> 단계 줄을 누르면 바로 아래로 펼침 · 작업 0/6 (0%) · 진행 0'
    tree += '<div class="ep">▼ 에픽 · 공통 디자인 원칙과 프로젝트 프로필 계약 수립 <span class="pill">초안</span> <span style="color:#64748b;font-weight:400">작업 0/6</span></div>'
    for story, tasks in TREE:
        tree += f'<div class="st">▼ 스토리 · {E(story)} <span class="pill">초안</span> <span style="color:#64748b;font-weight:400">작업 0/{len(tasks)}</span></div>'
        tree += "".join(f'<div class="tk"><span>☐</span><span>{E(t)}</span><span class="pill">초안</span></div>' for t in tasks)
    tree += '</div>'
    rows = ('<div class="msr"><span class="car">▶</span><span class="dot" style="background:#16a34a"></span><b>M1</b> 기존 자산 감사와 사용자 요구 명세 확정'
            '<span class="r" style="margin-left:auto;color:#16a34a">완료 · 작업 0/6 <span style="color:#b45309">⚠ 하위 작업 완료 표시 안 됨</span></span></div>'
            '<div class="msr on"><span class="car">▼</span><span class="dot" style="background:#dc2626"></span><b>M2</b> 공통 디자인 원칙과 프로젝트 프로필 계약 수립'
            '<span class="r" style="margin-left:auto;color:#dc2626">막힘 · 작업 0/6</span></div>' + tree +
            '<div class="msr"><span class="car">▶</span><span class="dot" style="background:#dc2626"></span><b>M3</b> AADS 파일럿 시안과 승인 패키지 확정'
            '<span class="r" style="margin-left:auto;color:#dc2626">막힘 · 작업 0/10</span></div>'
            '<div class="msr"><span class="car">▶</span><span class="dot" style="background:#94a3b8"></span><b>M4</b> 개발 인계와 서버 실행 검증 연결'
            '<span class="r" style="margin-left:auto;color:#64748b">대기</span></div>'
            '<div class="emp new"><span class="nb">새로</span>업무가 없는 단계를 펼쳤을 때(예: "러너 검수 인프라 무응답 제거" M3): '
            '"이 단계에는 아직 Epic·Story·Task 가 없습니다. 진행률은 단계 순번으로만 보입니다." + '
            '<div class="btns"><button class="s">주도에게 업무 등록 요청</button></div></div>')
    head = ('<div style="font-size:15px;font-weight:800">🎯 오비스 UI/UX 디자인 기획 체계 구축</div>'
            '<div style="font-size:12px;margin:4px 0 10px;color:#334155">진행 중 · 마일스톤 1/6 완료 · 0% · 승인: 전체 자동 0/2000 (지금 운영 그대로)</div>')
    return (f'<style>{CHAT_CSS}{NEW_CSS}{TREE_CSS}</style><h1>채팅 — 오른쪽 목표 패널 · 마일스톤을 눌러 업무 계층 펼침 · 7판</h1>'
            '<div class="sub">지금 운영: 마일스톤 카드는 눌러도 반응 없음 — 카드 아래 작은 "▶ 업무 계층" 줄만 펼쳐진다. 시안: 단계 줄 전체(44px 이상)를 누르면 그 아래로 Epic→Story→Task 가 펼쳐지고, 업무가 없으면 빈 칸 대신 다음 행동을 보여 준다. 예시: 운영 DB 실측 업무 9건.</div>'
            f'<div class="chat"><div class="left"><div class="strip"><div class="sbtn">🎯 목표 6 ▾</div></div>'
            f'<div class="msgs">(대화 영역)</div></div><div class="panel" style="width:600px">{head}{rows}</div></div>')


SPECS = [
    ("goal-cards_before", (1440, 900), "default", "live-sample", before_view, "before"),
    ("goal-cards_before_mobile", (390, 844), "default", "live-sample", before_view, "before"),
    ("goal-cards_default", (1440, 900), "default", "live-sample", default_view),
    ("goal-cards_default_mobile", (390, 844), "default", "live-sample", default_view),
    ("goal-cards_loading", (1440, 900), "loading", "loading", loading_view),
    ("goal-cards_error", (1440, 900), "error", "current-milestone-missing", error_view),
    ("goal-cards_empty", (1440, 900), "empty", "no-active", empty_view),
    ("chat-goal-button_open", (1440, 900), "default", "live-production", chat_view),
    ("chat-goal-panel", (1440, 900), "default", "live-production", panel_view),
    ("chat-goal-panel_mobile", (390, 844), "default", "live-production", panel_view),
    ("chat-goal-panel_review", (1440, 900), "default", "live-production", review_view),
    ("chat-goal-panel_expanded", (1440, 900), "default", "live-production", expanded_view),
    ("chat-goal-panel_expanded_mobile", (390, 844), "default", "live-production", expanded_view),
]

LIVE_BEFORE = ["live-before_chat-strip_1440x900.png", "live-before_chat-goal-button_open_1440x900.png",
               "live-before_chat-goal-panel_1440x900.png", "live-before_chat-strip_390x844.png"]


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def main() -> None:
    (ROOT / "html").mkdir(exist_ok=True)
    (ROOT / "png").mkdir(exist_ok=True)
    captured = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
    assets, children = [], []
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        for name, (w, h), state, fixture, build, *ph in SPECS:
            phase = ph[0] if ph else "mockup"
            html_path = ROOT / "html" / f"{name}.html"
            html_path.write_text(page(name, build()), encoding="utf-8")
            png_path = ROOT / "png" / f"{name}_{w}x{h}.png"
            pg = browser.new_page(viewport={"width": w, "height": h}, device_scale_factor=1)
            pg.goto(html_path.as_uri())
            pg.wait_for_timeout(150)
            pg.screenshot(path=str(png_path), full_page=False)
            pg.close()
            data = png_path.read_bytes()
            assets.append({
                "asset_id": f"{phase}-{name}-{w}x{h}", "role": "primary",
                "uri": f"internal://{REL}/png/{png_path.name}", "sha256": sha(data), "byte_size": len(data),
                "mime": "image/png", "screen_id": "goal-card-current-milestone", "phase": phase,
                "viewport": f"{w}x{h}", "fixture_id": fixture, "state": state,
                "captured_at": captured.isoformat(), "capture_source": "static_html_render" if phase == "mockup" else "source_reconstruction",
                "redaction_status": "not_required",
            })
            hdata = html_path.read_bytes()
            children.append({"asset_id": f"html-{name}", "role": "child",
                             "uri": f"internal://{REL}/html/{html_path.name}", "sha256": sha(hdata),
                             "byte_size": len(hdata), "mime": "text/html"})
        browser.close()
    for fn in LIVE_BEFORE:  # 운영 화면 실캡처(capture_before_live.py) — 변경 전
        data = (ROOT / "png" / fn).read_bytes()
        w, h = fn.rsplit("_", 1)[1].removesuffix(".png").split("x")
        assets.append({
            "asset_id": "before-" + fn.removesuffix(".png"), "role": "primary",
            "uri": f"internal://{REL}/png/{fn}", "sha256": sha(data), "byte_size": len(data),
            "mime": "image/png", "screen_id": "goal-card-current-milestone", "phase": "before",
            "viewport": f"{w}x{h}", "fixture_id": "live-production", "state": "default",
            "captured_at": "2026-10-28T04:21:00+00:00", "capture_source": "live_capture",
            "redaction_status": "not_required",
        })
    manifest = {
        "screens": [{
            "screen_id": "goal-card-current-milestone",
            "title": "목표 카드 하단 — 현재 단계 진행률·진행 상태",
            "route": "/goals",
            "requirement_ids": ["CM-1", "CM-2", "CM-3", "CM-4", "CM-5", "CM-6", "CM-7", "CM-8", "CM-10", "CM-11", "CM-12", "CM-13", "CM-14"],
            "states_not_applicable": {
                "permission": "목표 화면 공통 권한 처리를 따르며 이 요소 전용 권한 화면이 없다",
                "session-expired": "로그인 만료는 공통 로그인 화면으로 이동한다",
                "offline": "오프라인 전용 화면이 없고 목표 목록 공통 오류 표시를 쓴다",
            },
        }],
        "assets": assets + children,
        "evidence": [{
            "evidence_id": "render-goal-card-current-milestone", "kind": "browser_capture",
            "screen_id": "goal-card-current-milestone", "route": "/goals", "success": True, "login_used": False,
            "detail": ("서버 Playwright chromium 으로 정적 HTML 시안 8장(목표 카드 1440 default·loading·error·empty, 390 default, 채팅 목표 버튼 1440, 오른쪽 현황 패널 1440·390)과 현행 카드 소스 재현 2장(변경 전) 캡처. 운영 /goals 는 로그인이 필요해 변경 전은 소스 재현. "
                       "예시 값은 2026-10-28 08:2x KST 운영 DB 진행 중 목표. 진행 중 단계 61개 중 하위 작업 있는 단계 24개."),
            "recorded_at": captured.isoformat(),
        }, {
            "evidence_id": "live-before-chat", "kind": "browser_capture",
            "screen_id": "goal-card-current-milestone", "route": "/chat", "success": True, "login_used": True,
            "detail": ("5판: 운영 /chat(대시보드 3bd1eaf)에 E2E 로그인해 서버 Playwright 로 변경 전 실캡처 4장 — 목표 버튼 접힘·펼침·오른쪽 목표 패널(1440), "
                       "목표 버튼 접힘(390). 390 에서는 목표 버튼을 찾지 못해 펼침·패널 미캡처. 화면 글자 덤프 live_before_text.json. "
                       "시안의 초록 점선='새로', 노란 칠=영문 상태값 한글화(CM-13)."),
            "recorded_at": "2026-10-28T04:21:00+00:00",
        }],
        "source_sha": SOURCE_SHA,
    }
    (ROOT / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({"png": len(assets), "html": len(children)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
