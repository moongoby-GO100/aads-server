#!/usr/bin/env python3
"""채팅 아티팩트 '문서파일' 탭 정본 승인 처리 시안 생성기 (2026-10-28, AADS).

현행 화면(aads-dashboard origin/main ChatArtifactPanel.tsx 2408~2460행 '정본 문서 N건' 목록)을
before.html 로 재현하고, 제안 화면을 after.html(장면 = URL 해시)로 만든 뒤
Playwright chromium 으로 데스크톱 1440x900 / 모바일 390x844 PNG 를 evidence/ 에 캡처한다.
CSS 변수는 대시보드 globals.css 의 다크 테마 값을 그대로 쓴다.

실행: cd docs/design/mockups/20261028_chat_artifact_canonical_approve && timeout 300 python3 build_mockup.py
정본: PRD chat-artifact-canonical-approve-prd
"""
from __future__ import annotations

import datetime
import hashlib
import html
import json
import pathlib

from playwright.sync_api import sync_playwright

ROOT = pathlib.Path(__file__).resolve().parent
EV = ROOT / "evidence"
REL = "docs/design/mockups/20261028_chat_artifact_canonical_approve"
SOURCE_SHA = "fde1b4f"  # aads-dashboard origin/main
E = html.escape

CSS = """
:root{--bg-primary:#0f172a;--bg-card:#1e293b;--bg-hover:#334155;--text-primary:#e2e8f0;--text-secondary:#94a3b8;
--accent:#3b82f6;--success:#22c55e;--warning:#f59e0b;--danger:#ef4444;--border:#334155}
*{box-sizing:border-box}
html,body{margin:0;height:100%}
body{background:var(--bg-primary);color:var(--text-primary);font-family:-apple-system,"Segoe UI","Noto Sans KR","Apple SD Gothic Neo",sans-serif;font-size:14px;line-height:1.45;overflow-x:hidden}
button{font-family:inherit;color:inherit}
header.app{height:48px;display:flex;align-items:center;gap:12px;padding:0 20px;border-bottom:1px solid var(--border);font-weight:700}
header.app .tag{font-size:11px;color:var(--warning);border:1px dashed var(--warning);border-radius:4px;padding:1px 6px;font-weight:600}
.layout{display:flex;height:calc(100% - 48px)}
.chat{flex:1;min-width:0;padding:20px 24px;overflow:hidden}
.bubble{max-width:620px;background:var(--bg-card);border:1px solid var(--border);border-radius:12px;padding:12px 14px;margin-bottom:12px;color:var(--text-secondary)}
.bubble.me{margin-left:auto;background:var(--bg-hover);color:var(--text-primary)}
.panel{width:480px;flex:none;border-left:1px solid var(--border);background:var(--bg-card);display:flex;flex-direction:column;min-height:0}
.tabs{display:flex;gap:2px;padding:8px 8px 0;border-bottom:1px solid var(--border);overflow:hidden}
.tabs span{padding:8px 9px;font-size:12px;color:var(--text-secondary);white-space:nowrap}
.tabs span.on{color:var(--accent);border-bottom:2px solid var(--accent);font-weight:700}
.body{padding:10px 12px;overflow-y:auto;flex:1;min-height:0}
.scenario{margin:0;padding:6px 12px;font-size:11px;color:var(--warning);border-bottom:1px dashed var(--warning);background:rgba(245,158,11,.06)}
.head{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:8px;font-size:11.5px;color:var(--text-secondary)}
.chip{min-height:44px;display:inline-flex;align-items:center;gap:6px;padding:0 14px;border-radius:999px;border:1px solid var(--border);background:var(--bg-primary);font-size:12px;font-weight:600;color:var(--text-secondary)}
.chip.on{border-color:var(--accent);color:var(--text-primary);background:rgba(59,130,246,.18)}
.chip b{color:var(--accent)}
.doc{display:block;margin-bottom:6px;padding:8px 10px;border-radius:8px;border:1px solid var(--border);background:var(--bg-primary);text-decoration:none;color:inherit}
.doc.open{border-color:var(--accent)}
.doc.old{opacity:.6}
.doc .t{display:flex;gap:7px;align-items:baseline}
.doc .t .name{font-size:12.5px;font-weight:650;word-break:break-all}
.doc .sub{margin-top:3px;font-size:10.5px;color:var(--text-secondary);word-break:break-all}
.doc .st{margin-top:4px;font-size:11px;color:var(--text-secondary)}
.badge{margin-left:auto;font-size:10px;border:1px solid var(--border);border-radius:5px;padding:1px 6px;white-space:nowrap;color:var(--text-secondary)}
.b-draft{background:var(--bg-hover);color:var(--text-primary)}
.b-review{border-color:var(--accent);color:var(--text-primary)}
.b-approved{border-color:var(--accent);color:var(--text-primary);background:rgba(59,130,246,.30);font-weight:600}
.b-rejected{border-color:var(--warning);color:var(--warning)}
.b-old{border-style:dashed}
.detail{margin-top:8px;padding-top:8px;border-top:1px solid var(--border);font-size:12px}
.kv{display:grid;grid-template-columns:76px 1fr;gap:3px 8px;margin:0 0 8px}
.kv dt{color:var(--text-secondary);font-size:11px}.kv dd{margin:0;word-break:break-all}
.view{min-height:44px;display:flex;align-items:center;gap:6px;padding:0 10px;border:1px solid var(--border);border-radius:8px;background:var(--bg-card);cursor:pointer;font-size:12px;font-weight:600;width:100%;text-align:left}
.content{margin:6px 0 0;padding:10px;max-height:320px;overflow:auto;white-space:pre-wrap;word-break:break-word;font-size:11.5px;background:#0b1220;border:1px solid var(--border);border-radius:8px;color:var(--text-secondary)}
.actions{display:flex;flex-wrap:wrap;gap:8px;margin-top:10px}
.btn{min-height:44px;min-width:44px;padding:0 16px;border-radius:8px;border:1px solid var(--border);background:var(--bg-hover);font-size:13px;font-weight:700;cursor:pointer;display:inline-flex;align-items:center;justify-content:center;gap:6px}
.btn.primary{background:var(--accent);border-color:var(--accent);color:#fff}
.btn.danger{background:transparent;border-color:var(--danger);color:var(--danger)}
.btn.ghost{font-weight:500;color:var(--text-secondary)}
.btn[disabled]{opacity:.45;cursor:not-allowed}
.btn.grow{flex:1 1 100%}
.btn.gap{margin-top:8px}
.hint{margin-top:8px;font-size:11.5px;color:var(--text-secondary)}
.alert{margin-top:10px;padding:10px 12px;border-radius:8px;border:1px solid var(--danger);background:rgba(239,68,68,.08);font-size:12px}
.alert b{display:block;margin-bottom:2px}
.alert.warn{border-color:var(--warning);background:rgba(245,158,11,.08)}
.toast{margin-bottom:8px;padding:10px 12px;border-radius:8px;border:1px solid var(--success);background:rgba(34,197,94,.10);font-size:12px}
.spin{width:14px;height:14px;border:2px solid #fff6;border-top-color:#fff;border-radius:50%;display:inline-block;animation:r 1s linear infinite}
@keyframes r{to{transform:rotate(360deg)}}
.sheet{position:absolute;left:0;right:0;bottom:0;background:var(--bg-card);border-top:1px solid var(--accent);border-radius:14px 14px 0 0;padding:14px 14px 16px;box-shadow:0 -12px 30px rgba(0,0,0,.45)}
.sheet h4{margin:0 0 4px;font-size:14px}
.sheet textarea{width:100%;min-height:92px;margin-top:8px;border-radius:8px;border:1px solid var(--border);background:var(--bg-primary);color:var(--text-primary);padding:10px;font:inherit;font-size:13px}
.sheet .cnt{font-size:11px;color:var(--text-secondary);text-align:right}
.panelwrap{position:relative;display:flex;flex-direction:column;min-height:0}
.dim{position:absolute;inset:0;background:rgba(2,6,23,.55)}
.scene{display:none}
.scene.on{display:contents}
@media (max-width:700px){
 header.app{padding:0 12px}.chat{display:none}
 .panel{width:100%;border-left:none}
 .layout{display:block}
 .panelwrap{height:100%}
 .panel{height:100%}
}
"""

TABS = ["로그", "배포", "아이디어 메모", "지시서", "보고서", "문서파일", "대화응답", "코드"]


def tabs() -> str:
    return '<div class="tabs">' + "".join(
        f'<span class="{"on" if t == "문서파일" else ""}">{E(t)}</span>' for t in TABS) + "</div>"


def shell(inner: str, title: str, scenario: str = "", tag: str = "시안 · 예시 데이터") -> str:
    chat = (
        '<div class="chat"><div class="bubble me">정본 문서 승인 대기 건 정리해 줘</div>'
        '<div class="bubble">PM 세션이 검토로 올린 정본 문서 3건이 있습니다. 오른쪽 문서파일 탭에서 확인할 수 있습니다.</div></div>'
    )
    scen = f'<p class="scenario">{E(scenario)}</p>' if scenario else ""
    return (f'<header class="app">AADS · 채팅 <span class="tag">{E(tag)}</span> <span style="font-weight:400;color:var(--text-secondary);font-size:12px">{E(title)}</span></header>'
            f'<div class="layout">{chat}<div class="panelwrap panel">{scen}{tabs()}{inner}</div></div>')


# ------------------------------------------------------------------ before
def before_html() -> str:
    docs = [
        ("문서 등급·교차 검토 설계", "AADS", "doc-tier-cross-review-design", 1, "초안"),
        ("문서 등급별 작성·검토 모델 교차 원칙 PRD", "AADS", "doc-tier-cross-review-prd", 2, "초안"),
        ("목표 현재 마일스톤 표시 설계", "AADS", "goal-current-milestone-design", 1, "승인됨"),
        ("목표 스트립 드롭다운 시안", "AADS", "goal-strip-dropdown-mock", 3, "초안"),
    ]
    rows = ""
    for title, proj, key, rev, st in docs:
        rows += (f'<a class="doc"><div class="t"><span>📘</span><span class="name">{E(title)}</span>'
                 f'<span class="badge">{st}</span></div><div class="sub">{proj} · {key} · 개정 {rev}</div></a>')
    inner = ('<div class="body"><div class="head">정본 문서 4건</div>' + rows +
             '<div class="hint" style="margin-top:14px">※ 현행: 링크만 있다. 검토 상태와 처리 버튼이 없다. 검토로 올라간 문서도 「초안」으로 보인다(<code>approved ? "승인됨" : "초안"</code>).</div></div>')
    return page("현재 화면 (before)", shell(inner, "현재: 정본 문서 목록 = 링크 + 배지 2종", "before · ChatArtifactPanel.tsx 2408~2460행 재현", "현행 재현"))


def page(title: str, body: str, script: str = "") -> str:
    return (f'<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{E(title)}</title><style>{CSS}</style></head><body>{body}{script}</body></html>')


# ------------------------------------------------------------------ after
CONTENT_SAMPLE = ("# 문서 등급별 작성·검토 모델 교차 원칙 PRD\n\n- document_key: doc-tier-cross-review-prd\n"
                  "- 상태: 초안\n\n## 1. 왜 지금 필요한가\n같은 회사 모델이 쓰고 검수하면 같은 약점을 서로 통과시킨다...\n\n"
                  "## 3. 등급 정의\nD1 가벼움 / D2 보통 / D3 중요 ...\n")


def row(kind: str, *, title: str, key: str, rev: int, ver: str, open_: bool = False, extra: str = "", old: bool = False,
        status_line: str = "") -> str:
    badge = {
        "draft": '<span class="badge b-draft">초안</span>',
        "review": '<span class="badge b-review">검토 대기 · 3일</span>',
        "approved": '<span class="badge b-approved">승인됨</span>',
        "rejected": '<span class="badge b-rejected">반려</span>',
        "busy": '<span class="badge b-review">처리 중…</span>',
    }[kind]
    cls = "doc" + (" open" if open_ else "") + (" old" if old else "")
    st = f'<div class="st">{status_line}</div>' if status_line else ""
    return (f'<div class="{cls}"><div class="t"><span>📘</span><span class="name">{E(title)}</span>{badge}</div>'
            f'<div class="sub">AADS · {key} · 개정 {rev} · v{ver}</div>{st}{extra}</div>')


def kv(rows: list[tuple[str, str]]) -> str:
    return '<dl class="kv">' + "".join(f"<dt>{E(k)}</dt><dd>{v}</dd>" for k, v in rows) + "</dl>"


REVIEW_KV = kv([
    ("개정", "개정 2 · v1.1.0 (직전 개정 1 → +1,240자)"),
    ("종류", "제품 요구사항 (prd)"),
    ("작성 출처", "러너 <code>runner-8c1f20aa</code>"),
    ("작성 사유", "CEO 결정 반영: 3등급 + 교차 회사 검토 원칙 확정안 기재"),
    ("검토로 올림", "PM 세션 chat:7f3a91c2 · 2026-10-25 15:22 KST (3일 대기)"),
])


def viewer(open_: bool) -> str:
    if not open_:
        return '<button class="view">▶ 내용 보기 · 승인할 본문 확인</button>'
    return f'<button class="view">▼ 내용 보기 · 승인할 본문 확인</button><pre class="content">{E(CONTENT_SAMPLE)}</pre>'


def actions(approve_enabled: bool, busy: bool = False, note: str = "") -> str:
    dis = "" if approve_enabled and not busy else " disabled"
    label = '<span class="spin"></span> 처리 중…' if busy else "승인"
    bdis = " disabled" if busy else ""
    n = note or ("" if approve_enabled else '<div class="hint">본문을 먼저 열어 확인하세요. 열람 후 승인 버튼이 켜집니다.</div>')
    return (f'{n}<div class="actions"><button class="btn primary grow"{dis}>{label}</button>'
            f'<button class="btn danger"{bdis}>반려…</button><button class="btn ghost"{bdis}>정본 화면에서 열기</button></div>')


def review_row(*, viewed: bool, busy: bool = False, alert: str = "", extra_note: str = "") -> str:
    inner = (f'<div class="detail">{REVIEW_KV}{viewer(viewed)}{alert}{actions(viewed, busy, extra_note)}</div>')
    return row("busy" if busy else "review", title="문서 등급별 작성·검토 모델 교차 원칙 PRD", key="doc-tier-cross-review-prd",
               rev=2, ver="1.1.0", open_=True, extra=inner)


def other_rows(skip: str = "") -> str:
    r = ""
    if skip != "design":
        r += row("review", title="문서 등급·교차 검토 설계", key="doc-tier-cross-review-design", rev=1, ver="1.0.0",
                 status_line="승인 대기 3일 · 눌러서 열기")
    if skip != "approved":
        r += row("approved", title="목표 현재 마일스톤 표시 설계", key="goal-current-milestone-design", rev=1, ver="1.0.0",
                 status_line="승인: 대표님 · 2026-10-27 18:04 KST")
    r += row("rejected", title="목표 스트립 드롭다운 시안", key="goal-strip-dropdown-mock", rev=3, ver="1.0.2",
             status_line="반려 사유: 모바일 390px 에서 가로 스크롤이 생김 · 대표님 · 10-26 09:12")
    r += '<div class="hint" style="margin:0 0 6px">▸ 이전 판본 2개 접힘 (버튼 없음)</div>'
    return r


def draft_row(open_: bool = False, pm: bool = False) -> str:
    extra = ""
    if open_ and pm:
        extra = ('<div class="detail">' + kv([("개정", "개정 1 · v1.0.0"), ("작성 출처", "러너 <code>runner-35be9f34</code>"),
                                              ("작성 사유", "기획 PRD 초안 등록")]) +
                 '<label class="hint" style="display:block;margin-top:0">검토로 올리는 사유 (10자 이상)</label>'
                 '<textarea style="width:100%;min-height:64px;margin-top:6px;border-radius:8px;border:1px solid var(--border);background:var(--bg-card);color:var(--text-primary);padding:8px;font:inherit;font-size:13px">본문 점검 완료, 승인 요청</textarea>'
                 '<div class="actions"><button class="btn primary grow">검토로 올리기</button></div></div>')
    elif open_:
        extra = ('<div class="detail">' + kv([("개정", "개정 1 · v1.0.0"), ("작성 출처", "러너 <code>runner-35be9f34</code>")]) +
                 '<div class="hint">PM 이 검토로 올려야 승인할 수 있습니다.</div></div>')
    return row("draft", title="채팅 문서파일 탭 정본 승인 처리 기획", key="chat-artifact-canonical-approve-prd", rev=1,
               ver="1.0.0", open_=open_, extra=extra, status_line="" if open_ else "초안 · PM 이 검토로 올려야 승인 가능")


def head(k: int, filter_on: bool, n: int = 5) -> str:
    chip = (f'<button class="chip{" on" if filter_on else ""}" aria-pressed="{str(filter_on).lower()}">검토 대기 <b>{k}</b></button>'
            if k else "")
    allc = f'<button class="chip{"" if filter_on else " on"}">전체</button>'
    return f'<div class="head"><span>정본 문서 {n}건</span>{chip}{allc}</div>'


def body(inner: str, toast: str = "") -> str:
    return f'<div class="body">{toast}{inner}</div>'


SHEET = ('<div class="dim"></div><div class="sheet"><h4>반려 사유 (필수, 10자 이상)</h4>'
         '<div class="hint" style="margin-top:0">반려하면 이 개정은 승인할 수 없습니다. 작성자가 새 개정을 등록해야 합니다.</div>'
         '<textarea>모바일 390px 폭에서 표가 가로로 넘칩니다. 카드형으로 바꿔 다시 올려 주세요.</textarea>'
         '<div class="cnt">38 / 10자 이상</div>'
         '<div class="actions"><button class="btn danger grow">반려 확정</button><button class="btn ghost grow">취소</button></div></div>')

ALERT_403 = ('<div class="alert" role="alert"><b>이 프로젝트의 문서 승인 권한이 없습니다.</b>'
             '권한이 필요하면 관리자에게 요청하세요. (403 project_access_denied)</div>')
ALERT_409 = ('<div class="alert warn" role="alert"><b>다른 변경이 먼저 반영되었습니다. 최신 내용을 확인했습니다.</b>'
             '새 개정 3 이 등록되었습니다. 본문을 다시 열어 확인한 뒤 승인하세요. (409 generation_conflict)</div>')
ALERT_NOTREVIEW = ('<div class="alert warn" role="alert"><b>검토 상태의 문서만 승인할 수 있습니다.</b>'
                   '문서 상태가 바뀌었습니다. 줄을 새로 불러왔습니다. (409 revision_not_in_review)</div>')
ALERT_NET = ('<div class="alert" role="alert"><b>처리 결과를 확인하지 못했습니다. 상태를 다시 확인합니다.</b>'
             '승인되지 않았으면 [다시 시도]를 누르세요. 중복 승인은 서버가 막습니다.</div>')

# 장면: id -> (제목, 시나리오 설명, 패널 내부 html, 시트 오버레이)
SCENES: dict[str, tuple[str, str, str]] = {}


def add(sid: str, title: str, inner: str, overlay: str = "") -> None:
    SCENES[sid] = (title, inner, overlay)


add("default", "CEO 승인자 · 목록 기본",
    body(head(2, False) +
         row("review", title="문서 등급별 작성·검토 모델 교차 원칙 PRD", key="doc-tier-cross-review-prd", rev=2, ver="1.1.0",
             status_line="승인 대기 3일 · 눌러서 열기") + other_rows() + draft_row()))
add("filter", "CEO 승인자 · 「검토 대기」 필터 적용",
    body(head(2, True) + row("review", title="문서 등급별 작성·검토 모델 교차 원칙 PRD", key="doc-tier-cross-review-prd", rev=2,
                              ver="1.1.0", status_line="승인 대기 3일 · 눌러서 열기") +
         row("review", title="문서 등급·교차 검토 설계", key="doc-tier-cross-review-design", rev=1, ver="1.0.0",
             status_line="승인 대기 3일 · 눌러서 열기")))
add("review-open", "CEO 승인자 · 검토 대기 줄 펼침 (본문 미열람 → 승인 비활성)",
    body(head(2, False) + review_row(viewed=False) + other_rows("") ))
add("review-viewed", "CEO 승인자 · 본문 열람 후 승인 활성",
    body(head(2, False) + review_row(viewed=True)))
add("reject-sheet", "CEO 승인자 · 반려 사유 입력 시트 (사유 필수)",
    body(head(2, False) + review_row(viewed=True)), SHEET)
add("busy", "처리 중 · 두 버튼 모두 비활성",
    body(head(2, False) + review_row(viewed=True, busy=True)))
add("done", "승인 완료 · 줄이 「승인됨」으로 바뀌고 토스트 4초",
    body(head(1, False) +
         row("approved", title="문서 등급별 작성·검토 모델 교차 원칙 PRD", key="doc-tier-cross-review-prd", rev=2, ver="1.1.0",
             status_line="승인: 대표님 · 2026-10-28 19:40 KST") +
         row("review", title="문서 등급·교차 검토 설계", key="doc-tier-cross-review-design", rev=1, ver="1.0.0",
             status_line="승인 대기 3일 · 눌러서 열기") + other_rows("design"),
         '<div class="toast" role="status">승인되었습니다. 2026-10-28 19:40 KST · 대표님</div>'))
add("draft-pm", "PM 검토자 · 초안 줄 펼침 → 「검토로 올리기」 (사유 10자 이상)",
    body(head(2, False) + draft_row(open_=True, pm=True) + other_rows("")), "")
add("draft-ceo", "CEO 승인자 · 초안은 버튼 없음 + 안내",
    body(head(2, False) + draft_row(open_=True, pm=False) + other_rows("")))
add("general", "일반 세션 사용자 · 배지와 건수만, 버튼 없음",
    body(f'<div class="head"><span>정본 문서 5건</span><span class="badge b-review" style="margin:0">검토 대기 2건</span></div>' +
         row("review", title="문서 등급별 작성·검토 모델 교차 원칙 PRD", key="doc-tier-cross-review-prd", rev=2, ver="1.1.0",
             status_line="승인 대기 3일 · 승인은 CEO 가 합니다") + other_rows() + draft_row()))
add("err-403", "오류 · 403 권한 없음", body(head(2, False) + review_row(viewed=True, alert=ALERT_403)))
add("err-409", "오류 · 409 다른 사람이 먼저 처리(새 개정)", body(head(2, False) + review_row(viewed=False, alert=ALERT_409)))
add("err-notreview", "오류 · 409 검토 상태 아님 (지시서의 422 는 실제 서버에서 409)",
    body(head(1, False) + draft_row(open_=False) + f'<div style="margin-top:6px"></div>' +
         row("review", title="문서 등급·교차 검토 설계", key="doc-tier-cross-review-design", rev=1, ver="1.0.0",
             open_=True, extra=f'<div class="detail">{ALERT_NOTREVIEW}<div class="hint">상태가 초안/반려로 바뀌었습니다.</div></div>')))
add("err-network", "오류 · 네트워크 실패 → 자동 재조회 안내", body(head(2, False) + review_row(viewed=True, alert=ALERT_NET)))


def after_html() -> str:
    parts = []
    for sid, (title, inner, overlay) in SCENES.items():
        panel_inner = inner
        if overlay:
            panel_inner = f'<div class="body" style="position:relative;padding:0">{inner}{overlay}</div>'
        parts.append(f'<section class="scene" id="s-{sid}" data-scene="{sid}" data-title="{E(title)}">'
                     + shell(panel_inner, title, f"after · 장면: {sid} — {title}") + "</section>")
    script = ("<script>function show(){var h=(location.hash||'#default').slice(1);var f=false;"
              "document.querySelectorAll('.scene').forEach(function(s){var on=s.dataset.scene===h;s.classList.toggle('on',on);if(on)f=true});"
              "if(!f)document.getElementById('s-default').classList.add('on')}"
              "addEventListener('hashchange',show);show()</script>")
    return page("정본 승인 처리 시안 (after)", "".join(parts), script)


def sha(p: pathlib.Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def main() -> None:
    EV.mkdir(exist_ok=True)
    (ROOT / "before.html").write_text(before_html(), encoding="utf-8")
    (ROOT / "after.html").write_text(after_html(), encoding="utf-8")
    viewports = {"1440x900": (1440, 900), "390x844": (390, 844)}
    now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    assets, checks = [], []
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        jobs = [("before", "before.html", "", "before")] + [(sid, "after.html", f"#{sid}", "mockup") for sid in SCENES]
        for name, fname, frag, phase in jobs:
            for vp, (w, h) in viewports.items():
                ctx = browser.new_context(viewport={"width": w, "height": h}, device_scale_factor=1)
                pg = ctx.new_page()
                pg.goto((ROOT / fname).as_uri() + frag)
                pg.wait_for_timeout(150)
                out = EV / (f"before_{vp}.png" if name == "before" else f"after_{name}_{vp}.png")
                pg.screenshot(path=str(out))
                m = pg.evaluate("""() => {
                  const vis = e => e.offsetParent !== null && e.getBoundingClientRect().width > 0;
                  const btns = [...document.querySelectorAll('.scene.on .btn, .scene.on .chip, .scene.on .view')].filter(vis);
                  const small = btns.filter(b => b.getBoundingClientRect().height < 43.5).length;
                  return {scrollW: document.documentElement.scrollWidth, innerW: innerWidth, buttons: btns.length, small};
                }""")
                checks.append({"scene": name, "viewport": vp, **m})
                assets.append({
                    "asset_id": f"mockup-{name}-{vp}", "role": "primary", "uri": f"internal://{REL}/evidence/{out.name}",
                    "sha256": sha(out), "byte_size": out.stat().st_size, "mime": "image/png",
                    "screen_id": "chat-artifact-files-canonical", "phase": phase, "viewport": vp,
                    "fixture_id": "sample-docs", "state": "default" if name == "before" else name,
                    "captured_at": now, "capture_source": "static_html_render", "redaction_status": "not_required",
                })
                ctx.close()
        browser.close()
    bad = [c for c in checks if c["scene"] != "before" and (c["scrollW"] > c["innerW"] or c["small"])]
    manifest = {
        "screens": [{
            "screen_id": "chat-artifact-files-canonical",
            "title": "채팅 > 아티팩트 패널 > 문서파일 탭 — 정본 문서 목록의 검토·승인 처리",
            "route": "/chat",
            "requirement_ids": ["PRD-4-1", "PRD-4-2", "PRD-5-1", "PRD-5-3", "PRD-7", "PRD-8"],
            "states_not_applicable": {
                "session-expired": "로그인 만료는 PRD §7 의 401 문구와 [로그인] 링크를 따른다(장면 생략)",
            },
        }],
        "assets": assets,
        "source_sha": SOURCE_SHA,
        "document_key": "chat-artifact-canonical-approve-prd",
        "checks": checks,
        "check_failures": bad,
    }
    (ROOT / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"scenes={len(SCENES)} png={len(assets)} failures={len(bad)}")
    for c in bad:
        print("FAIL", c)


if __name__ == "__main__":
    main()
