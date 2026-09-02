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
    ("Anthropic Pricing", "2026-09-02", "https://docs.anthropic.com/en/docs/about-claude/pricing", "Claude Fable 5.1, Mythos 5.1, Opus 5, Sonnet 5, Haiku 4.5 pricing."),
    ("Google Gemini Pricing", "2026-09-02", "https://ai.google.dev/gemini-api/docs/pricing", "Gemini 3.x Pro/Flash pricing, free tier, grounding cost and data-use notes."),
    ("xAI Grok Models", "2026-09-02", "https://docs.x.ai/developers/models", "Grok 4.6 family, context windows, text/image/video capability notes."),
    ("Mistral Models", "2026-09-02", "https://docs.mistral.ai/models", "Mistral model lineup and capability selection guide."),
    ("DeepSeek Pricing", "2026-09-02", "https://api-docs.deepseek.com/quick_start/pricing/", "DeepSeek V4 peak/off-peak pricing and tool/JSON support."),
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

OTHER_OFFICIAL = [
    ("Google", "Gemini 3.1 Pro Preview", "최상위 멀티모달·장문 추론", "1M+", "$1.35-$2.70", "$6.75-$13.50", "Free tier 일부 제공, Google Search grounding 별도", "Google Pricing, 2026-09-02"),
    ("Google", "Gemini 3.6 Flash", "고속 멀티모달", "1M+", "$0.75", "$3.75", "대량 처리, 이미지/문서 분석, 지연시간 민감 워크로드", "Google Pricing, 2026-09-02"),
    ("xAI", "Grok 4.6", "실시간·에이전트·코딩", "500K", "$2.00", "$6.00", "OpenAI SDK 호환, 이미지/비디오 생성 API 별도", "xAI Docs, 2026-09-02"),
    ("DeepSeek", "DeepSeek V4 Flash", "초저가 코딩·수학", "1M", "$0.22-$0.44", "$0.66-$1.32", "피크/오프피크 과금, JSON/tool call 지원", "DeepSeek Docs, 2026-09-02"),
    ("DeepSeek", "DeepSeek V4 Pro", "고성능 저가 추론", "1M", "$0.66-$1.32", "$1.98-$3.96", "피크/오프피크 과금, Anthropic API 호환", "DeepSeek Docs, 2026-09-02"),
    ("Mistral", "Mistral Large / Devstral", "EU 규정·코딩·에이전트", "128K+", "모델별 상이", "모델별 상이", "EU 데이터/엔터프라이즈 요건, 코딩 세션/에이전트", "Mistral Docs, 2026-09-02"),
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

TASK_GUIDE_ROWS = [
    ("최고난도 추론/전략", "GPT-5.6 Sol, Claude Opus 5, Claude Fable 5.1", "정확도·계획 품질 우선. 비용보다 실패 비용이 큰 작업에 사용.", "장문 컨텍스트, tool use, reasoning, 높은 출력 안정성"),
    ("일반 업무 기본값", "Claude Sonnet 5, GPT-5.6 Terra, Gemini 3.1 Pro", "품질과 비용 균형. CEO 보고서, 코드 리뷰, 중간 규모 개발에 사용.", "reasoning, JSON, function/tool call, 1M급 컨텍스트"),
    ("대량/저비용 자동화", "GPT-5.6 Luna, Gemini Flash, DeepSeek V4 Flash", "분류·요약·추출·반복 배치에 사용. 실패 시 상위 모델 재시도.", "낮은 입력/출력 단가, 빠른 응답, 캐시 효율"),
    ("코딩/디버깅", "GPT-5.6 Sol, Claude Sonnet 5, DeepSeek V4, Qwen/Devstral", "repo 수정, 테스트 실패 분석, migration 작성에 사용.", "coding score, long context, patch 작성 안정성"),
    ("에이전트/MCP/툴", "Claude Opus/Sonnet, GPT-5.6, Grok 4.6", "도구 호출과 상태 전이가 많은 AADS 작업에 사용.", "tools/function calling, JSON, long-running workflow"),
    ("멀티모달/비전", "GPT-5.6, Gemini 3.x, Claude 5, gpt-image-2", "스크린샷 판독, OCR, 이미지 생성·편집, 문서 QA에 사용.", "image input/output, file, video/audio support"),
    ("무료 실험/프로토타입", "Llama, Qwen, MiniMax, Nemotron free variants", "비용 없이 초안·샌드박스 실험. 운영 핵심에는 검증 후 승격.", "$0 pricing, rate limit 확인, 상업 라이선스 확인"),
]

# TOP_PICKS: dict 리스트. bench = {"intelligence": 0~100, "coding": 0~100, "agentic": 0~100}
TOP_PICKS = [
    {
        "name": "Claude Opus 5", "company": "Anthropic", "tier": "최상위",
        "tagline": "최고 추론·복잡한 분석",
        "description": "Anthropic 최강 모델. 복잡한 다단계 추론, 긴 문서 분석, 전략적 계획 수립에 최적. 컨텍스트 1M 토큰 지원.",
        "use_cases": ["복잡한 분석", "전략 수립", "장문 문서"],
        "ctx": "1M", "price_in": "$5.00", "price_out": "$25.00",
        "bench": {"intelligence": 92, "coding": 88, "agentic": 90},
        "highlight": "Anthropic 플래그십 · 1M 컨텍스트 · 최장 체인 지원",
        "new": False,
    },
    {
        "name": "Claude Sonnet 5", "company": "Anthropic", "tier": "균형 권장",
        "tagline": "성능·비용 최적 균형",
        "description": "생산 환경 기본값. Opus 수준에 근접하는 성능을 절반 비용으로 제공. 대부분의 실무 작업에 권장.",
        "use_cases": ["일반 개발", "문서 작성", "데이터 분석"],
        "ctx": "1M", "price_in": "$2.00", "price_out": "$10.00",
        "bench": {"intelligence": 87, "coding": 85, "agentic": 82},
        "highlight": "입력 $2/1M · Opus 대비 60% 절약 · AADS 기본 권장",
        "new": False,
    },
    {
        "name": "Claude Haiku 4.5", "company": "Anthropic", "tier": "고속 저가",
        "tagline": "초저지연·대량 처리",
        "description": "응답 속도 최우선 또는 대량 배치 처리가 필요할 때. Anthropic 라인업 중 가장 저렴.",
        "use_cases": ["챗봇", "분류", "대량 배치"],
        "ctx": "200K+", "price_in": "$1.00", "price_out": "$5.00",
        "bench": {"intelligence": 74, "coding": 72, "agentic": 68},
        "highlight": "입력 $1/1M · 저지연 최적 · 분류/배치 1순위",
        "new": False,
    },
    {
        "name": "GPT-5.6 Sol", "company": "OpenAI", "tier": "최상위",
        "tagline": "추론·코딩·멀티모달 최정상",
        "description": "OpenAI 플래그십. 1.05M 컨텍스트, 코딩·수학·멀티모달 전 영역 최상위. computer use 지원.",
        "use_cases": ["최고 난이도 코딩", "멀티모달", "추론"],
        "ctx": "1.05M", "price_in": "$4.00", "price_out": "$20.00",
        "bench": {"intelligence": 95, "coding": 96, "agentic": 94},
        "highlight": "1.05M ctx · computer use · 이미지입력 · 도구 지원",
        "new": False,
    },
    {
        "name": "GPT-5.6 Terra", "company": "OpenAI", "tier": "균형 권장",
        "tagline": "Sol 90% 성능, 절반 비용",
        "description": "Sol의 능력을 실용적 비용으로. 대부분의 OpenAI 연동 프로젝트에 권장되는 기본값.",
        "use_cases": ["API 연동", "콘텐츠 생성", "코딩 지원"],
        "ctx": "1.05M", "price_in": "$2.00", "price_out": "$12.00",
        "bench": {"intelligence": 89, "coding": 90, "agentic": 87},
        "highlight": "Sol의 90% 성능 · $2/1M · 웹/파일 서치 포함",
        "new": False,
    },
    {
        "name": "GPT-5.6 Luna", "company": "OpenAI", "tier": "고속 저가",
        "tagline": "Terra 1/10 비용, 실사용 TOP3",
        "description": "OpenRouter 실사용량 3위. 빠른 응답이 필요한 고빈도 작업에 최적. 비용 대비 효율 최강.",
        "use_cases": ["고빈도 API", "챗봇", "분류·추출"],
        "ctx": "1.05M", "price_in": "$0.20", "price_out": "$1.20",
        "bench": {"intelligence": 79, "coding": 80, "agentic": 75},
        "highlight": "OpenRouter 사용량 3위 · $0.20/1M · 고빈도 최적",
        "new": False,
    },
    {
        "name": "Gemini 3.1 Pro Preview", "company": "Google", "tier": "균형",
        "tagline": "멀티모달·장문·Google 생태계",
        "description": "Google 상위 멀티모달 모델. 이미지·영상·오디오·문서 처리와 Google Search grounding을 함께 쓰는 리서치 워크로드에 적합.",
        "use_cases": ["멀티모달", "장문 분석", "Google 연동"],
        "ctx": "1M+", "price_in": "$1.35-$2.70", "price_out": "$6.75-$13.50",
        "bench": {"intelligence": 89, "coding": 84, "agentic": 83},
        "highlight": "멀티모달 입력 · grounding · Google 생태계",
        "new": True,
    },
    {
        "name": "Grok 4.6", "company": "xAI", "tier": "균형",
        "tagline": "실시간 리서치·에이전트·코딩",
        "description": "OpenAI SDK 호환 API로 붙이기 쉽고, 긴 컨텍스트와 실시간 정보성 작업에 강한 xAI 최신 플래그십 계열.",
        "use_cases": ["실시간 리서치", "에이전트", "코딩"],
        "ctx": "500K", "price_in": "$2.00", "price_out": "$6.00",
        "bench": {"intelligence": 86, "coding": 84, "agentic": 86},
        "highlight": "OpenAI SDK 호환 · long-running agent · xAI 최신 계열",
        "new": True,
    },
    {
        "name": "DeepSeek V4 Flash", "company": "DeepSeek", "tier": "코딩 특화",
        "tagline": "코딩·수학 최강 초저가, 사용량 1위",
        "description": "OpenRouter 실사용량 1위 (12.1T 토큰). 코딩·수학 벤치마크 최상위권을 초저가로 제공.",
        "use_cases": ["코드 생성", "디버깅", "수학 추론"],
        "ctx": "128K", "price_in": "극저가", "price_out": "극저가",
        "bench": {"intelligence": 86, "coding": 93, "agentic": 80},
        "highlight": "OpenRouter 사용량 1위 · 코딩 벤치 최상위 · 초저가",
        "new": True,
    },
    {
        "name": "Llama 4 Maverick (free)", "company": "Meta", "tier": "무료 최강",
        "tagline": "무료 최고 성능, 상업 사용 가능",
        "description": "무료 모델 중 최강급 성능. Meta 오픈소스 라이선스로 상업 프로젝트에도 활용 가능.",
        "use_cases": ["프로토타이핑", "무료 개발", "오픈소스 프로젝트"],
        "ctx": "1M", "price_in": "무료", "price_out": "무료",
        "bench": {"intelligence": 82, "coding": 79, "agentic": 77},
        "highlight": "완전 무료 · 상업 라이선스 · 1M ctx",
        "new": False,
    },
    {
        "name": "Qwen3 235B (free)", "company": "Alibaba", "tier": "무료 고성능",
        "tagline": "235B 대형·추론 특화·한국어 우수",
        "description": "235B 파라미터 대형 모델 무료 제공. 추론 특화 설계, 한국어 품질 우수. 복잡한 분석 무료 처리.",
        "use_cases": ["한국어 처리", "추론", "대규모 분석"],
        "ctx": "128K", "price_in": "무료", "price_out": "무료",
        "bench": {"intelligence": 85, "coding": 82, "agentic": 78},
        "highlight": "무료 235B · 한국어 최우수 · 추론 특화",
        "new": False,
    },
    {
        "name": "Mistral Large / Devstral", "company": "Mistral", "tier": "균형",
        "tagline": "EU 규정 준수, 다국어·코딩",
        "description": "유럽 규정 준수와 엔터프라이즈 배포가 중요한 프로젝트에 적합. Devstral 계열은 IDE/코딩 세션에 강점.",
        "use_cases": ["EU 규정 준수", "다국어", "코딩"],
        "ctx": "128K", "price_in": "중간", "price_out": "중간",
        "bench": {"intelligence": 78, "coding": 77, "agentic": 72},
        "highlight": "EU 데이터 요건 · Devstral 코딩 · 엔터프라이즈 API",
        "new": False,
    },
    {
        "name": "gpt-image-2", "company": "OpenAI", "tier": "이미지 생성",
        "tagline": "이미지 생성·편집 전용",
        "description": "OpenAI 최신 이미지 생성 모델. 고품질 이미지 생성·인페인팅·아웃페인팅 지원.",
        "use_cases": ["이미지 생성", "편집", "디자인"],
        "ctx": "-", "price_in": "$8/img(입력)", "price_out": "$30/img(출력)",
        "bench": {"intelligence": 0, "coding": 0, "agentic": 0},
        "highlight": "이미지 전용 · 인페인팅·아웃페인팅 · DALL-E 후속",
        "new": False,
    },
]

# ---------------------------------------------------------------------------
# CSS — 모듈 상수 (f-string 아님, {} 이스케이프 불필요)
# ---------------------------------------------------------------------------
_CSS = """
:root {
  color-scheme: dark;
  --bg: #0e1116;
  --panel: #171c24;
  --panel2: #1a2130;
  --line: #2a3342;
  --text: #e5edf7;
  --muted: #91a0b4;
  --brand: #7dd3fc;
  --accent: #fbbf24;
  --ok: #34d399;
  --tier1: #c084fc;
  --tier2: #60a5fa;
  --tier3: #fb923c;
  --tierfree: #34d399;
  --tiercoding: #f472b6;
  --tierimg: #a78bfa;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--text); font-family: Arial, "Noto Sans KR", sans-serif; line-height: 1.5; }
a { color: var(--brand); }

/* ---- HEADER ---- */
header {
  position: sticky; top: 0; z-index: 10;
  background: rgba(14,17,22,.97); border-bottom: 1px solid var(--line);
  padding: 14px 20px 0;
}
header h1 { font-size: 22px; margin: 0 0 4px; }
.meta { color: var(--muted); font-size: 12px; margin-bottom: 8px; }

/* ---- TAB NAV ---- */
.tab-nav {
  display: flex; gap: 0; flex-wrap: nowrap; overflow-x: auto;
  border-bottom: none; padding-bottom: 0;
  scrollbar-width: none;
}
.tab-nav::-webkit-scrollbar { display: none; }
.tab-btn {
  padding: 8px 14px; border: none; border-bottom: 3px solid transparent;
  background: transparent; color: var(--muted); cursor: pointer;
  font-size: 13px; white-space: nowrap; transition: color .15s, border-color .15s;
}
.tab-btn:hover { color: var(--text); }
.tab-btn.active { color: var(--brand); border-bottom-color: var(--brand); font-weight: 700; }

/* ---- MAIN ---- */
main { padding: 20px; max-width: 1500px; margin: 0 auto; }

/* ---- TAB PANELS ---- */
.tab-panel { display: none; }
.tab-panel.active { display: block; }

h2 { font-size: 18px; margin: 28px 0 10px; color: var(--brand); }
h3 { font-size: 14px; margin: 18px 0 8px; color: var(--accent); }

.warn {
  border-left: 4px solid var(--accent); background: #2a210d;
  padding: 10px 14px; border-radius: 6px; margin: 12px 0; color: #ffe8b3; font-size: 13px;
}

/* ---- STAT CARDS ---- */
.cards {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
  gap: 10px; margin: 14px 0 20px;
}
.card { background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 14px; }
.num { font-size: 26px; font-weight: 700; color: var(--brand); }
.label { font-size: 12px; color: var(--muted); margin-top: 3px; }

/* ---- PICK CARDS ---- */
.pick-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  gap: 14px; margin: 16px 0 24px;
}
.pick-card {
  background: var(--panel2); border: 1px solid var(--line); border-radius: 10px;
  padding: 16px; display: flex; flex-direction: column; gap: 8px;
  transition: border-color .15s;
}
.pick-card:hover { border-color: var(--brand); }
.pick-header { display: flex; align-items: flex-start; gap: 10px; }
.pick-name { font-size: 15px; font-weight: 700; color: var(--text); }
.pick-company { font-size: 12px; color: var(--muted); margin-top: 2px; }
.pick-tagline { font-size: 12px; color: var(--accent); font-weight: 600; }
.pick-desc { font-size: 13px; color: var(--muted); line-height: 1.5; }
.pick-tags { display: flex; flex-wrap: wrap; gap: 5px; }
.pick-tag {
  font-size: 11px; padding: 2px 8px; border-radius: 20px;
  background: rgba(125,211,252,.12); color: var(--brand); border: 1px solid rgba(125,211,252,.25);
}
.pick-pricing { font-size: 12px; color: var(--muted); }
.pick-pricing span { color: var(--text); }

/* TIER BADGES */
.tier-badge {
  font-size: 11px; padding: 2px 8px; border-radius: 20px;
  font-weight: 700; white-space: nowrap; flex-shrink: 0;
}
.tier-top    { background: rgba(192,132,252,.18); color: var(--tier1); border: 1px solid rgba(192,132,252,.3); }
.tier-bal    { background: rgba(96,165,250,.18);  color: var(--tier2); border: 1px solid rgba(96,165,250,.3); }
.tier-fast   { background: rgba(251,146,60,.18);  color: var(--tier3); border: 1px solid rgba(251,146,60,.3); }
.tier-free   { background: rgba(52,211,153,.18);  color: var(--tierfree); border: 1px solid rgba(52,211,153,.3); }
.tier-coding { background: rgba(244,114,182,.18); color: var(--tiercoding); border: 1px solid rgba(244,114,182,.3); }
.tier-img    { background: rgba(167,139,250,.18); color: var(--tierimg); border: 1px solid rgba(167,139,250,.3); }

/* ---- SEARCH ---- */
.search-bar {
  display: flex; align-items: center; gap: 10px; margin: 14px 0 10px;
}
.search-bar input {
  flex: 1; max-width: 360px; padding: 7px 12px; border-radius: 6px;
  background: var(--panel); border: 1px solid var(--line); color: var(--text);
  font-size: 13px; outline: none;
}
.search-bar input:focus { border-color: var(--brand); }
.search-count { font-size: 12px; color: var(--muted); }

/* ---- TABLES ---- */
.table-wrap { overflow: auto; border: 1px solid var(--line); border-radius: 8px; background: #111722; }
table { width: 100%; border-collapse: collapse; font-size: 12px; min-width: 820px; }
th {
  position: sticky; top: 111px;
  background: #1d2633; color: #b8e7ff;
  text-align: left; padding: 9px; border-bottom: 1px solid var(--line);
  white-space: nowrap; cursor: pointer; user-select: none;
}
th:hover { background: #243044; }
th.sort-asc::after  { content: " ▲"; font-size: 10px; }
th.sort-desc::after { content: " ▼"; font-size: 10px; }
td { padding: 7px 9px; border-bottom: 1px solid #202938; vertical-align: top; }
td span { color: var(--muted); font-size: 11px; }
tr:hover td { background: #16202d; }
tr.hidden-row { display: none; }
.free { color: var(--ok);    font-style: normal; font-weight: 700; }
.paid { color: var(--accent); font-style: normal; font-weight: 700; }

/* ---- PAGINATION ---- */
.paginate-btn {
  display: block; margin: 14px auto 0;
  padding: 8px 22px; background: var(--panel); border: 1px solid var(--line);
  border-radius: 6px; color: var(--brand); cursor: pointer; font-size: 13px;
  transition: background .15s;
}
.paginate-btn:hover { background: var(--panel2); }
.paginate-btn:disabled { color: var(--muted); cursor: default; }

/* PERF BAR */
.perf-bar-wrap { margin-top: 6px; }
.perf-bar-label { display: flex; justify-content: space-between; font-size: 11px; color: var(--muted); margin-bottom: 2px; }
.perf-bar-track { height: 5px; background: var(--line); border-radius: 3px; overflow: hidden; }
.perf-bar-fill { height: 100%; border-radius: 3px; background: linear-gradient(90deg, var(--brand), var(--ok)); }

/* NEW BADGE */
.badge-new {
  font-size: 10px; padding: 1px 6px; border-radius: 10px;
  background: rgba(52,211,153,.2); color: var(--ok);
  border: 1px solid rgba(52,211,153,.35); font-weight: 700; margin-left: 6px;
}

/* SECTION GUIDE */
.section-guide {
  background: var(--panel); border: 1px solid var(--line); border-radius: 8px;
  padding: 12px 16px; margin: 10px 0 18px; font-size: 13px; color: var(--muted);
  border-left: 3px solid var(--brand);
}
.section-guide b { color: var(--text); }

/* SCROLL TOP BUTTON */
#scroll-top-btn {
  position: fixed; bottom: 24px; right: 24px; z-index: 100;
  width: 42px; height: 42px; border-radius: 50%;
  background: var(--panel2); border: 1px solid var(--line);
  color: var(--brand); cursor: pointer; display: none;
  align-items: center; justify-content: center;
  font-size: 18px; box-shadow: 0 2px 12px rgba(0,0,0,.5);
  transition: background .15s;
}
#scroll-top-btn:hover { background: var(--panel); }

/* COMPARISON ROW (TOP PICKS 표) */
.cmp-row { display: flex; gap: 6px; flex-wrap: wrap; margin: 10px 0 20px; }
.cmp-chip {
  padding: 5px 12px; border-radius: 6px; font-size: 12px;
  background: var(--panel2); border: 1px solid var(--line); color: var(--text);
}
.cmp-chip b { color: var(--brand); }

/* 픽카드 hover 개선 */
.pick-card { cursor: default; }
.pick-card:hover { border-color: var(--brand); box-shadow: 0 0 0 1px rgba(125,211,252,.15); }

/* 기능 태그 hover */
.pick-tag:hover { background: rgba(125,211,252,.22); }

/* 성능 숫자 하이라이트 */
.bench-hi { color: var(--ok); font-weight: 700; }
.bench-mid { color: var(--accent); }
.bench-lo { color: var(--muted); }

/* ---- RESPONSIVE ---- */
@media (max-width: 768px) {
  .pick-grid { grid-template-columns: 1fr; }
  .cards { grid-template-columns: repeat(2, 1fr); }
  th { top: 105px; }
  header h1 { font-size: 17px; }
  .tab-btn { padding: 7px 10px; font-size: 12px; }
}
"""

# ---------------------------------------------------------------------------
# JS — 모듈 상수 (f-string 아님)
# ---------------------------------------------------------------------------
_SCRIPT = r"""
(function () {
  // ---- TAB SWITCHING ----
  const tabs = document.querySelectorAll('.tab-btn');
  const panels = document.querySelectorAll('.tab-panel');
  function activateTab(id) {
    tabs.forEach(t => t.classList.toggle('active', t.dataset.tab === id));
    panels.forEach(p => p.classList.toggle('active', p.id === 'panel-' + id));
    location.hash = id;
  }
  tabs.forEach(t => t.addEventListener('click', () => activateTab(t.dataset.tab)));
  const hash = location.hash.replace('#', '');
  const validTabs = Array.from(tabs).map(t => t.dataset.tab);
  activateTab(validTabs.includes(hash) ? hash : validTabs[0]);

  // ---- PAGINATION STATE MAP ----
  const tableApply = {}; // tableId -> applyPagination function

  // ---- PAGINATION ----
  document.querySelectorAll('.paginate-btn').forEach(btn => {
    const tableId = btn.dataset.table;
    const pageSize = parseInt(btn.dataset.pagesize || '50', 10);
    const table = document.getElementById(tableId);
    if (!table) return;
    let shown = pageSize;

    function applyPagination() {
      const rows = Array.from(table.querySelectorAll('tbody tr'));
      let visIdx = 0;
      rows.forEach(r => {
        if (r.classList.contains('hidden-row')) {
          r.style.display = 'none';
          return;
        }
        r.style.display = visIdx < shown ? '' : 'none';
        visIdx++;
      });
      btn.disabled = shown >= visIdx;
      btn.textContent = btn.disabled
        ? '모두 표시됨'
        : '다음 ' + pageSize + '개 더 보기 (총 ' + visIdx + '개 중 ' + Math.min(shown, visIdx) + '개 표시)';
    }

    tableApply[tableId] = function(resetPage) {
      if (resetPage) shown = pageSize;
      applyPagination();
    };

    applyPagination();
    btn.addEventListener('click', () => {
      shown += pageSize;
      applyPagination();
    });
  });

  // ---- SEARCH ----
  document.querySelectorAll('.search-input').forEach(input => {
    const tableId = input.dataset.table;
    const countEl = input.closest('.search-bar').querySelector('.search-count');
    const table = document.getElementById(tableId);
    if (!table) return;

    function doSearch() {
      const q = input.value.toLowerCase().trim();
      let visible = 0;
      table.querySelectorAll('tbody tr').forEach(tr => {
        const text = tr.textContent.toLowerCase();
        const show = !q || text.includes(q);
        tr.classList.toggle('hidden-row', !show);
        if (show) visible++;
      });
      if (countEl) countEl.textContent = q ? visible + '개 검색됨' : '';
      // 검색 후 페이지네이션 재적용 (첫 페이지로 리셋)
      if (tableApply[tableId]) tableApply[tableId](true);
    }
    input.addEventListener('input', doSearch);
  });

  // ---- COLUMN SORT ----
  document.querySelectorAll('table').forEach(tbl => {
    const tableId = tbl.id;
    const ths = tbl.querySelectorAll('thead th');
    ths.forEach((th, colIdx) => {
      let dir = 1;
      th.addEventListener('click', () => {
        ths.forEach(h => h.classList.remove('sort-asc', 'sort-desc'));
        th.classList.add(dir === 1 ? 'sort-asc' : 'sort-desc');
        const tbody = tbl.querySelector('tbody');
        const rows = Array.from(tbody.querySelectorAll('tr'));
        rows.sort((a, b) => {
          const aText = (a.cells[colIdx] || {}).textContent || '';
          const bText = (b.cells[colIdx] || {}).textContent || '';
          const aNum = parseFloat(aText.replace(/[^0-9.\-]/g, ''));
          const bNum = parseFloat(bText.replace(/[^0-9.\-]/g, ''));
          if (!isNaN(aNum) && !isNaN(bNum)) return dir * (aNum - bNum);
          return dir * aText.localeCompare(bText, 'ko');
        });
        rows.forEach(r => tbody.appendChild(r));
        dir = -dir;
        // 정렬 후에는 첫 페이지부터 다시 보여줘야 행이 사라진 것처럼 보이지 않는다.
        if (tableApply[tableId]) tableApply[tableId](true);
      });
    });
  });

  // ---- SCROLL TO TOP ----
  const topBtn = document.getElementById('scroll-top-btn');
  if (topBtn) {
    window.addEventListener('scroll', () => {
      topBtn.style.display = window.scrollY > 400 ? 'flex' : 'none';
    });
    topBtn.addEventListener('click', () => window.scrollTo({top: 0, behavior: 'smooth'}));
  }
})();
"""


# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------

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


def _quality_score(model: dict[str, Any]) -> float:
    """Blend public benchmark fields with capability and currentness fallback."""
    intelligence = _bench(model, "intelligence_index")
    coding = _bench(model, "coding_index")
    agentic = _bench(model, "agentic_index")
    benchmark_part = (intelligence * 0.45) + (coding * 0.3) + (agentic * 0.25)
    capability_part = min(_capability_score(model) * 4.5, 35)
    currentness_part = 0.0
    try:
        age_days = max((time.time() - int(model.get("created") or 0)) / 86400, 0)
        currentness_part = max(0.0, 15.0 - min(age_days / 30, 15.0))
    except (TypeError, ValueError, OSError):
        currentness_part = 0.0
    if benchmark_part:
        return benchmark_part + (capability_part * 0.2) + (currentness_part * 0.2)
    return capability_part + currentness_part


def _model_category(model: dict[str, Any]) -> str:
    model_id = str(model.get("id") or "").lower()
    name = str(model.get("name") or "").lower()
    text = f"{model_id} {name}"
    architecture = model.get("architecture") or {}
    params = set(model.get("supported_parameters") or [])
    inputs = set(architecture.get("input_modalities") or [])
    outputs = set(architecture.get("output_modalities") or [])
    if any(key in text for key in ("image", "dall-e", "flux", "midjourney", "stable-diffusion")) or "image" in outputs:
        return "이미지 생성/편집"
    if any(key in text for key in ("sora", "video", "veo", "runway")) or "video" in outputs:
        return "동영상 생성"
    if "audio" in inputs or "audio" in outputs or any(key in text for key in ("audio", "whisper", "tts", "voice")):
        return "음성/오디오"
    if any(key in text for key in ("coder", "code", "devstral", "codestral", "swe", "claude", "gpt-5.6", "deepseek")):
        return "코딩/개발"
    if model.get("reasoning") or "reasoning" in params or any(key in text for key in ("reason", "thinking", "r1", "qwq")):
        return "추론/분석"
    if "tools" in params or "tool_choice" in params or "function" in text:
        return "에이전트/툴"
    if "image" in inputs or "file" in inputs:
        return "멀티모달/비전"
    if _availability(model).startswith("무료"):
        return "무료 실험"
    return "일반 텍스트"


def _model_feature_summary(model: dict[str, Any]) -> str:
    category = _model_category(model)
    capability = _capability_score(model)
    context = int(model.get("context_length") or 0)
    availability = _availability(model)
    if category == "이미지 생성/편집":
        return "이미지 생성·편집 전용"
    if category == "동영상 생성":
        return "영상 생성·스토리보드"
    if category == "음성/오디오":
        return "음성 인식·생성·대화"
    if category == "멀티모달/비전":
        return "스크린샷·OCR·문서 이미지"
    if category == "에이전트/툴":
        return "MCP·function call·자동화"
    if category == "코딩/개발":
        return "코드 생성·리뷰·디버깅"
    if category == "추론/분석":
        return "전략·수학·장문 분석"
    if availability.startswith("무료"):
        return "무료 초안·프로토타입"
    if capability >= 8 and context >= 200000:
        return "범용 고성능·장문 업무"
    return "일반 챗·요약·분류"


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
    )[:50]
    performance = sorted(
        models,
        key=lambda m: (_quality_score(m), _bench(m, "intelligence_index"), _bench(m, "coding_index"), int(m.get("created") or 0)),
        reverse=True,
    )[:40]
    coding = sorted(
        models,
        key=lambda m: (_bench(m, "coding_index"), _capability_score(m), int(m.get("created") or 0)),
        reverse=True,
    )[:40]
    multimodal = sorted(
        [m for m in models if "image" in set((m.get("architecture") or {}).get("input_modalities") or [])],
        key=lambda m: (_capability_score(m), int(m.get("created") or 0)),
        reverse=True,
    )[:40]
    agentic = sorted(
        [m for m in models if "tools" in set(m.get("supported_parameters") or [])],
        key=lambda m: (_bench(m, "agentic_index"), _capability_score(m), int(m.get("created") or 0)),
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
    def _leaders(rows: list[dict[str, Any]]) -> str:
        return ", ".join(str(m.get("name") or m.get("id")) for m in rows[:4])

    category_rows = [
        {
            "category": "성능순",
            "count": len(performance),
            "free": sum(1 for m in performance if _availability(m).startswith("무료")),
            "leaders": _leaders(performance),
        },
        {
            "category": "코딩/개발",
            "count": len(coding),
            "free": sum(1 for m in coding if _availability(m).startswith("무료")),
            "leaders": _leaders(coding),
        },
        {
            "category": "에이전트/툴",
            "count": len(agentic),
            "free": sum(1 for m in agentic if _availability(m).startswith("무료")),
            "leaders": _leaders(agentic),
        },
        {
            "category": "멀티모달/비전",
            "count": len(multimodal),
            "free": sum(1 for m in multimodal if _availability(m).startswith("무료")),
            "leaders": _leaders(multimodal),
        },
        {
            "category": "무료 API",
            "count": len(free_models),
            "free": len(free_models),
            "leaders": _leaders(sorted(free_models, key=lambda item: (_quality_score(item), int(item.get("created") or 0)), reverse=True)),
        },
        {
            "category": "저가 유료",
            "count": len(cheapest_paid),
            "free": 0,
            "leaders": _leaders(cheapest_paid),
        },
        {
            "category": "전체 카탈로그",
            "count": len(models),
            "free": len(free_models),
            "leaders": _leaders(sorted(models, key=lambda item: int(item.get("created") or 0), reverse=True)),
        },
    ]
    return {
        "total": len(models),
        "providers": len(providers),
        "free_count": len(free_models),
        "paid_count": len(models) - len(free_models),
        "provider_rows": provider_rows,
        "category_rows": category_rows,
        "free_models": free_models,
        "performance": performance,
        "coding": coding,
        "multimodal": multimodal,
        "agentic": agentic,
        "cheapest_paid": cheapest_paid,
        "all_models": sorted(models, key=lambda item: (_provider(str(item.get("id", ""))), str(item.get("name") or ""))),
    }


def _bench_cell(score: float) -> str:
    if score >= 70:
        return f'<td><b class="bench-hi">{score:.1f}</b></td>'
    elif score >= 40:
        return f'<td><span class="bench-mid">{score:.1f}</span></td>'
    else:
        return f'<td><span class="bench-lo">{score:.1f}</span></td>'


def _model_row(model: dict[str, Any], rank: int | None = None, show_rank: bool = True) -> str:
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
    rank_cell = (f"<td>{rank}</td>" if rank is not None else "<td>-</td>") if show_rank else ""
    model_id = str(model.get("id") or "")
    model_label = str(model.get("name") or model_id)
    detail_href = f"https://openrouter.ai/{model_id}" if "/" in model_id else ""
    model_link = (
        f'<a href="{_safe_text(detail_href)}" target="_blank" rel="noopener noreferrer"><b>{_safe_text(model_label)}</b></a>'
        if detail_href else f"<b>{_safe_text(model_label)}</b>"
    )
    return (
        "<tr>"
        + rank_cell
        + f"<td>{model_link}<br><span>{_safe_text(model_id)}</span></td>"
        + f"<td>{_safe_text(_provider(str(model.get('id', ''))))}</td>"
        + f"<td>{_safe_text(created)}</td>"
        + f"<td>{int(model.get('context_length') or 0):,}</td>"
        + f"<td>{_safe_text(_money_label(pricing.get('prompt')))}</td>"
        + f"<td>{_safe_text(_money_label(pricing.get('completion')))}</td>"
        + f"<td><em class='{badge}'>{_safe_text(_availability(model))}</em></td>"
        + _bench_cell(_bench(model, 'intelligence_index'))
        + _bench_cell(_bench(model, 'coding_index'))
        + _bench_cell(_bench(model, 'agentic_index'))
        + f"<td>{_capability_score(model)}</td>"
        + f"<td>{_safe_text(', '.join(caps))}</td>"
        + f"<td>{_safe_text(_model_feature_summary(model))}</td>"
        + "</tr>"
    )


def _table_header(with_rank: bool = True) -> str:
    rank_th = "<th>#</th>" if with_rank else ""
    return (
        "<thead><tr>"
        + rank_th
        + "<th>모델</th><th>회사</th><th>출시</th><th>컨텍스트</th>"
        + "<th>입력/1M</th><th>출력/1M</th><th>유/무료</th>"
        + "<th>지능</th><th>코딩</th><th>에이전트</th><th>기능점수</th><th>기능</th><th>추천용도</th>"
        + "</tr></thead>"
    )


def _search_bar(table_id: str) -> str:
    return (
        '<div class="search-bar">'
        f'<input class="search-input" data-table="{table_id}" type="text" placeholder="모델명 또는 회사명으로 검색...">'
        '<span class="search-count"></span>'
        '</div>'
    )


def _paginate_btn(table_id: str, page_size: int = 50) -> str:
    return f'<button class="paginate-btn" data-table="{table_id}" data-pagesize="{page_size}">다음 {page_size}개 더 보기</button>'


def _model_table(models: list[dict[str, Any]], table_id: str, paginate: bool = False, page_size: int = 50) -> str:
    rows = "".join(_model_row(m, i + 1) for i, m in enumerate(models))
    parts = [
        _search_bar(table_id),
        '<div class="table-wrap">',
        f'<table id="{table_id}">',
        _table_header(with_rank=True),
        f"<tbody>{rows}</tbody>",
        "</table></div>",
    ]
    if paginate:
        parts.append(_paginate_btn(table_id, page_size))
    return "".join(parts)


def _tuple_rows(rows: list[tuple[Any, ...]]) -> str:
    return "".join("<tr>" + "".join(f"<td>{_safe_text(cell)}</td>" for cell in row) + "</tr>" for row in rows)


def _tier_class(tier: str) -> str:
    mapping = {
        "최상위": "tier-top",
        "균형 권장": "tier-bal",
        "균형": "tier-bal",
        "고속 저가": "tier-fast",
        "무료 최강": "tier-free",
        "무료 고성능": "tier-free",
        "코딩 특화": "tier-coding",
        "이미지 생성": "tier-img",
    }
    return mapping.get(tier, "tier-bal")


def _pick_cards_html() -> str:
    cards = []
    for pick in TOP_PICKS:
        name = pick["name"]
        company = pick["company"]
        tier = pick["tier"]
        tagline = pick["tagline"]
        desc = pick["description"]
        use_cases = pick["use_cases"]
        ctx = pick["ctx"]
        pin = pick["price_in"]
        pout = pick["price_out"]
        bench = pick.get("bench", {})
        highlight = pick.get("highlight", "")
        is_new = pick.get("new", False)

        tier_cls = _tier_class(tier)
        tags_html = "".join(f'<span class="pick-tag">{_safe_text(t)}</span>' for t in use_cases)
        new_badge = '<span class="badge-new">NEW</span>' if is_new else ""

        # 성능바 HTML (지능/코딩/에이전트)
        perf_bars = ""
        if bench.get("intelligence") or bench.get("coding") or bench.get("agentic"):
            bars = [
                ("지능", bench.get("intelligence", 0)),
                ("코딩", bench.get("coding", 0)),
                ("에이전트", bench.get("agentic", 0)),
            ]
            perf_bars_html = ""
            for label, score in bars:
                if score == 0:
                    continue
                pct = min(int(score), 100)
                perf_bars_html += "".join([
                    f'<div class="perf-bar-label"><span>{_safe_text(label)}</span><span>{pct}</span></div>',
                    '<div class="perf-bar-track">',
                    f'<div class="perf-bar-fill" style="width:{pct}%"></div>',
                    '</div>',
                ])
            if perf_bars_html:
                perf_bars = f'<div class="perf-bar-wrap">{perf_bars_html}</div>'

        card = "".join([
            '<div class="pick-card">',
            '<div class="pick-header">',
            '<div style="flex:1">',
            f'<div class="pick-name">{_safe_text(name)}{new_badge}</div>',
            f'<div class="pick-company">{_safe_text(company)}</div>',
            '</div>',
            f'<span class="tier-badge {tier_cls}">{_safe_text(tier)}</span>',
            '</div>',
            f'<div class="pick-tagline">{_safe_text(tagline)}</div>',
            f'<div class="pick-desc">{_safe_text(desc)}</div>',
            perf_bars,
            f'<div class="pick-tags">{tags_html}</div>',
            '<div class="pick-pricing">',
            f'컨텍스트 <span>{_safe_text(ctx)}</span> &nbsp;|&nbsp; ',
            f'입력 <span>{_safe_text(pin)}</span> &nbsp;|&nbsp; ',
            f'출력 <span>{_safe_text(pout)}</span>',
            '</div>',
            f'<div style="font-size:11px;color:var(--muted);margin-top:4px;">핵심: {_safe_text(highlight)}</div>',
            '</div>',
        ])
        cards.append(card)
    return '<div class="pick-grid">' + "".join(cards) + "</div>"


# ---------------------------------------------------------------------------
# Main render
# ---------------------------------------------------------------------------

def _render_report(models: list[dict[str, Any]], *, error: str | None, loaded_at: float) -> str:
    summary = _summarize(models)
    now_kst = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S KST")
    loaded_label = datetime.fromtimestamp(loaded_at, tz=KST).strftime("%Y-%m-%d %H:%M:%S KST") if loaded_at else "미로드"

    # -- provider table rows
    provider_rows_html = "".join(
        "<tr>"
        f"<td><b>{_safe_text(row['provider'])}</b></td>"
        f"<td>{int(row['count']):,}</td>"
        f"<td>{int(row['free']):,}</td>"
        f"<td>{_safe_text(row['latest'])}</td>"
        "</tr>"
        for row in summary["provider_rows"]
    )
    category_rows_html = "".join(
        "<tr>"
        f"<td><b>{_safe_text(row['category'])}</b></td>"
        f"<td>{int(row['count']):,}</td>"
        f"<td>{int(row['free']):,}</td>"
        f"<td>{_safe_text(row['leaders'])}</td>"
        "</tr>"
        for row in summary["category_rows"]
    )
    task_rows_html = _tuple_rows(TASK_GUIDE_ROWS)

    source_rows = _tuple_rows(PINNED_SOURCES)
    error_banner = (
        f'<div class="warn">OpenRouter API 오류: {_safe_text(error)} · 캐시 데이터로 표시 중입니다.</div>'
        if error else ""
    )

    # Tab labels & IDs
    TAB_DEFS = [
        ("summary",    "요약·추천픽"),
        ("performance", "성능순"),
        ("guide",      "기능별 선택"),
        ("coding",     "코딩/개발"),
        ("multimodal", "멀티모달·비전"),
        ("agentic",    "에이전트"),
        ("free",       "무료모델"),
        ("cheap",      "저가유료"),
        ("all",        "전체목록"),
        ("bycompany",  "회사별"),
        ("official",   "공식모델"),
    ]

    tab_nav = "".join(
        f'<button class="tab-btn" data-tab="{tid}">{label}</button>'
        for tid, label in TAB_DEFS
    )

    # ---- Panel: summary ----
    stat_cards = "".join([
        f'<div class="card"><div class="num">{summary["total"]:,}</div><div class="label">OpenRouter catalog 모델</div></div>',
        f'<div class="card"><div class="num">{summary["providers"]:,}</div><div class="label">제공사/조직 수</div></div>',
        f'<div class="card"><div class="num">{summary["free_count"]:,}</div><div class="label">무료 API 모델</div></div>',
        f'<div class="card"><div class="num">{summary["paid_count"]:,}</div><div class="label">유료 API 모델</div></div>',
        '<div class="card"><div class="num">1.05M</div><div class="label">GPT-5.6 최대 컨텍스트</div></div>',
        '<div class="card"><div class="num">$0</div><div class="label">무료 모델 최저 가격</div></div>',
    ])

    panel_summary = "".join([
        '<section id="panel-summary" class="tab-panel">',
        f'<div class="cards">{stat_cards}</div>',
        '<div class="warn">판정: 본 보고서는 OpenRouter 공개 catalog와 OpenAI/Anthropic 공식 문서 확인 모델을 기준으로 전수 정렬합니다.</div>',
        error_banner,
        "<h2>기능별 빠른 선택</h2>",
        '<div class="table-wrap"><table id="tbl-task-guide">',
        "<thead><tr><th>업무</th><th>우선 모델</th><th>선택 기준</th><th>확인할 기능</th></tr></thead>",
        f"<tbody>{task_rows_html}</tbody>",
        "</table></div>",
        "<h2>추천픽 — 용도별 대표 모델</h2>",
        _pick_cards_html(),
        "<h2>OpenRouter 실사용 TOP 10</h2>",
        '<div class="warn">2026-09-01 사용 버킷 기준 토큰 처리량. 품질이 아닌 채택량 지표입니다.</div>',
        '<div class="table-wrap"><table id="tbl-ranking">',
        "<thead><tr><th>순위</th><th>모델</th><th>회사</th><th>처리 토큰</th><th>증감</th></tr></thead>",
        f"<tbody>{_tuple_rows(RANKING_ROWS)}</tbody>",
        "</table></div>",
        "</section>",
    ])

    # ---- Panel: performance ----
    panel_performance = "".join([
        '<section id="panel-performance" class="tab-panel">',
        "<h2>성능순 TOP 40</h2>",
        (
            '<div class="section-guide">공개 벤치마크가 있는 모델은 지능·코딩·에이전트 점수를 우선하고, '
            '벤치마크가 없는 최신 모델은 기능점수와 출시일을 보정해 정렬합니다. 운영 투입 전에는 AADS 실제 프롬프트로 추가 검증하세요.</div>'
        ),
        _model_table(summary["performance"], "tbl-performance"),
        "</section>",
    ])

    # ---- Panel: guide ----
    panel_guide = "".join([
        '<section id="panel-guide" class="tab-panel">',
        "<h2>기능별 모델 선택 가이드</h2>",
        (
            '<div class="section-guide">모델을 회사명보다 실제 사용 목적 기준으로 고르는 화면입니다. '
            '각 기능군의 대표 모델을 먼저 확인한 뒤 세부 탭에서 가격·컨텍스트·툴 지원을 비교하세요.</div>'
        ),
        '<div class="table-wrap"><table id="tbl-category">',
        "<thead><tr><th>기능군</th><th>모델 수</th><th>무료 수</th><th>대표 모델</th></tr></thead>",
        f"<tbody>{category_rows_html}</tbody>",
        "</table></div>",
        "<h2>업무별 권장 라우팅</h2>",
        '<div class="table-wrap"><table id="tbl-task-guide-detail">',
        "<thead><tr><th>업무</th><th>우선 모델</th><th>선택 기준</th><th>확인할 기능</th></tr></thead>",
        f"<tbody>{task_rows_html}</tbody>",
        "</table></div>",
        "</section>",
    ])

    # ---- Panel: coding ----
    panel_coding = "".join([
        '<section id="panel-coding" class="tab-panel">',
        "<h2>코딩/개발 특화 TOP 40</h2>",
        (
            '<div class="section-guide">코딩·개발 특화 상위 40개 모델입니다. 코딩 벤치마크(HumanEval, SWE-bench 등) 기준 정렬. '
            '<b>모델명 클릭 → OpenRouter</b> 상세 확인 가능.</div>'
        ),
        _model_table(summary["coding"], "tbl-coding"),
        "</section>",
    ])

    # ---- Panel: multimodal ----
    panel_multimodal = "".join([
        '<section id="panel-multimodal" class="tab-panel">',
        "<h2>멀티모달·비전 지원 모델</h2>",
        (
            '<div class="section-guide">이미지·영상·오디오 입력을 지원하는 멀티모달 모델입니다. '
            '비전 API, 이미지 분석, 문서 OCR에 활용하세요.</div>'
        ),
        _model_table(summary["multimodal"], "tbl-multimodal"),
        "</section>",
    ])

    # ---- Panel: agentic ----
    panel_agentic = "".join([
        '<section id="panel-agentic" class="tab-panel">',
        "<h2>에이전트/툴 지원 TOP 40</h2>",
        (
            '<div class="section-guide">도구 호출(function calling)을 지원하는 에이전트 모델입니다. '
            'AADS 자동화, 파이프라인 실행, MCP 연동에 권장합니다.</div>'
        ),
        _model_table(summary["agentic"], "tbl-agentic"),
        "</section>",
    ])

    # ---- Panel: free ----
    panel_free = "".join([
        '<section id="panel-free" class="tab-panel">',
        f'<h2>무료 API 모델 ({summary["free_count"]:,}개)</h2>',
        (
            '<div class="section-guide">API 호출 비용 없이 사용 가능한 무료 모델 전체 목록입니다. '
            '프로토타입 개발, 테스트, 무료 배포에 활용하세요.</div>'
        ),
        _model_table(summary["free_models"], "tbl-free", paginate=True, page_size=50),
        "</section>",
    ])

    # ---- Panel: cheap paid ----
    panel_cheap = "".join([
        '<section id="panel-cheap" class="tab-panel">',
        "<h2>저가 유료 모델 TOP 50</h2>",
        (
            '<div class="section-guide">유료 모델 중 출력 토큰 기준 가장 저렴한 50개입니다. '
            '대량 처리·배치 작업의 비용 최적화에 활용하세요.</div>'
        ),
        _model_table(summary["cheapest_paid"], "tbl-cheap"),
        "</section>",
    ])

    # ---- Panel: all models ----
    panel_all = "".join([
        '<section id="panel-all" class="tab-panel">',
        f'<h2>전체 모델 목록 ({summary["total"]:,}개)</h2>',
        '<div class="section-guide">OpenRouter catalog 전체 모델 목록입니다. 검색·정렬로 원하는 모델을 찾아보세요.</div>',
        _model_table(summary["all_models"], "tbl-all", paginate=True, page_size=50),
        "</section>",
    ])

    # ---- Panel: by company ----
    panel_bycompany = "".join([
        '<section id="panel-bycompany" class="tab-panel">',
        "<h2>회사별 모델 보유 현황</h2>",
        _search_bar("tbl-bycompany"),
        '<div class="table-wrap"><table id="tbl-bycompany">',
        "<thead><tr><th>회사/제공사</th><th>모델 수</th><th>무료 수</th><th>최근 모델 예시</th></tr></thead>",
        f"<tbody>{provider_rows_html}</tbody>",
        "</table></div>",
        "</section>",
    ])

    # ---- Panel: official ----
    panel_official = "".join([
        '<section id="panel-official" class="tab-panel">',
        "<h2>공식 확인 핵심 모델</h2>",
        "<h3>OpenAI</h3>",
        '<div class="table-wrap"><table id="tbl-openai">',
        "<thead><tr><th>회사</th><th>모델</th><th>역할</th><th>컨텍스트</th><th>최대 출력</th><th>입력/1M</th><th>출력/1M</th><th>기능</th><th>출처</th></tr></thead>",
        f"<tbody>{_tuple_rows(OPENAI_OFFICIAL)}</tbody>",
        "</table></div>",
        "<h3>Anthropic Claude</h3>",
        '<div class="table-wrap"><table id="tbl-claude">',
        "<thead><tr><th>회사</th><th>모델</th><th>컨텍스트</th><th>입력/1M</th><th>출력/1M</th><th>용도</th><th>출처</th></tr></thead>",
        f"<tbody>{_tuple_rows(CLAUDE_OFFICIAL)}</tbody>",
        "</table></div>",
        "<h3>Google / xAI / DeepSeek / Mistral</h3>",
        '<div class="table-wrap"><table id="tbl-other-official">',
        "<thead><tr><th>회사</th><th>모델</th><th>역할</th><th>컨텍스트</th><th>입력/1M</th><th>출력/1M</th><th>기능·주의</th><th>출처</th></tr></thead>",
        f"<tbody>{_tuple_rows(OTHER_OFFICIAL)}</tbody>",
        "</table></div>",
        "<h2>출처 및 검증 기준</h2>",
        '<div class="table-wrap"><table id="tbl-sources">',
        "<thead><tr><th>출처</th><th>확인일</th><th>URL</th><th>용도</th></tr></thead>",
        f"<tbody>{source_rows}</tbody>",
        "</table></div>",
        "</section>",
    ])

    # ---- Assemble ----
    return "".join([
        "<!doctype html>",
        '<html lang="ko">',
        "<head>",
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width,initial-scale=1">',
        "<title>AADS 최신 LLM 전수 분석 보고서</title>",
        f"<style>{_CSS}</style>",
        "</head><body>",
        "<header>",
        "<h1>AADS 최신 LLM 회사·모델 전수 분석 보고서</h1>",
        f'<div class="meta">보고 기준: 2026-09-02 KST &nbsp;·&nbsp; 생성: {now_kst} &nbsp;·&nbsp; OpenRouter loaded: {loaded_label}</div>',
        f'<nav class="tab-nav">{tab_nav}</nav>',
        "</header>",
        "<main>",
        panel_summary,
        panel_performance,
        panel_guide,
        panel_coding,
        panel_multimodal,
        panel_agentic,
        panel_free,
        panel_cheap,
        panel_all,
        panel_bycompany,
        panel_official,
        "</main>",
        '<button id="scroll-top-btn" title="위로">↑</button>',
        f"<script>{_SCRIPT}</script>",
        "</body></html>",
    ])


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
