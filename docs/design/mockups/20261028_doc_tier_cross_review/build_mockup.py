#!/usr/bin/env python3
"""문서 3등급 + 작성·검토 교차 회사 원칙 설정 화면 시안 생성기 (2026-10-28, AADS).

현행 '러너 모델 우선순위'(settings/page.tsx RunnerModelConfig)와 정본 문서 화면
(docs/CanonicalDocuments.tsx)의 구조·CSS 변수를 그대로 따른 정적 HTML 을 만들고,
Playwright chromium 으로 데스크톱 1440 / 모바일 390 PNG 를 evidence/ 에 캡처한 뒤
manifest.json(screens/assets/evidence)을 쓴다.

실행: cd docs/design/mockups/20261028_doc_tier_cross_review && timeout 300 python3 build_mockup.py
정본: PRD doc-tier-cross-review-prd, 설계 doc-tier-cross-review-design
"""
from __future__ import annotations

import datetime
import hashlib
import html
import json
import pathlib

from playwright.sync_api import sync_playwright

ROOT = pathlib.Path(__file__).resolve().parent
REL = "docs/design/mockups/20261028_doc_tier_cross_review"
SOURCE_SHA = "e4e4e970"  # aads-dashboard origin/main (settings/page.tsx · CanonicalDocuments.tsx 기준)
E = html.escape

# ---- 현재 DB 값 (runner_model_config, 2026-10-28 15:3x KST 실측) ----
DB = {
    "XS": ["codex:gpt-6-luna", "claude-sonnet-5-5", "codex:gpt-5.6-luna", "codex:gpt-6.1-sol"],
    "S": ["claude-sonnet-5-5", "codex:gpt-6.1-sol", "codex:gpt-6-luna", "claude-opus-5-5"],
    "M": ["claude-sonnet-5-5", "codex:gpt-6.1-sol", "claude-opus-5-5", "codex:gpt-6-astra"],
    "L": ["codex:gpt-6.1-sol", "claude-opus-5-5", "claude-sonnet-5-5", "codex:gpt-6-astra"],
    "XL": ["claude-opus-5-5", "codex:gpt-6-astra", "claude-fable-5-1", "codex:gpt-6.1-sol"],
    "AI_REVIEW": ["claude-sonnet-5-5", "codex:gpt-6.1-sol", "claude-opus-5-5", "codex:gpt-6-astra"],
}
SIZE_LABELS = {"XS": "XS (초소형)", "S": "S (소형)", "M": "M (중형)", "L": "L (대형)", "XL": "XL (초대형)",
               "AI_REVIEW": "AI Review (코드 리뷰)"}

# 제안 초기값 — 설계 §5-1 (확정 아님, 저장 전 미리보기용)
NEW = {
    "AI_REVIEW_LIGHT": ["claude-sonnet-5-5", "codex:gpt-6-luna", "codex:gpt-6.1-sol"],
    "AI_REVIEW_STD": ["codex:gpt-6.1-sol", "claude-opus-5-5", "codex:gpt-6-astra"],
    "AI_REVIEW_HEAVY": ["claude-opus-5-5", "codex:gpt-6-astra", "codex:gpt-6.1-sol", "claude-fable-5-1"],
}


def provider_of(model: str) -> str:
    m = model.lower()
    if m.startswith(("claude:", "claude-")):
        return "anthropic"
    if m.startswith("codex:"):
        return "openai"
    return "unknown"


CO = {"anthropic": "Claude", "openai": "Codex"}


def first_cross(models: list[str], author: str) -> str | None:
    ap = provider_of(author)
    for m in models:
        if m != author and provider_of(m) in CO and provider_of(m) != ap:
            return m
    return None


def cross_check(models: list[str], author: str) -> dict:
    """설정 1순위가 작성 회사와 같으면 same=True, replacement=교체될 모델."""
    top = models[0]
    ok = provider_of(top) != provider_of(author) and top != author
    return {"ok": ok, "top": top, "pick": first_cross(models, author)}


CSS = """
:root{--bg-primary:#0f172a;--bg-card:#1e293b;--bg-hover:#334155;--text-primary:#e2e8f0;--text-secondary:#94a3b8;
--accent:#3b82f6;--success:#22c55e;--warning:#f59e0b;--danger:#ef4444;--border:#334155}
*{box-sizing:border-box}
body{margin:0;background:var(--bg-primary);color:var(--text-primary);font-family:-apple-system,"Segoe UI","Noto Sans KR","Apple SD Gothic Neo",sans-serif;font-size:14px;line-height:1.45}
.mono{font-family:ui-monospace,"SF Mono",Menlo,Consolas,monospace}
header.app{height:48px;display:flex;align-items:center;padding:0 24px;border-bottom:1px solid var(--border);font-weight:700}
.toptabs{display:flex;gap:4px;border-bottom:1px solid var(--border);padding:12px 24px 0}
.toptabs button{background:transparent;border:none;color:var(--text-secondary);padding:10px 16px;font-size:14px;font-weight:600;position:relative}
.toptabs button.on{color:var(--accent)}.toptabs button.on:after{content:"";position:absolute;left:8px;right:8px;bottom:0;height:2px;background:var(--accent);border-radius:1px}
main{padding:24px;display:flex;flex-direction:column;gap:20px}
.pills{display:flex;gap:8px;flex-wrap:wrap}
.pill{padding:6px 16px;font-size:12px;font-weight:600;border-radius:999px;background:var(--bg-hover);color:var(--text-secondary);border:1px solid var(--border)}
.pill.on{background:var(--accent);border-color:var(--accent);color:#fff}
section.card{background:var(--bg-card);border:1px solid var(--border);border-radius:12px;padding:20px}
section.card h2{font-size:14px;font-weight:600;margin:0 0 4px}
section.card h2.sub{font-size:13px;margin:22px 0 4px}
.muted{color:var(--text-secondary)}.xs{font-size:12px}.x11{font-size:11px}
.grid2{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px}
.grid3{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:16px}
.cfg{background:var(--bg-hover);border:1px solid var(--border);border-radius:8px;padding:16px;position:relative}
.cfg.hl{outline:2px dashed var(--warning);outline-offset:2px}
.cfg h3{font-size:14px;font-weight:700;margin:0 0 12px;display:flex;justify-content:space-between;gap:8px;align-items:baseline}
.cfg h3 .sizes{font-size:11px;font-weight:500;color:var(--text-secondary)}
.cfg.ai h3{color:var(--accent)}
.row{display:flex;align-items:center;gap:8px;background:var(--bg-primary);border-radius:4px;padding:8px 12px;margin-bottom:8px;min-width:0}
.row .n{width:20px;text-align:center;font-size:12px;font-weight:700;color:var(--text-secondary)}
.row .n.first{color:var(--success)}
.row .m{flex:1;min-width:0;font-size:14px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.row .ctl{font-size:12px;padding:2px 6px;border-radius:4px;background:var(--bg-hover);color:var(--text-primary)}
.row .ctl.x{color:var(--danger);background:transparent}
.row.hl{outline:2px solid var(--warning)}
.co{font-size:10px;font-weight:700;border-radius:4px;padding:1px 6px;color:#fff;white-space:nowrap}
.co.anthropic{background:#c2410c}.co.openai{background:#0f766e}
.addrow{display:flex;gap:8px;margin-top:8px}
.addrow .sel{flex:1;min-width:0;background:var(--bg-primary);border:1px solid var(--border);border-radius:4px;padding:6px 12px;font-size:14px;color:var(--text-secondary)}
.btn{border:none;border-radius:4px;padding:6px 12px;font-size:12px;font-weight:700;background:var(--accent);color:#fff}
.btn.lg{padding:8px 20px;font-size:14px;border-radius:8px}
.btn.ghost{background:var(--bg-hover);color:var(--text-secondary);border:1px solid var(--border);font-weight:500}
.btn.mini{background:var(--bg-hover);color:var(--text-primary);font-size:11px;padding:3px 8px;border:1px solid var(--border)}
.chain{margin-top:12px;border-radius:8px;padding:8px 12px;background:rgba(16,163,127,.08);border:1px solid rgba(16,163,127,.18)}
.chain b{color:var(--success);font-size:11px;display:block;margin-bottom:4px}
.chain span{font-size:11px;color:var(--text-secondary);word-break:break-all}
.actions{display:flex;gap:12px;align-items:center;padding-top:8px;flex-wrap:wrap}
/* 교차 회사 배지 */
.xchk{display:flex;flex-direction:column;gap:6px;margin-top:10px}
.badge{display:inline-flex;gap:6px;align-items:flex-start;border-radius:8px;padding:6px 10px;font-size:12px;line-height:1.4}
.badge.ok{background:rgba(34,197,94,.12);border:1px solid rgba(34,197,94,.3);color:#86efac}
.badge.warn{background:rgba(245,158,11,.12);border:1px solid rgba(245,158,11,.35);color:#fbbf24}
.badge.block{background:rgba(239,68,68,.12);border:1px solid rgba(239,68,68,.35);color:#fca5a5}
.badge b{font-weight:700}
/* 등급 */
.tier{display:inline-block;font-weight:800;font-size:12px;border-radius:6px;padding:2px 8px;white-space:nowrap}
.tier.D1{background:rgba(148,163,184,.2);color:#cbd5e1;border:1px solid #64748b}
.tier.D2{background:rgba(59,130,246,.18);color:#93c5fd;border:1px solid #3b82f6}
.tier.D3{background:rgba(245,158,11,.18);color:#fcd34d;border:1px solid #f59e0b}
.tier.SPLIT{background:rgba(239,68,68,.15);color:#fca5a5;border:1px solid #ef4444}
table.tiers{width:100%;border-collapse:separate;border-spacing:0;margin-top:10px;font-size:13px}
table.tiers th{text-align:left;font-size:11px;color:var(--text-secondary);font-weight:600;padding:8px 10px;border-bottom:1px solid var(--border);white-space:nowrap}
table.tiers td{vertical-align:top;padding:12px 10px;border-bottom:1px solid var(--border)}
table.tiers tr:last-child td{border-bottom:none}
.chips{display:flex;flex-wrap:wrap;gap:4px}
.chip{background:var(--bg-primary);border:1px solid var(--border);border-radius:4px;padding:2px 6px;font-size:12px}
.chip.first{border-color:var(--success)}
.kv{display:flex;flex-direction:column;gap:2px}
.kv small{font-size:11px;color:var(--text-secondary)}
.prev{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;margin-top:10px}
.pdoc{background:var(--bg-hover);border:1px solid var(--border);border-radius:8px;padding:14px;display:flex;flex-direction:column;gap:8px}
.pdoc h4{margin:0;font-size:13px}
.pdoc .meta{font-size:12px;color:var(--text-secondary)}
.pdoc .flow{display:flex;flex-direction:column;gap:6px;font-size:12px}
.pdoc .flow div{display:flex;gap:6px;align-items:flex-start;flex-wrap:wrap}
.pdoc .flow b{min-width:52px;color:var(--text-secondary);font-weight:600}
/* 시안 주석 (실제 화면 아님) */
.note{position:absolute;top:-10px;right:-6px;background:var(--warning);color:#111;font-weight:800;font-size:12px;border-radius:999px;width:24px;height:24px;display:flex;align-items:center;justify-content:center;box-shadow:0 0 0 3px var(--bg-card)}
.legend{border:1px dashed var(--warning);border-radius:8px;padding:12px 14px;font-size:12px;background:rgba(245,158,11,.06)}
.legend h3{margin:0 0 6px;font-size:12px;color:var(--warning)}
.legend ol{margin:0;padding-left:20px;display:flex;flex-direction:column;gap:4px}
.mocktag{font-size:11px;color:var(--warning);border:1px dashed var(--warning);border-radius:4px;padding:1px 6px;margin-left:8px;font-weight:600}
.cmp{width:100%;border-collapse:collapse;font-size:12px;margin-top:8px}
.cmp th,.cmp td{border:1px solid var(--border);padding:6px 8px;text-align:left}
.cmp th{background:var(--bg-hover);color:var(--text-secondary);font-weight:600}
.cmp tr.bad td{background:rgba(245,158,11,.1)}
.cmp .tag{font-weight:700}.cmp .tag.ok{color:var(--success)}.cmp .tag.bad{color:var(--warning)}
/* 정본 문서 화면 */
.docs{padding:16px 20px;display:flex;flex-direction:column;gap:12px}
.docs .projects{display:flex;gap:8px;overflow-x:auto;align-items:center;white-space:nowrap}
.docs .projects span.l{font-weight:600}
.docs .projects button{min-height:40px;border-radius:8px;padding:0 12px;background:transparent;border:1px solid var(--border);color:var(--text-primary);font-size:14px}
.docs .projects button.on{border-color:var(--accent);background:rgba(59,130,246,.15)}
.docs .cols{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1.1fr);gap:12px}
.panel{border:1px solid var(--border);border-radius:12px;padding:12px;min-width:0}
.panel h2{margin:0 0 8px;font-size:16px;font-weight:600}
.srch{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:8px}
.srch .in{flex:1;min-width:140px;min-height:44px;border:1px solid var(--border);border-radius:8px;padding:0 12px;display:flex;align-items:center;color:var(--text-secondary);font-size:14px}
.srch .f{min-height:44px;border:1px solid var(--border);border-radius:8px;padding:0 10px;display:flex;align-items:center;font-size:13px;background:var(--bg-card)}
.li{display:block;border:1px solid var(--border);border-radius:8px;padding:10px 12px;margin-bottom:6px;background:var(--bg-card)}
.li.sel{border-color:var(--accent);background:rgba(59,130,246,.12)}
.li .t{font-size:14px;font-weight:600;word-break:break-word}
.li .s{font-size:12px;color:var(--text-secondary);margin-top:2px}
.li .b{display:flex;flex-wrap:wrap;gap:6px;margin-top:6px;align-items:center}
.st{font-size:11px;border-radius:999px;padding:1px 8px;border:1px solid var(--border)}
.st.draft{color:#cbd5e1}.st.review{color:#fbbf24;border-color:#f59e0b}.st.approved{color:#86efac;border-color:#22c55e}
.rv{font-size:11px;color:var(--text-secondary)}
.rv.pass{color:#86efac}.rv.rej{color:#fca5a5}.rv.wait{color:#fbbf24}
.detail h3.title{font-size:16px;margin:0 0 4px}
.detail p{margin:2px 0}
.revbox{border:1px solid var(--border);border-radius:8px;padding:10px 12px;margin:10px 0;background:var(--bg-card)}
.revbox h4{margin:0 0 8px;font-size:13px}
.revbox .line{display:flex;gap:8px;align-items:center;flex-wrap:wrap;font-size:13px;padding:5px 0;border-top:1px solid var(--border)}
.revbox .line:first-of-type{border-top:none}
.revbox .line .k{min-width:84px;color:var(--text-secondary);font-size:12px}
.act{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:6px 0}
.act button{min-height:44px;border-radius:8px;padding:0 16px;border:none;font-weight:600;font-size:14px;background:var(--accent);color:#fff}
.act button.off{background:var(--bg-hover);color:var(--text-secondary)}
@media (max-width:767px){
  header.app{padding:0 12px}.toptabs{padding:8px 12px 0;overflow-x:auto}.toptabs button{padding:10px 12px;white-space:nowrap}
  main{padding:12px;gap:14px}section.card{padding:14px}
  .grid2,.grid3,.prev,.docs .cols{grid-template-columns:minmax(0,1fr)}
  .row{padding:10px 8px;flex-wrap:wrap}.row .m{flex:1 1 150px;white-space:normal;overflow:visible;word-break:break-all}.row .ctl{padding:8px 10px;min-width:36px;text-align:center}
  .btn{min-height:44px}.btn.lg{width:100%}.addrow .sel{min-height:44px}
  table.tiers,table.tiers tbody,table.tiers tr,table.tiers td{display:block;width:100%}
  table.tiers thead{display:none}
  table.tiers tr{border:1px solid var(--border);border-radius:8px;padding:10px;margin-bottom:10px;background:var(--bg-hover)}
  table.tiers td{border:none;padding:6px 0}
  table.tiers td:before{content:attr(data-l);display:block;font-size:11px;color:var(--text-secondary);margin-bottom:2px}
  .docs{padding:10px}.note{right:2px}
  .cmp{display:block;overflow-x:auto}
}
"""

DOC_HEAD = ('<!doctype html><html lang="ko"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1"><title>{t}</title>'
            "<style>" + CSS + "</style></head><body>")


def co_chip(model: str) -> str:
    p = provider_of(model)
    return f'<span class="co {p}">{CO.get(p, "기타")}</span>' if p in CO else ""


def model_rows(models: list[str], *, chips: bool = False, hl_first: bool = False, ctl: bool = True) -> str:
    out = []
    for i, m in enumerate(models):
        hl = " hl" if (hl_first and i == 0) else ""
        c = co_chip(m) if chips else ""
        controls = ('<span class="ctl">▲</span><span class="ctl">▼</span><span class="ctl x">✕</span>' if ctl else "")
        out.append(f'<div class="row{hl}"><span class="n{" first" if i == 0 else ""}">{i + 1}</span>'
                   f'<span class="m mono">{E(m)}</span>{c}{controls}</div>')
    return "".join(out)


def add_row() -> str:
    return '<div class="addrow"><div class="sel mono">모델 선택...</div><span class="btn">추가</span></div>'


def chrome(inner: str, title: str, sub_active: int = 0, tab_active: int = 0) -> str:
    tabs = "".join(f'<button class="{"on" if i == tab_active else ""}">{t}</button>'
                   for i, t in enumerate(["모델 설정", "LLM 관리", "시스템"]))
    pills = "".join(f'<span class="pill{" on" if i == sub_active else ""}">{t}</span>'
                    for i, t in enumerate(["러너 모델", "지시서 생성", "모델 라우팅", "채팅창 노출 순서"]))
    return (DOC_HEAD.replace('{t}', E(title)) + f'<header class="app">Settings</header><div class="toptabs">{tabs}</div>'
            f'<main><div class="pills">{pills}</div>{inner}</main></body></html>')


# ------------------------------------------------------------------ BEFORE
def build_before() -> str:
    cards = []
    notes = {"S": "②", "M": "②", "XL": "②", "AI_REVIEW": "①"}
    for size in ["XS", "S", "M", "L", "XL", "AI_REVIEW"]:
        same = size in ("S", "M", "XL")
        cls = "cfg ai" if size == "AI_REVIEW" else "cfg"
        if same:
            cls += " hl"
        n = f'<span class="note">{notes[size]}</span>' if size in notes else ""
        cards.append(
            f'<div class="{cls}">{n}<h3><span>{E(SIZE_LABELS[size])}</span>'
            f'<span class="sizes">2026. 10. 28. 오전 7:19</span></h3>'
            f'{model_rows(DB[size], hl_first=same)}'
            f'<div class="chain"><b>실제 자동 폴백 체인</b><span class="mono">{" → ".join(DB[size])}</span></div>'
            f'{add_row()}</div>')
    rows = []
    for size in ["XS", "S", "M", "L", "XL"]:
        a, r = DB[size][0], DB["AI_REVIEW"][0]
        same = provider_of(a) == provider_of(r)
        rows.append(f'<tr class="{"bad" if same else ""}"><td>{size}</td><td class="mono">{E(a)} ({CO[provider_of(a)]})</td>'
                    f'<td class="mono">{E(r)} ({CO[provider_of(r)]})</td>'
                    f'<td><span class="tag {"bad" if same else "ok"}">{"⚠️ 같은 회사" if same else "✅ 다른 회사"}</span></td></tr>')
    legend = (
        '<div class="legend"><h3>시안 주석 — 실제 화면에는 없음</h3><ol>'
        '<li><b>검수 순서가 1개뿐</b>입니다. 작업 크기(XS~XL)와 문서 종류에 관계없이 같은 AI Review 순서(4개)를 씁니다.</li>'
        '<li><b>S·M·XL 은 작성 1순위(claude)와 검수 1순위(claude-sonnet-5-5)가 같은 회사</b>입니다 (노란 점선). '
        '같은 회사 모델이 쓰고 검수합니다. 5개 크기 중 3개.</li></ol>'
        '<table class="cmp"><thead><tr><th>크기</th><th>작성 1순위</th><th>검수 1순위(AI Review)</th><th>교차 여부</th></tr></thead>'
        f'<tbody>{"".join(rows)}</tbody></table></div>')
    body = (
        '<section class="card"><h2>러너 모델 우선순위<span class="mocktag">변경 전 · 현재 DB 값 재구성</span></h2>'
        '<p class="xs muted" style="margin:0 0 16px">Size별 모델 실행 순서를 설정합니다. 1순위 실패 시 다음 순위로 자동 폴백됩니다.</p>'
        f'<div class="grid2">{"".join(cards)}</div>'
        '<p class="xs muted" style="margin-top:16px">러너는 저장된 size 순서를 먼저 쓰고, 실패하거나 비어 있으면 AI Review 설정과 LLM 라우팅 순서로 자동 폴백합니다.</p>'
        '<div class="actions"><span class="btn lg">설정 저장</span><span class="btn lg ghost">초기화</span></div></section>'
        f'{legend}')
    return chrome(body, "변경 전 — 러너 모델 우선순위 (현재 화면)")


# ------------------------------------------------------------------ AFTER
def tier_badges(models: list[str], covers: list[tuple[str, str]]) -> str:
    out = []
    for label, author in covers:
        c = cross_check(models, author)
        if c["ok"]:
            out.append(f'<div class="badge ok"><span>✅</span><span><b>교차 회사 확인</b> · {E(label)} 작성 '
                       f'<span class="mono">{E(author)}</span>({CO[provider_of(author)]}) → 검수 '
                       f'<span class="mono">{E(c["top"])}</span>({CO[provider_of(c["top"])]}) · 다른 회사</span></div>')
        else:
            out.append(f'<div class="badge warn"><span>⚠️</span><span><b>교차 회사 확인</b> · {E(label)} 작성 '
                       f'<span class="mono">{E(author)}</span>({CO[provider_of(author)]}) = 검수 1순위 '
                       f'<span class="mono">{E(c["top"])}</span> 같은 회사 → 자동 교체: '
                       f'<b class="mono">{E(c["pick"])}</b></span></div>')
    return f'<div class="xchk">{"".join(out)}</div>'


def build_after() -> str:
    # 작성 순서(변경 없음) 요약
    summary_rows = "".join(
        f'<tr><td data-l="작업 크기"><b>{s}</b></td><td data-l="작성 순서"><div class="chips">'
        + "".join(f'<span class="chip mono{" first" if i == 0 else ""}">{E(m)}</span>' for i, m in enumerate(DB[s][:3]))
        + '<span class="chip muted">…</span></div></td></tr>' for s in ["XS", "S", "M", "L", "XL"])
    block_work = (
        '<h2 class="sub">작업 모델 순서 <span class="muted xs">· 변경 없음 (XS~XL)</span></h2>'
        '<p class="xs muted" style="margin:0 0 6px">크기별 작성 순서입니다. 기존 5개 카드 그대로이며 여기서는 앞 3개만 요약합니다.</p>'
        f'<table class="tiers"><thead><tr><th>작업 크기</th><th>작성 순서</th></tr></thead><tbody>{summary_rows}</tbody></table>')

    light = tier_badges(NEW["AI_REVIEW_LIGHT"], [("XS", DB["XS"][0]), ("S", DB["S"][0])])
    std = tier_badges(NEW["AI_REVIEW_STD"], [("M", DB["M"][0])])
    heavy = tier_badges(NEW["AI_REVIEW_HEAVY"], [("L", DB["L"][0]), ("XL", DB["XL"][0])])

    def card(name, key, sizes, models, badges, n):
        return (f'<div class="cfg ai"><span class="note">{n}</span><h3><span>{name} <span class="muted xs mono">({key})</span></span>'
                f'<span class="sizes">{sizes}</span></h3>{model_rows(models, chips=True)}{add_row()}{badges}</div>')

    block_code = (
        '<h2 class="sub">코드 검수 <span class="muted xs">· 작업 크기별 3줄 (기존 AI Review 1줄을 나눔)</span></h2>'
        '<p class="xs muted" style="margin:0 0 10px">실제 작성 모델의 반대편 회사를 1순위로 둡니다. 같은 회사가 1순위가 되면 다음 순위의 다른 회사 모델로 자동 교체됩니다.</p>'
        '<div class="grid3">'
        + card("경량 검수", "AI_REVIEW_LIGHT", "XS · S", NEW["AI_REVIEW_LIGHT"], light, "①")
        + card("보통 검수", "AI_REVIEW_STD", "M", NEW["AI_REVIEW_STD"], std, "①")
        + card("상위 검수", "AI_REVIEW_HEAVY", "L · XL", NEW["AI_REVIEW_HEAVY"], heavy, "①")
        + '</div>'
        '<p class="x11 muted" style="margin-top:8px">기존 「AI Review (코드 리뷰)」 순서는 작성 실패 시 마지막 폴백과 구버전 호환용으로 그대로 유지됩니다 (<span class="mono">AI_REVIEW</span>).</p>')

    # 문서 등급 표
    def doc_row(tier, label, crit, author, review_html, approve, badge_html):
        return (f'<tr><td data-l="등급"><span class="tier {tier}">{tier}</span><div class="xs" style="margin-top:4px">{label}</div></td>'
                f'<td data-l="기준 (종류 · 글자 수 · 목표 연결)">{crit}</td>'
                f'<td data-l="작성 순서">{author}</td><td data-l="검토 모델">{review_html}</td>'
                f'<td data-l="승인 방식">{approve}</td><td data-l="교차 회사 확인">{badge_html}</td></tr>')

    d1c = cross_check(NEW["AI_REVIEW_LIGHT"], DB["S"][0])
    d2c = cross_check(NEW["AI_REVIEW_STD"], DB["M"][0])
    d3_cross = first_cross(NEW["AI_REVIEW_HEAVY"], DB["L"][0])
    d3_same = next(m for m in NEW["AI_REVIEW_HEAVY"] if provider_of(m) == provider_of(DB["L"][0]) and m != DB["L"][0] and m != d3_cross)

    def chips(ms):
        return '<div class="chips">' + "".join(
            f'<span class="chip mono{" first" if i == 0 else ""}">{E(m)}</span>' for i, m in enumerate(ms)) + '</div>'

    def b_ok(txt):
        return f'<div class="badge ok"><span>✅</span><span><b>다른 회사</b> · {txt}</span></div>'

    def b_warn(txt):
        return f'<div class="badge warn"><span>⚠️</span><span><b>같은 회사</b> · {txt}</span></div>'

    d1_badge = (b_ok("작성 1순위와 다른 회사") if d1c["ok"] else
                b_warn(f'설정 1순위 <span class="mono">{E(d1c["top"])}</span> → 자동 교체 <b class="mono">{E(d1c["pick"])}</b>'))
    d2_badge = (b_ok(f'<span class="mono">{E(d2c["top"])}</span> ({CO[provider_of(d2c["top"])]})') if d2c["ok"] else
                b_warn(f'자동 교체 <b class="mono">{E(d2c["pick"])}</b>'))
    d3_badge = b_ok(f'Claude·Codex 양쪽 검토 · 반대편 <span class="mono">{E(d3_cross)}</span>')

    table = (
        '<table class="tiers"><thead><tr><th>등급</th><th>기준 (종류 · 글자 수 · 목표 연결)</th><th>작성 순서</th>'
        '<th>검토 모델</th><th>승인 방식</th><th>교차 회사 확인</th></tr></thead><tbody>'
        + doc_row("D1", "가벼움", "보고서·참고 자료이면서 <b>15,000자 이하</b>", chips(DB["S"][:3]) + '<div class="x11 muted">러너 S 순서</div>',
                  f'<div class="kv"><b class="mono">{E(d1c["pick"] or d1c["top"])}</b><small>다른 회사 경량 모델 1개 ({"설정 1순위" if d1c["ok"] else "자동 교체"})</small></div>',
                  '<b>정본 승인 없음</b><div class="x11 muted">등록·기록은 유지</div>', d1_badge)
        + doc_row("D2", "보통", "계획·디자인·명세이면서 <b>15,000자 이하</b>", chips(DB["M"][:3]) + '<div class="x11 muted">러너 M 순서</div>',
                  f'<div class="kv"><b class="mono">{E(d2c["pick"] or d2c["top"])}</b><small>다른 회사 상위 모델 1개</small></div>',
                  '<b>PM 검토 → CEO 승인</b>', d2_badge)
        + doc_row("D3", "중요", "제품 요구사항·계약·아키텍처 · <b>15,000자 초과</b> · <b>목표에 연결</b> 중 하나라도",
                  chips(DB["L"][:3]) + '<div class="x11 muted">러너 L·XL 순서</div>',
                  f'<div class="kv"><b class="mono">{E(d3_cross)}</b> <span class="xs muted">+</span> <b class="mono">{E(d3_same)}</b>'
                  '<small>두 회사 상위 모델이 각각 검토 · 둘 다 통과</small></div>',
                  '<b>PM 검토 → CEO 승인</b>', d3_badge)
        + '<tr><td data-l="등급"><span class="tier SPLIT">분할 권고</span></td><td data-l="기준 (종류 · 글자 수 · 목표 연결)"><b>50,000자 초과</b></td>'
          '<td data-l="작성 순서">—</td><td data-l="검토 모델">검토 전에 나눠 쓰도록 반려</td><td data-l="승인 방식">—</td>'
          '<td data-l="교차 회사 확인"><span class="xs muted">해당 없음</span></td></tr>'
        + '</tbody></table>')
    block_doc = (
        '<h2 class="sub">문서 작성·검토 <span class="muted xs">· 새 블록 (등급별)</span><span class="mocktag">신규</span></h2>'
        '<p class="xs muted" style="margin:0 0 4px">문서 등급은 글자 수·종류·목표 연결로 자동 정해집니다. 검토 모델은 항상 작성 모델과 다른 회사에서 고릅니다. 둘 이상 걸리면 높은 등급이 이깁니다.</p>'
        + table
        + '<div class="badge block" style="margin-top:10px"><span>⛔</span><span><b>대체 불가 시 차단</b> · 다른 회사 모델이 하나도 없으면 같은 회사로 대신 검토하지 않고 검토를 멈춘 뒤 알립니다.</span></div>')

    def pdoc(title, meta, tier, author, reviewers, approve):
        rv = "".join(f'<span class="chip mono">{E(m)}</span> {co_chip(m)} ' for m in reviewers)
        return (f'<div class="pdoc"><h4>{E(title)} <span class="mocktag">예시</span></h4><div class="meta">{E(meta)}</div>'
                f'<div class="flow"><div><b>등급</b><span class="tier {tier}">{tier}</span></div>'
                f'<div><b>작성</b><span class="chip mono">{E(author)}</span> {co_chip(author)}</div>'
                f'<div><b>검토</b>{rv}</div><div><b>승인</b><span>{approve}</span></div></div></div>')

    block_prev = (
        '<h2 class="sub">저장 전 미리보기 <span class="muted xs">· 예시 문서 3건이 가는 곳</span></h2>'
        '<div class="prev">'
        + pdoc("주간 점검 보고서", "보고서 · 4,200자 · 목표 연결 없음", "D1", DB["S"][0], [d1c["pick"] or d1c["top"]], "정본 승인 없음 (기록만)")
        + pdoc("러너 대기열 설계", "디자인 · 9,800자 · 목표 연결 없음", "D2", DB["M"][0], [d2c["pick"] or d2c["top"]], "PM 검토 → CEO 승인")
        + pdoc("문서 등급 제품 요구사항", "제품 요구사항 · 31,000자 · 목표 연결", "D3", DB["L"][0], [d3_cross, d3_same], "PM 검토 → CEO 승인 (둘 다 통과 시)")
        + '</div>')

    legend = (
        '<div class="legend"><h3>시안 주석 — 실제 화면에는 없음</h3><ol>'
        '<li>코드 검수 1줄 → <b>경량 / 보통 / 상위 3줄</b>. 각 카드에 교차 회사 배지(✅ 다른 회사 / ⚠️ 같은 회사 → 자동 교체 모델).</li></ol>'
        '<p class="xs muted" style="margin:6px 0 0">경량 카드의 S 줄이 ⚠️ 로 나오는 것은 의도한 시연입니다: 제안 초기값에서 1순위 <span class="mono">claude-sonnet-5-5</span> 는 S 작성 1순위와 같은 회사라 '
        '<span class="mono">codex:gpt-6-luna</span> 로 교체됩니다. 상위 카드의 XL 줄도 같은 이유입니다. 모델 순서·값은 제안이며 확정이 아닙니다.</p></div>')

    body = (
        '<section class="card"><h2>러너 모델 우선순위<span class="mocktag">변경 후 · 시안</span></h2>'
        '<p class="xs muted" style="margin:0 0 4px">Size별 모델 실행 순서를 설정합니다. 1순위 실패 시 다음 순위로 자동 폴백됩니다.</p>'
        + block_work + block_code + block_doc + block_prev +
        '<p class="xs muted" style="margin-top:16px">러너는 저장된 size 순서를 먼저 쓰고, 실패하거나 비어 있으면 AI Review 설정과 LLM 라우팅 순서로 자동 폴백합니다.</p>'
        '<div class="actions"><span class="btn lg">설정 저장</span><span class="btn lg ghost">초기화</span>'
        '<span class="xs muted">저장하면 이후 등록되는 문서부터 적용됩니다. 기존 문서는 소급 적용하지 않습니다.</span></div></section>'
        + legend)
    return chrome(body, "변경 후 — 러너 모델 우선순위 (문서 등급·교차 검토)")


# ------------------------------------------------------------------ 정본 문서 화면
def build_docs_after() -> str:
    def li(title, kind, ver, status, tier, author, rv, sel=False):
        rvcls = {"pass": "pass", "rej": "rej", "wait": "wait", "none": ""}[rv[0]]
        return (f'<div class="li{" sel" if sel else ""}"><div class="t">{E(title)}</div>'
                f'<div class="s">{E(kind)} · 버전 {ver} · <span class="st {status[0]}">{status[1]}</span></div>'
                f'<div class="b"><span class="tier {tier}">{tier}</span><span class="rv">작성 {E(author)}</span>'
                f'<span class="rv {rvcls}">{E(rv[1])}</span></div></div>')

    lst = (
        li("문서 등급별 작성·검토 모델 교차 원칙 PRD", "제품 요구사항", "0.1.0", ("draft", "초안"), "D3", "codex:gpt-6.1-sol",
           ("wait", "검토 1/2 통과 · 대기 중"), sel=True)
        + li("문서 등급·교차 검토 설계", "디자인", "0.1.0", ("review", "검토"), "D3", "codex:gpt-6.1-sol", ("pass", "검토 2/2 통과"))
        + li("러너 대기열 설계", "디자인", "1.2.0", ("approved", "승인"), "D2", "claude-sonnet-5-5", ("pass", "codex:gpt-6.1-sol 통과"))
        + li("주간 점검 보고서", "보고서", "1.0.0", ("draft", "초안"), "D1", "claude-sonnet-5-5", ("pass", "codex:gpt-6-luna 통과 · 승인 불필요"))
        + li("전체 운영 매뉴얼", "참고 자료", "3.0.0", ("draft", "초안"), "SPLIT", "claude-opus-5-5", ("rej", "분할 권고 · 52,300자"))
        + li("2026-09 이전 보고서", "보고서", "1.0.0", ("approved", "승인"), "D1", "미상", ("none", "등급 미산정 · 기존 문서")))

    cols = (
        '<div class="docs"><div class="projects"><span class="l">프로젝트</span>'
        '<button class="on">AADS</button><button>KIS</button><button>GO100</button><button>ACCT</button></div>'
        '<div class="cols"><section class="panel"><h2>문서 정본 목록</h2>'
        '<div class="srch"><div class="in">문서명 또는 내용 검색</div><span class="btn ghost" style="min-height:44px;display:flex;align-items:center">검색</span></div>'
        '<div class="srch"><span class="f">상태 ▾</span><span class="f">종류 ▾</span><span class="f"><b>등급 ▾</b></span>'
        '<span class="f muted xs">새 필터: 등급</span></div>'
        f'{lst}</section>'
        '<section class="panel detail"><h2>현재 문서와 개정 이력</h2>'
        '<h3 class="title">문서 등급별 작성·검토 모델 교차 원칙 PRD</h3>'
        '<p>최신 개정판 · 종류: 제품 요구사항 · 버전: 0.1.0 · 상태: 초안 · <span class="tier D3">D3 중요</span></p>'
        '<p class="xs muted">문서 키: doc-tier-cross-review-prd</p>'
        '<div class="revbox"><h4>작성·검토 <span class="mocktag">신규</span></h4>'
        '<div class="line"><span class="k">등급 근거</span><span>제품 요구사항 · 31,000자 · 목표 연결</span></div>'
        '<div class="line"><span class="k">작성 모델</span><span class="chip mono">codex:gpt-6.1-sol</span><span class="co openai">Codex</span><span class="xs muted">러너 작업 기록에서 자동 확인</span></div>'
        '<div class="line"><span class="k">검토 모델 1</span><span class="chip mono">claude-opus-5-5</span><span class="co anthropic">Claude</span>'
        '<span class="badge ok" style="padding:2px 8px">✅ 통과 · 다른 회사</span><span class="xs muted">10. 28. 17:12</span></div>'
        '<div class="line"><span class="k">검토 모델 2</span><span class="chip mono">codex:gpt-6-astra</span><span class="co openai">Codex</span>'
        '<span class="badge warn" style="padding:2px 8px">진행 중</span><span class="xs muted">작성 모델과 다른 모델</span></div>'
        '<div class="line"><span class="k">승인 조건</span><span>D3 — 두 검토 모두 통과해야 「검토로 이동」 가능 · CEO 승인</span></div></div>'
        '<div class="act"><h3 style="margin:0;font-size:14px;width:100%">상태 변경</h3>'
        '<button class="off">검토로 이동</button><span class="xs muted">두 번째 검토가 끝나야 검토로 이동할 수 있습니다.</span></div>'
        '<div class="act"><button class="off">정본 승인</button><span class="xs muted">검토 상태에서만 승인할 수 있습니다.</span></div>'
        '<h3 style="font-size:14px;margin:12px 0 6px">개정 이력</h3>'
        '<div class="revbox" style="margin:0"><div class="xs"><b>문서 등급별 작성·검토 모델 교차 원칙 PRD · v0.1.0</b></div>'
        '<div class="xs muted">개정 1 · 초안 · D3 · 작성 codex:gpt-6.1-sol · 검토 claude-opus-5-5 통과</div></div>'
        '</section></div></div>')
    return (DOC_HEAD.replace("{t}", "정본 문서 화면 — 등급 배지·작성/검토 모델 (변경 후)")
            + '<header class="app">문서 · 정본</header>' + cols + '</body></html>')


# ------------------------------------------------------------------ 캡처·manifest
VIEWS = {"1440": (1440, 900), "390": (390, 844)}
SCREENS = [
    ("before", "doc-tier-settings", "변경 전 — 러너 모델 우선순위 (현재 DB 값 재구성, 문제 주석)", "before.html"),
    ("after", "doc-tier-settings", "변경 후 — 코드 검수 3줄 · 문서 작성·검토 등급 블록 · 교차 회사 배지 · 저장 전 미리보기", "after.html"),
    ("docs_after", "canonical-docs-tier", "변경 후 — 정본 문서 목록·상세의 등급 배지와 작성/검토 모델", "canonical_docs_after.html"),
]


def sha(p: pathlib.Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def main() -> None:
    (ROOT / "evidence").mkdir(exist_ok=True)
    pages = {"before.html": build_before(), "after.html": build_after(), "canonical_docs_after.html": build_docs_after()}
    for name, text in pages.items():
        (ROOT / name).write_text(text, encoding="utf-8")
    now = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat()
    assets, overflow = [], {}
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        for key, screen_id, _title, fname in SCREENS:
            for vp, (w, h) in VIEWS.items():
                ctx = browser.new_context(viewport={"width": w, "height": h}, device_scale_factor=1)
                page = ctx.new_page()
                page.goto((ROOT / fname).as_uri())
                page.wait_for_timeout(200)
                sw = page.evaluate("document.documentElement.scrollWidth")
                overflow[f"{key}_{vp}"] = sw - w
                out = ROOT / "evidence" / f"{key}_{w}x{h}.png"
                page.screenshot(path=str(out), full_page=True)
                ctx.close()
                assets.append({
                    "asset_id": f"mockup-{key}-{w}x{h}", "role": "primary", "uri": f"internal://{REL}/evidence/{out.name}",
                    "sha256": sha(out), "byte_size": out.stat().st_size, "mime": "image/png", "screen_id": screen_id,
                    "phase": "mockup" if key != "before" else "before", "viewport": f"{w}x{h}",
                    "fixture_id": {"before": "db-2026-10-28", "after": "proposed-defaults", "docs_after": "sample-docs"}[key],
                    "state": "default", "captured_at": now, "capture_source": "static_html_render", "redaction_status": "not_required"})
        browser.close()
    for name in pages:
        p = ROOT / name
        assets.append({"asset_id": f"html-{p.stem}", "role": "child", "uri": f"internal://{REL}/{name}",
                       "sha256": sha(p), "byte_size": p.stat().st_size, "mime": "text/html"})
    manifest = {
        "screens": [
            {"screen_id": "doc-tier-settings", "title": "설정 > 모델 설정 > 러너 모델 — 코드 검수 3줄 · 문서 작성·검토 등급 · 교차 회사 확인",
             "route": "/settings", "requirement_ids": ["FR-15", "FR-16", "FR-17", "FR-18", "FR-19", "FR-21"],
             "states_not_applicable": {
                 "empty": "러너 모델 설정은 항상 기본 행이 있어 빈 상태가 없다",
                 "loading": "현행 화면의 「로딩 중...」 문구를 그대로 쓰며 이번 변경 범위가 아니다",
                 "permission": "설정 화면은 기존 로그인·권한 처리를 따른다",
                 "session-expired": "로그인 만료는 공통 로그인 화면으로 이동한다"}},
            {"screen_id": "canonical-docs-tier", "title": "문서 > 정본 — 목록·상세의 등급 배지와 작성/검토 모델·결과",
             "route": "/docs", "requirement_ids": ["FR-7", "FR-20", "FR-21"],
             "states_not_applicable": {
                 "loading": "현행 CanonicalListBody 의 로딩 상태를 그대로 쓴다",
                 "empty": "현행 「승인된 정본 없음」 문구를 그대로 쓴다",
                 "permission": "기존 문서 화면의 권한 오류 상자를 따른다",
                 "session-expired": "기존 「로그인 복구」 링크를 따른다"}}],
        "assets": assets,
        "evidence": [{
            "evidence_id": "render-doc-tier-cross-review", "kind": "browser_capture", "screen_id": "doc-tier-settings",
            "route": "/settings", "success": all(v <= 0 for v in overflow.values()), "login_used": False,
            "detail": ("서버 Playwright chromium 으로 정적 HTML 시안 3장(변경 전·변경 후·정본 화면)을 데스크톱 1440x900·모바일 390x844 로 캡처(총 6장). "
                       "운영 /settings 는 로그인이 필요해 변경 전은 settings/page.tsx(origin/main e4e4e970)의 RunnerModelConfig 구조와 DB 값(runner_model_config, 2026-10-28)으로 재구성. "
                       f"가로 넘침(scrollWidth-viewport): {json.dumps(overflow)}. "
                       "검토 모델 순서와 문서 예시는 제안·가상 값이며 확정이 아니다."),
            "recorded_at": now}],
        "source_sha": SOURCE_SHA}
    (ROOT / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print("overflow", overflow)


if __name__ == "__main__":
    main()
