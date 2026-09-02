"""Browser-readable LLM market report backed by the OpenRouter catalog.

The admin report uses public catalog data at request time so it does not drift
when model providers add or retire models between releases.
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import time
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Query
from fastapi.responses import HTMLResponse, JSONResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/llm-models", tags=["llm-models"])

KST = ZoneInfo("Asia/Seoul")
OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
OPENROUTER_RANKINGS_URL = "https://openrouter.ai/rankings"
CACHE_TTL_SECONDS = 60 * 60 * 12
STATIC_REPORT_PATH = Path(__file__).resolve().parents[1] / "static" / "reports" / "llm-models-current.html"

_CACHE: dict[str, Any] = {"loaded_at": 0.0, "models": [], "error": None}

PINNED_SOURCES = [
    ("OpenAI Models", "2026-09-02", "https://developers.openai.com/api/docs/models", "GPT-5.6 Sol/Terra/Luna model IDs, 1.05M context, 128K max output, tool support."),
    ("OpenAI Pricing", "2026-09-02", "https://developers.openai.com/api/docs/pricing", "GPT-5.6 text, image, realtime, Sora 2 video, Daybreak pricing."),
    ("Anthropic Pricing", "2026-09-02", "https://platform.claude.com/docs/en/about-claude/pricing", "Claude Fable 5.1, Mythos 5.1, Opus 5, Sonnet 5, Haiku 4.5 pricing."),
    ("OpenRouter Models API", "live", OPENROUTER_MODELS_URL, "Public catalog used for provider/model list, pricing, context and capability fields."),
    ("OpenRouter Rankings", "2026-09-01 usage bucket", OPENROUTER_RANKINGS_URL, "Real-world token-volume leaderboard; adoption signal, not quality score."),
]

OPENAI_OFFICIAL = [
    ("OpenAI", "gpt-5.6-sol", "최고 추론/코딩", "1.05M", "128K", "$4.00", "$20.00", "text, image input, tools, web/file search, computer use", "OpenAI Docs, 2026-09-02"),
    ("OpenAI", "gpt-5.6-terra", "성능/비용 균형", "1.05M", "128K", "$2.00", "$12.00", "text, image input, tools, web/file search, computer use", "OpenAI Docs, 2026-09-02"),
    ("OpenAI", "gpt-5.6-luna", "대량 저비용", "1.05M", "128K", "$0.20", "$1.20", "text, image input, tools, web/file search, computer use", "OpenAI Docs, 2026-09-02"),
    ("OpenAI", "gpt-image-2", "이미지 생성/편집", "-", "-", "$8 image / $5 text", "$30 image", "image generation", "OpenAI Pricing, 2026-09-02"),
    ("OpenAI", "sora-2 / sora-2-pro", "동영상 생성", "-", "720p-1080p", "-", "$0.10-$0.70/sec", "video generation", "OpenAI Pricing, 2026-09-02"),
]

CLAUDE_OFFICIAL = [
    ("Anthropic", "Claude Fable 5.1", "1M", "$10.00", "$50.00", "agentic coding / long-running workflows", "Anthropic Pricing, 2026-09-02"),
    ("Anthropic", "Claude Mythos 5.1", "1M", "$10.00", "$50.00", "limited availability / high reasoning", "Anthropic Pricing, 2026-09-02"),
    ("Anthropic", "Claude Opus 5", "1M", "$5.00", "$25.00", "highest Claude general intelligence", "Anthropic Pricing, 2026-09-02"),
    ("Anthropic", "Claude Sonnet 5", "1M", "$2.00", "$10.00", "balanced production default", "Anthropic Pricing, 2026-09-02"),
    ("Anthropic", "Claude Haiku 4.5", "200K+", "$1.00", "$5.00", "low-latency and cost-sensitive", "Anthropic Pricing, 2026-09-02"),
]

RANKING_ROWS = [
    ("1", "DeepSeek V4 Flash 0731", "DeepSeek", "12.1T", "+4%"),
    ("2", "GLM 5.3 Flash", "Z.ai", "10T", "new"),
    ("3", "GPT-5.6 Luna", "OpenAI", "9.52T", "+129%"),
    ("4", "MiMo-V2.5", "Xiaomi", "7.2T", "+27%"),
    ("5", "Hy3", "Tencent", "5.89T", "+18%"),
    ("6", "Hy4 preview", "Tencent", "5.72T", "new"),
    ("7", "DeepSeek V4 Flash 0423", "DeepSeek", "5.16T", "+7%"),
    ("8", "Nemotron 3 Ultra (free)", "NVIDIA", "4.63T", "+14%"),
    ("9", "Ox Alpha", "Stealth", "4T", "+83%"),
    ("10", "MiniMax M3 (free)", "MiniMax", "3.77T", ">999%"),
]


def _money_per_million(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value)) * Decimal(1000000)
    except (InvalidOperation, TypeError, ValueError):
        return None


def _money_label(value: Any) -> str:
    amount = _money_per_million(value)
    if amount is None:
        return "미공개"
    if amount == 0:
        return "$0"
    return f"${amount.normalize():f}"


def _provider(model_id: str) -> str:
    return model_id.split("/", 1)[0] if "/" in model_id else "unknown"


def _bench(model: dict[str, Any], key: str) -> float:
    return float(((model.get("benchmarks") or {}).get("artificial_analysis") or {}).get(key) or 0)


def _capability_score(model: dict[str, Any]) -> int:
    architecture = model.get("architecture") or {}
    params = set(model.get("supported_parameters") or [])
    inputs = set(architecture.get("input_modalities") or [])
    outputs = set(architecture.get("output_modalities") or [])
    score = 0
    score += 3 if "tools" in params else 0
    score += 2 if "structured_outputs" in params or "response_format" in params else 0
    score += 2 if "reasoning" in params or model.get("reasoning") else 0
    score += 2 if "image" in inputs else 0
    score += 2 if "file" in inputs else 0
    score += 2 if "audio" in inputs or "video" in inputs else 0
    score += 2 if "image" in outputs or "audio" in outputs or "video" in outputs else 0
    score += 1 if (model.get("top_provider") or {}).get("max_completion_tokens") else 0
    return score


def _availability(model: dict[str, Any]) -> str:
    pricing = model.get("pricing") or {}
    prompt = _money_per_million(pricing.get("prompt"))
    completion = _money_per_million(pricing.get("completion"))
    if prompt == 0 and completion == 0:
        return "무료 API"
    if ":free" in str(model.get("id", "")):
        return "무료 API"
    return "유료 API"


def _safe_text(value: Any) -> str:
    return html.escape(str(value if value is not None else ""))


def _fetch_openrouter_models_sync() -> list[dict[str, Any]]:
    req = urllib.request.Request(OPENROUTER_MODELS_URL, headers={"User-Agent": "AADS-LLM-Report/2026.09"})
    with urllib.request.urlopen(req, timeout=20) as response:
        payload = json.loads(response.read().decode("utf-8"))
    models = payload.get("data") or []
    if not isinstance(models, list):
        raise TypeError("OpenRouter response data is not a list")
    return models


async def _load_models(*, refresh: bool = False) -> tuple[list[dict[str, Any]], str | None, float]:
    now = time.time()
    if not refresh and _CACHE["models"] and now - float(_CACHE["loaded_at"] or 0) < CACHE_TTL_SECONDS:
        return list(_CACHE["models"]), _CACHE.get("error"), float(_CACHE["loaded_at"] or 0)
    try:
        models = await asyncio.to_thread(_fetch_openrouter_models_sync)
        _CACHE.update({"models": models, "loaded_at": now, "error": None})
        return models, None, now
    except Exception as exc:
        logger.warning("llm_report_openrouter_fetch_failed", exc_info=exc)
        _CACHE["error"] = str(exc)
        return list(_CACHE["models"]), str(exc), float(_CACHE["loaded_at"] or 0)


def _summarize(models: list[dict[str, Any]]) -> dict[str, Any]:
    providers = Counter(_provider(str(m.get("id", ""))) for m in models)
    free_models = [m for m in models if _availability(m).startswith("무료")]
    priced = [m for m in models if _money_per_million((m.get("pricing") or {}).get("completion")) is not None]
    cheapest_paid = sorted(
        [m for m in priced if not _availability(m).startswith("무료")],
        key=lambda m: _money_per_million((m.get("pricing") or {}).get("completion")) or Decimal(999999),
    )[:20]
    performance = sorted(
        models,
        key=lambda m: (_bench(m, "intelligence_index"), _bench(m, "coding_index"), _bench(m, "agentic_index"), int(m.get("created") or 0)),
        reverse=True,
    )[:40]
    coding = sorted(
        models,
        key=lambda m: (_bench(m, "coding_index"), _capability_score(m), int(m.get("created") or 0)),
        reverse=True,
    )[:40]
    capability = sorted(
        models,
        key=lambda m: (_capability_score(m), int(m.get("context_length") or 0), int(m.get("created") or 0)),
        reverse=True,
    )[:40]
    by_provider: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for model in models:
        by_provider[_provider(str(model.get("id", "")))].append(model)
    provider_rows = []
    for provider, rows in providers.most_common():
        latest = sorted(by_provider[provider], key=lambda m: int(m.get("created") or 0), reverse=True)[:3]
        provider_rows.append(
            {
                "provider": provider,
                "count": rows,
                "free": sum(1 for m in by_provider[provider] if _availability(m).startswith("무료")),
                "latest": ", ".join(str(m.get("name") or m.get("id")) for m in latest),
            }
        )
    return {
        "total": len(models),
        "providers": len(providers),
        "free_count": len(free_models),
        "paid_count": len(models) - len(free_models),
        "provider_rows": provider_rows,
        "free_models": free_models[:80],
        "performance": performance,
        "coding": coding,
        "capability": capability,
        "cheapest_paid": cheapest_paid,
    }


def _model_row(model: dict[str, Any], rank: int | None = None) -> str:
    pricing = model.get("pricing") or {}
    architecture = model.get("architecture") or {}
    created = ""
    if model.get("created"):
        try:
            created = datetime.fromtimestamp(int(model["created"]), tz=KST).strftime("%Y-%m-%d")
        except (OSError, OverflowError, ValueError):
            created = ""
    params = set(model.get("supported_parameters") or [])
    caps = []
    caps.append("tools") if "tools" in params else None
    caps.append("JSON") if "structured_outputs" in params or "response_format" in params else None
    caps.append("reasoning") if model.get("reasoning") or "reasoning" in params else None
    caps.append("vision") if "image" in set(architecture.get("input_modalities") or []) else None
    caps.append("file") if "file" in set(architecture.get("input_modalities") or []) else None
    caps.append("multimodal-out") if set(architecture.get("output_modalities") or []) - {"text"} else None
    caps = caps or [architecture.get("modality") or "-"]
    badge = "free" if _availability(model).startswith("무료") else "paid"
    rank_cell = f"<td>{rank}</td>" if rank is not None else ""
    return (
        "<tr>"
        f"{rank_cell}<td><b>{_safe_text(model.get('name') or model.get('id'))}</b><br><span>{_safe_text(model.get('id'))}</span></td>"
        f"<td>{_safe_text(_provider(str(model.get('id', ''))))}</td><td>{_safe_text(created)}</td>"
        f"<td>{int(model.get('context_length') or 0):,}</td><td>{_safe_text(_money_label(pricing.get('prompt')))}</td>"
        f"<td>{_safe_text(_money_label(pricing.get('completion')))}</td><td><em class='{badge}'>{_safe_text(_availability(model))}</em></td>"
        f"<td>{_bench(model, 'intelligence_index'):.1f}</td><td>{_bench(model, 'coding_index'):.1f}</td>"
        f"<td>{_bench(model, 'agentic_index'):.1f}</td><td>{_capability_score(model)}</td><td>{_safe_text(', '.join(caps))}</td>"
        "</tr>"
    )


def _tuple_rows(rows: list[tuple[Any, ...]]) -> str:
    return "".join("<tr>" + "".join(f"<td>{_safe_text(cell)}</td>" for cell in row) + "</tr>" for row in rows)


def _render_report(models: list[dict[str, Any]], *, error: str | None, loaded_at: float) -> str:
    summary = _summarize(models)
    now_kst = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S KST")
    loaded_label = datetime.fromtimestamp(loaded_at, tz=KST).strftime("%Y-%m-%d %H:%M:%S KST") if loaded_at else "미로드"
    provider_rows = "".join(
        f"<tr><td><b>{_safe_text(row['provider'])}</b></td><td>{int(row['count']):,}</td><td>{int(row['free']):,}</td><td>{_safe_text(row['latest'])}</td></tr>"
        for row in summary["provider_rows"]
    )
    source_rows = _tuple_rows(PINNED_SOURCES)
    all_rows = "".join(
        _model_row(m, i + 1)
        for i, m in enumerate(sorted(models, key=lambda item: (_provider(str(item.get("id", ""))), str(item.get("name") or ""))))
    )
    return f"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>AADS 최신 LLM 전수 분석 보고서</title>
<style>
:root{{color-scheme:dark;--bg:#0e1116;--panel:#171c24;--line:#2a3342;--text:#e5edf7;--muted:#91a0b4;--brand:#7dd3fc;--accent:#fbbf24;--ok:#34d399}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--text);font-family:Arial,"Noto Sans KR",sans-serif;line-height:1.48}}
a{{color:var(--brand)}}header{{position:sticky;top:0;z-index:3;background:rgba(14,17,22,.95);border-bottom:1px solid var(--line);padding:18px 22px}}
main{{padding:20px;max-width:1500px;margin:0 auto}}h1{{font-size:26px;margin:0 0 6px}}h2{{font-size:18px;margin:34px 0 10px;color:var(--brand)}}h3{{font-size:15px;margin:20px 0 8px;color:var(--accent)}}
.meta{{color:var(--muted);font-size:13px}}.warn{{border-left:4px solid var(--accent);background:#2a210d;padding:10px 12px;border-radius:6px;margin:14px 0;color:#ffe8b3}}
.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px;margin:16px 0 20px}}.card{{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:14px}}
.num{{font-size:26px;font-weight:700;color:var(--brand)}}.label{{font-size:12px;color:var(--muted);margin-top:3px}}
.table-wrap{{overflow:auto;border:1px solid var(--line);border-radius:8px;background:#111722}}table{{width:100%;border-collapse:collapse;font-size:12px;min-width:980px}}
th{{position:sticky;top:70px;background:#1d2633;color:#b8e7ff;text-align:left;padding:9px;border-bottom:1px solid var(--line);white-space:nowrap}}
td{{padding:8px 9px;border-bottom:1px solid #202938;vertical-align:top}}td span{{color:var(--muted);font-size:11px}}tr:hover td{{background:#16202d}}
.free{{color:var(--ok);font-style:normal;font-weight:700}}.paid{{color:var(--accent);font-style:normal;font-weight:700}}
.nav{{display:flex;gap:8px;flex-wrap:wrap;margin-top:10px}}.nav a{{padding:6px 9px;border:1px solid var(--line);border-radius:6px;text-decoration:none;background:#131a23;font-size:12px}}
.grid2{{display:grid;grid-template-columns:1fr 1fr;gap:14px}}@media(max-width:900px){{.grid2{{grid-template-columns:1fr}}th{{top:92px}}}}
</style></head><body>
<header><h1>AADS 최신 LLM 회사·모델 전수 분석 보고서</h1>
<div class="meta">보고 기준: 2026-09-02 KST · 생성: {now_kst} · OpenRouter catalog loaded: {loaded_label}</div>
<nav class="nav"><a href="#summary">요약</a><a href="#official">공식 확인</a><a href="#ranking">실사용 TOP 10</a><a href="#performance">성능순</a><a href="#coding">코딩순</a><a href="#capability">기능순</a><a href="#free">무료</a><a href="#cheap">저가 유료</a><a href="#providers">회사별</a><a href="#all">전체</a><a href="/admin/model-routing">어드민 모델 라우팅</a></nav></header>
<main>
<section id="summary"><div class="cards">
<div class="card"><div class="num">{summary['total']:,}</div><div class="label">OpenRouter catalog 모델 [API]</div></div>
<div class="card"><div class="num">{summary['providers']:,}</div><div class="label">제공사/조직 [API]</div></div>
<div class="card"><div class="num">{summary['free_count']:,}</div><div class="label">무료 API 모델 [API]</div></div>
<div class="card"><div class="num">{summary['paid_count']:,}</div><div class="label">유료 API 모델 [API]</div></div>
<div class="card"><div class="num">1.05M</div><div class="label">GPT-5.6 / 최신 장문 컨텍스트 [공식]</div></div>
<div class="card"><div class="num">$0</div><div class="label">무료 모델 최저 가격 [API]</div></div></div>
<div class="warn">판정: 전 세계 모든 비공개/지역 한정 모델까지 무누락 검증은 불가능합니다. 본 보고서는 AADS가 즉시 라우팅 후보로 삼을 수 있는 OpenRouter 공개 catalog와 OpenAI/Anthropic 공식 문서 확인 모델을 기준으로 전수 정렬합니다.</div>
{"<div class='warn'>OpenRouter API 오류: " + _safe_text(error) + " · 캐시 데이터로 표시 중입니다.</div>" if error else ""}</section>
<section id="official"><h2>공식 확인 핵심 모델</h2><h3>OpenAI</h3>
<div class="table-wrap"><table><thead><tr><th>회사</th><th>모델</th><th>역할</th><th>컨텍스트</th><th>최대 출력</th><th>입력/1M</th><th>출력/1M</th><th>기능</th><th>출처</th></tr></thead><tbody>{_tuple_rows(OPENAI_OFFICIAL)}</tbody></table></div>
<h3>Anthropic Claude</h3><div class="table-wrap"><table><thead><tr><th>회사</th><th>모델</th><th>컨텍스트</th><th>입력/1M</th><th>출력/1M</th><th>용도</th><th>출처</th></tr></thead><tbody>{_tuple_rows(CLAUDE_OFFICIAL)}</tbody></table></div></section>
<section id="ranking"><h2>OpenRouter 실사용 TOP 10</h2><div class="warn">OpenRouter 랭킹은 2026-09-01 사용 버킷 기준 토큰 처리량입니다. 품질 점수가 아니라 채택량 지표입니다.</div><div class="table-wrap"><table><thead><tr><th>순위</th><th>모델</th><th>회사</th><th>처리 토큰</th><th>증감</th></tr></thead><tbody>{_tuple_rows(RANKING_ROWS)}</tbody></table></div></section>
<section id="performance"><h2>성능순 TOP 40</h2><div class="table-wrap"><table><thead><tr><th>#</th><th>모델</th><th>회사</th><th>출시/등록</th><th>컨텍스트</th><th>입력/1M</th><th>출력/1M</th><th>유/무료</th><th>지능</th><th>코딩</th><th>에이전트</th><th>기능점수</th><th>기능</th></tr></thead><tbody>{"".join(_model_row(m, i + 1) for i, m in enumerate(summary['performance']))}</tbody></table></div></section>
<section id="coding"><h2>코딩능력순 TOP 40</h2><div class="table-wrap"><table><thead><tr><th>#</th><th>모델</th><th>회사</th><th>출시/등록</th><th>컨텍스트</th><th>입력/1M</th><th>출력/1M</th><th>유/무료</th><th>지능</th><th>코딩</th><th>에이전트</th><th>기능점수</th><th>기능</th></tr></thead><tbody>{"".join(_model_row(m, i + 1) for i, m in enumerate(summary['coding']))}</tbody></table></div></section>
<section id="capability"><h2>기능순 TOP 40</h2><div class="table-wrap"><table><thead><tr><th>#</th><th>모델</th><th>회사</th><th>출시/등록</th><th>컨텍스트</th><th>입력/1M</th><th>출력/1M</th><th>유/무료</th><th>지능</th><th>코딩</th><th>에이전트</th><th>기능점수</th><th>기능</th></tr></thead><tbody>{"".join(_model_row(m, i + 1) for i, m in enumerate(summary['capability']))}</tbody></table></div></section>
<section class="grid2"><div id="free"><h2>무료 API 모델</h2><div class="table-wrap"><table><thead><tr><th>#</th><th>모델</th><th>회사</th><th>출시/등록</th><th>컨텍스트</th><th>입력/1M</th><th>출력/1M</th><th>유/무료</th><th>지능</th><th>코딩</th><th>에이전트</th><th>기능점수</th><th>기능</th></tr></thead><tbody>{"".join(_model_row(m, i + 1) for i, m in enumerate(summary['free_models']))}</tbody></table></div></div>
<div id="cheap"><h2>저가 유료 모델 TOP 20</h2><div class="table-wrap"><table><thead><tr><th>#</th><th>모델</th><th>회사</th><th>출시/등록</th><th>컨텍스트</th><th>입력/1M</th><th>출력/1M</th><th>유/무료</th><th>지능</th><th>코딩</th><th>에이전트</th><th>기능점수</th><th>기능</th></tr></thead><tbody>{"".join(_model_row(m, i + 1) for i, m in enumerate(summary['cheapest_paid']))}</tbody></table></div></div></section>
<section id="providers"><h2>회사별 모델 보유 현황</h2><div class="table-wrap"><table><thead><tr><th>회사/제공사</th><th>모델 수</th><th>무료 수</th><th>최근 모델 예시</th></tr></thead><tbody>{provider_rows}</tbody></table></div></section>
<section id="all"><h2>전체 모델 목록 ({summary['total']:,}개)</h2><div class="table-wrap"><table><thead><tr><th>#</th><th>모델</th><th>회사</th><th>출시/등록</th><th>컨텍스트</th><th>입력/1M</th><th>출력/1M</th><th>유/무료</th><th>지능</th><th>코딩</th><th>에이전트</th><th>기능점수</th><th>기능</th></tr></thead><tbody>{all_rows}</tbody></table></div></section>
<section id="sources"><h2>출처 및 검증 기준</h2><div class="table-wrap"><table><thead><tr><th>출처</th><th>확인일</th><th>URL</th><th>용도</th></tr></thead><tbody>{source_rows}</tbody></table></div><div class="warn">월 자동 최신화: AADS APScheduler monthly_llm_model_sync가 매월 1일 00:05 KST에 모델 레지스트리를 동기화합니다. 이 보고서 URL은 열릴 때 OpenRouter catalog를 12시간 캐시로 갱신합니다.</div></section>
</main></body></html>"""


async def refresh_static_report(*, refresh: bool = True) -> dict[str, Any]:
    """Regenerate the browser-openable static HTML report."""
    models, error, loaded_at = await _load_models(refresh=refresh)
    html_content = _render_report(models, error=error, loaded_at=loaded_at)
    STATIC_REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATIC_REPORT_PATH.write_text(html_content, encoding="utf-8")
    summary = _summarize(models) if models else {}
    return {
        "path": str(STATIC_REPORT_PATH),
        "bytes": STATIC_REPORT_PATH.stat().st_size,
        "models": summary.get("total", 0),
        "providers": summary.get("providers", 0),
        "free": summary.get("free_count", 0),
        "paid": summary.get("paid_count", 0),
        "error": error,
    }


@router.get("/report", response_class=HTMLResponse, include_in_schema=False)
async def get_llm_models_report(refresh: bool = Query(False, description="Force OpenRouter catalog refresh")):
    """Return the browser-readable LLM market report."""
    models, error, loaded_at = await _load_models(refresh=refresh)
    return HTMLResponse(content=_render_report(models, error=error, loaded_at=loaded_at))


@router.get("/report.json", include_in_schema=False)
async def get_llm_models_report_json(refresh: bool = Query(False)):
    """Return the same report data as JSON for automation and smoke tests."""
    models, error, loaded_at = await _load_models(refresh=refresh)
    return JSONResponse(
        {
            "ok": bool(models),
            "loaded_at_kst": datetime.fromtimestamp(loaded_at, tz=KST).strftime("%Y-%m-%d %H:%M:%S KST") if loaded_at else None,
            "error": error,
            "summary": _summarize(models) if models else {},
            "sources": [
                {"name": name, "checked": checked, "url": url, "summary": summary}
                for name, checked, url, summary in PINNED_SOURCES
            ],
        }
    )
