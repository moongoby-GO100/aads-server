"""
LLM 디자인 감리 엔진 (6단계 스코어카드) — T-025.

DesignAuditor 클래스:
  - audit_screenshot(screenshot_path, project_context) → AuditResult
  - audit_multiple(screenshot_paths) → List[AuditResult]
  - generate_report(audit_results) → str (마크다운)

LLM: Gemini 2.5 Flash Vision (primary) → Claude Sonnet Vision (fallback)
결과: experience_memory에 experience_type="design_audit"로 저장
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import structlog

logger = structlog.get_logger()

# ---------------------------------------------------------------------------
# 프롬프트 상수
# ---------------------------------------------------------------------------

AUDIT_PROMPT = """
당신은 10년 경력의 UI/UX 디자인 감리관입니다.
첨부된 웹 페이지 스크린샷을 아래 5개 기준으로 검수하세요.

[평가 항목]
1. 시각 일관성 (Visual Consistency): 여백, 정렬, 컴포넌트 스타일 통일 (10점)
2. 접근성 (Accessibility): 색상 대비, 터치 영역, 텍스트 가독성 (10점)
3. 인터랙션 명확성 (Interaction Clarity): 버튼 구분, 상태 표시, 네비게이션 (10점)
4. 브랜드 일관성 (Brand Coherence): 색상 톤, 폰트, 전체 무드 (10점)
5. 완성도 (Polish): 빈 공간, 깨진 요소, 로딩 상태, 에러 처리 (10점)

[출력 형식 - 반드시 JSON]
{
  "scores": {
    "visual_consistency": {"score": 8, "issues": ["..."], "fixes": ["..."]},
    "accessibility": {"score": 7, "issues": ["..."], "fixes": ["..."]},
    "interaction_clarity": {"score": 9, "issues": [], "fixes": []},
    "brand_coherence": {"score": 8, "issues": ["..."], "fixes": ["..."]},
    "polish": {"score": 6, "issues": ["..."], "fixes": ["..."]}
  },
  "total_score": 38,
  "verdict": "PASS",
  "summary": "한 줄 요약",
  "critical_issues": ["즉시 수정 필요 항목"]
}

판정 기준: PASS(35+) / CONDITIONAL(25-34) / FAIL(24 이하)
"""

IMAGE_AUDIT_PROMPT = """
시니어 이커머스 이미지 검수관으로서 아래 상품 이미지를 6가지 기준으로 심사하세요.
## 기준 (각 10점, 총 60점)
1. resolution_clarity (해상도/선명도): 최소 800x800, 블러없음, 노이즈없음
2. background_quality (배경): 깨끗한 배경, 불필요 요소 없음, 일관성
3. product_visibility (상품 가시성): 상품이 화면의 60%+, 잘림없음, 그림자적절
4. color_accuracy (색상 정확도): 자연스러운 색감, 과보정없음, 화이트밸런스
5. text_overlay (텍스트/워터마크): 가독성, 위치적절, 상품가림없음 (텍스트없으면 10점)
6. commercial_readiness (상업적 완성도): 구매 전환 유도, 신뢰감, 프로 수준
## JSON만 반환
{"scores":{...},"total_score":0,"verdict":"PASS|CONDITIONAL|FAIL","summary":"","critical_issues":[]}
판정: PASS 48+(80%), CONDITIONAL 36-47(60-79%), FAIL 35이하
"""

IMAGE_AUDIT_CATEGORIES = (
    "resolution_clarity",
    "background_quality",
    "product_visibility",
    "color_accuracy",
    "text_overlay",
    "commercial_readiness",
)

MOBILE_AUDIT_PROMPT = """
모바일 앱 스크린샷을 분석하세요. 6개 항목 각 10점(총 60점):
1. layout_consistency (레이아웃 정렬, 여백, 간격)
2. touch_target_size (터치 영역 최소 44x44dp)
3. text_readability (폰트 크기, 대비, 가독성)
4. navigation_clarity (내비게이션 명확성, 뒤로가기)
5. visual_hierarchy (시각적 계층, CTA 버튼 강조)
6. platform_compliance (Material Design / iOS HIG 준수)

[출력 형식 - 반드시 JSON]
{
  "scores": {
    "layout_consistency": {"score": 8, "issues": ["..."], "fixes": ["..."]},
    "touch_target_size": {"score": 7, "issues": ["..."], "fixes": ["..."]},
    "text_readability": {"score": 9, "issues": [], "fixes": []},
    "navigation_clarity": {"score": 8, "issues": ["..."], "fixes": ["..."]},
    "visual_hierarchy": {"score": 7, "issues": ["..."], "fixes": ["..."]},
    "platform_compliance": {"score": 8, "issues": ["..."], "fixes": ["..."]}
  },
  "total_score": 47,
  "verdict": "CONDITIONAL",
  "summary": "한 줄 요약",
  "critical_issues": ["즉시 수정 필요 항목"],
  "platform": "android"
}

판정 기준: PASS 48+(80%), CONDITIONAL 36-47, FAIL 35이하
"""

MOBILE_AUDIT_CATEGORIES = (
    "layout_consistency",
    "touch_target_size",
    "text_readability",
    "navigation_clarity",
    "visual_hierarchy",
    "platform_compliance",
)


def _calc_mobile_verdict(total: int) -> str:
    if total >= 48:
        return "PASS"
    if total >= 36:
        return "CONDITIONAL"
    return "FAIL"


def _calc_image_verdict(total: int) -> str:
    if total >= 48:
        return "PASS"
    if total >= 36:
        return "CONDITIONAL"
    return "FAIL"


# ---------------------------------------------------------------------------
# 데이터 클래스
# ---------------------------------------------------------------------------

@dataclass
class CategoryScore:
    score: int
    issues: List[str] = field(default_factory=list)
    fixes: List[str] = field(default_factory=list)


@dataclass
class AuditResult:
    screenshot_path: str
    scores: Dict[str, CategoryScore]
    total_score: int
    verdict: str          # PASS / CONDITIONAL / FAIL
    summary: str
    critical_issues: List[str]
    error: Optional[str] = None
    audited_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())

    @classmethod
    def error_result(cls, screenshot_path: str, error: str) -> "AuditResult":
        """LLM 호출 실패 시 에러 결과 반환."""
        return cls(
            screenshot_path=screenshot_path,
            scores={},
            total_score=0,
            verdict="ERROR",
            summary=f"감리 실패: {error}",
            critical_issues=[],
            error=error,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "screenshot_path": self.screenshot_path,
            "scores": {
                k: {"score": v.score, "issues": v.issues, "fixes": v.fixes}
                for k, v in self.scores.items()
            },
            "total_score": self.total_score,
            "verdict": self.verdict,
            "summary": self.summary,
            "critical_issues": self.critical_issues,
            "error": self.error,
            "audited_at": self.audited_at,
        }


# ---------------------------------------------------------------------------
# 헬퍼 — base64 변환
# ---------------------------------------------------------------------------

def _image_to_base64(path: str) -> str:
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


def _extract_json(text: str) -> dict:
    """LLM 응답에서 JSON 블록 추출."""
    # ```json ... ``` 블록 우선
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        return json.loads(m.group(1))
    # 중괄호 영역 직접 탐색
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        return json.loads(m.group(0))
    raise ValueError(f"JSON 없음: {text[:200]}")


def _parse_audit_json(data: dict) -> tuple[Dict[str, CategoryScore], int, str, str, List[str]]:
    """파싱된 JSON → 도메인 객체 변환."""
    scores_raw = data.get("scores", {})
    scores: Dict[str, CategoryScore] = {}
    for key in ("visual_consistency", "accessibility", "interaction_clarity", "brand_coherence", "polish"):
        raw = scores_raw.get(key, {})
        scores[key] = CategoryScore(
            score=int(raw.get("score", 0)),
            issues=raw.get("issues", []),
            fixes=raw.get("fixes", []),
        )

    total_score = int(data.get("total_score", sum(v.score for v in scores.values())))
    verdict = data.get("verdict", _calc_verdict(total_score))
    summary = data.get("summary", "")
    critical_issues = data.get("critical_issues", [])
    return scores, total_score, verdict, summary, critical_issues


def _calc_verdict(total: int) -> str:
    if total >= 35:
        return "PASS"
    if total >= 25:
        return "CONDITIONAL"
    return "FAIL"


# ---------------------------------------------------------------------------
# LLM 호출 함수
# ---------------------------------------------------------------------------

async def _call_gemini_vision(image_b64: str, prompt: str, project_context: str) -> str:
    """Gemini 2.5 Flash Vision API 호출 (google-generativeai)."""
    import google.generativeai as genai

    api_key = os.getenv("GOOGLE_API_KEY", "")
    if not api_key:
        raise ValueError("GOOGLE_API_KEY not set")

    genai.configure(api_key=api_key)

    # gemini-2.5-flash → 실제 API ID (llm/client.py MODEL_ALIASES 참조)
    model = genai.GenerativeModel("gemini-1.5-flash")

    full_prompt = f"{prompt}\n\n[프로젝트 컨텍스트]\n{project_context}" if project_context else prompt

    import PIL.Image
    import io
    image_bytes = base64.b64decode(image_b64)
    pil_image = PIL.Image.open(io.BytesIO(image_bytes))

    loop = asyncio.get_event_loop()
    response = await loop.run_in_executor(
        None,
        lambda: model.generate_content([full_prompt, pil_image]),
    )
    return response.text


async def _call_claude_vision(image_b64: str, prompt: str, project_context: str) -> str:
    """Claude Sonnet Vision API 폴백 호출 (anthropic SDK)."""
    import anthropic

    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY not set")

    client = anthropic.AsyncAnthropic(api_key=api_key)

    full_prompt = f"{prompt}\n\n[프로젝트 컨텍스트]\n{project_context}" if project_context else prompt

    message = await client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=2048,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": image_b64,
                        },
                    },
                    {"type": "text", "text": full_prompt},
                ],
            }
        ],
    )
    return message.content[0].text


# ---------------------------------------------------------------------------
# DesignAuditor 클래스
# ---------------------------------------------------------------------------

class DesignAuditor:
    """LLM 기반 UI/UX 디자인 감리 엔진."""

    async def audit_screenshot(
        self,
        screenshot_path: str,
        project_context: str = "",
    ) -> AuditResult:
        """
        단일 스크린샷 LLM 검수.

        1. 이미지 → base64
        2. Gemini 2.5 Flash Vision 호출 (primary)
        3. 실패 시 Claude Sonnet Vision fallback
        4. JSON 파싱 → AuditResult 반환
        5. experience_memory에 저장
        """
        logger.info("design_audit_start", path=screenshot_path)

        if not Path(screenshot_path).exists():
            logger.error("screenshot_not_found", path=screenshot_path)
            return AuditResult.error_result(screenshot_path, f"파일 없음: {screenshot_path}")

        try:
            image_b64 = _image_to_base64(screenshot_path)
        except Exception as e:
            logger.error("image_read_error", path=screenshot_path, error=str(e))
            return AuditResult.error_result(screenshot_path, f"이미지 읽기 실패: {e}")

        raw_text: str = ""
        provider_used: str = ""

        # Primary: Gemini 2.5 Flash Vision
        try:
            raw_text = await _call_gemini_vision(image_b64, AUDIT_PROMPT, project_context)
            provider_used = "gemini-2.5-flash"
            logger.info("gemini_vision_success", path=screenshot_path)
        except Exception as e:
            logger.warning("gemini_vision_failed_fallback", error=str(e))
            # Fallback: Claude Sonnet Vision
            try:
                raw_text = await _call_claude_vision(image_b64, AUDIT_PROMPT, project_context)
                provider_used = "claude-sonnet-4-5"
                logger.info("claude_vision_fallback_success", path=screenshot_path)
            except Exception as e2:
                logger.error("all_vision_providers_failed", error=str(e2))
                return AuditResult.error_result(screenshot_path, f"LLM 호출 실패: {e2}")

        # JSON 파싱
        try:
            data = _extract_json(raw_text)
            scores, total_score, verdict, summary, critical_issues = _parse_audit_json(data)
        except Exception as e:
            logger.error("json_parse_error", raw=raw_text[:300], error=str(e))
            return AuditResult.error_result(screenshot_path, f"JSON 파싱 실패: {e}")

        result = AuditResult(
            screenshot_path=screenshot_path,
            scores=scores,
            total_score=total_score,
            verdict=verdict,
            summary=summary,
            critical_issues=critical_issues,
        )

        logger.info(
            "design_audit_done",
            path=screenshot_path,
            total_score=total_score,
            verdict=verdict,
            provider=provider_used,
        )

        # experience_memory 저장 (graceful degradation)
        await self._save_to_memory(result, project_context, provider_used)

        return result

    async def audit_multiple(
        self,
        screenshot_paths: List[str],
        project_context: str = "",
    ) -> List[AuditResult]:
        """여러 페이지 동시 검수."""
        logger.info("audit_multiple_start", count=len(screenshot_paths))
        tasks = [
            self.audit_screenshot(p, project_context)
            for p in screenshot_paths
        ]
        results = await asyncio.gather(*tasks, return_exceptions=False)
        logger.info("audit_multiple_done", count=len(results))
        return list(results)

    async def audit_product_image(
        self,
        image_path_or_base64: str,
        is_base64: bool = False,
    ) -> dict:
        """
        상품 이미지 심사 → 스코어카드.

        Args:
            image_path_or_base64: 이미지 파일 경로 또는 base64 문자열
            is_base64: True면 base64 문자열로 처리
        Returns:
            스코어카드 dict (scores, total_score, verdict, summary, critical_issues)
        """
        logger.info("image_audit_start", is_base64=is_base64)

        if is_base64:
            image_b64 = image_path_or_base64
            image_ref = "base64_input"
        else:
            if not Path(image_path_or_base64).exists():
                logger.error("image_not_found", path=image_path_or_base64)
                return {
                    "image_ref": image_path_or_base64,
                    "scores": {},
                    "total_score": 0,
                    "verdict": "ERROR",
                    "summary": f"파일 없음: {image_path_or_base64}",
                    "critical_issues": [],
                    "error": f"파일 없음: {image_path_or_base64}",
                }
            try:
                image_b64 = _image_to_base64(image_path_or_base64)
            except Exception as e:
                logger.error("image_read_error", path=image_path_or_base64, error=str(e))
                return {
                    "image_ref": image_path_or_base64,
                    "scores": {},
                    "total_score": 0,
                    "verdict": "ERROR",
                    "summary": f"이미지 읽기 실패: {e}",
                    "critical_issues": [],
                    "error": str(e),
                }
            image_ref = image_path_or_base64

        if not image_b64:
            return {
                "image_ref": image_ref,
                "scores": {},
                "total_score": 0,
                "verdict": "ERROR",
                "summary": "이미지 데이터 없음",
                "critical_issues": ["이미지 base64가 비어 있습니다"],
                "error": "empty_image",
            }

        raw_text = ""
        provider_used = ""
        try:
            raw_text = await _call_gemini_vision(image_b64, IMAGE_AUDIT_PROMPT, "")
            provider_used = "gemini-2.5-flash"
        except Exception as e:
            logger.warning("image_audit_gemini_failed", error=str(e))
            try:
                raw_text = await _call_claude_vision(image_b64, IMAGE_AUDIT_PROMPT, "")
                provider_used = "claude-sonnet-4-5"
            except Exception as e2:
                logger.error("image_audit_all_llm_failed", error=str(e2))
                return {
                    "image_ref": image_ref,
                    "scores": {},
                    "total_score": 0,
                    "verdict": "ERROR",
                    "summary": f"LLM 호출 실패: {e2}",
                    "critical_issues": [],
                    "error": str(e2),
                }

        try:
            data = _extract_json(raw_text)
            scores_raw = data.get("scores", {})
            scores = {}
            for key in IMAGE_AUDIT_CATEGORIES:
                raw = scores_raw.get(key, {})
                scores[key] = {
                    "score": int(raw.get("score", 0)) if isinstance(raw, dict) else int(raw),
                    "issues": raw.get("issues", []) if isinstance(raw, dict) else [],
                    "fixes": raw.get("fixes", []) if isinstance(raw, dict) else [],
                }
            total_score = int(data.get("total_score", sum(v["score"] for v in scores.values())))
            verdict = data.get("verdict", _calc_image_verdict(total_score))
            summary = data.get("summary", "")
            critical_issues = data.get("critical_issues", [])
        except Exception as e:
            logger.error("image_audit_json_parse_error", raw=raw_text[:300], error=str(e))
            return {
                "image_ref": image_ref,
                "scores": {},
                "total_score": 0,
                "verdict": "ERROR",
                "summary": f"JSON 파싱 실패: {e}",
                "critical_issues": [],
                "error": str(e),
            }

        logger.info(
            "image_audit_done",
            image_ref=image_ref,
            total_score=total_score,
            verdict=verdict,
            provider=provider_used,
        )

        return {
            "image_ref": image_ref,
            "scores": scores,
            "total_score": total_score,
            "verdict": verdict,
            "summary": summary,
            "critical_issues": critical_issues,
            "error": None,
            "provider_used": provider_used,
        }

    async def audit_mobile_screen(
        self,
        screenshot_path_or_base64: str,
        platform: str = "android",
        is_base64: bool = False,
    ) -> dict:
        """
        모바일 앱 스크린샷 Gemini Vision 6항목 감리.

        Args:
            screenshot_path_or_base64: 이미지 파일 경로 또는 base64 문자열
            platform: "android" 또는 "ios"
            is_base64: True면 base64 문자열로 처리

        Returns:
            스코어카드 dict (scores, total_score, verdict, platform, summary, critical_issues)
        """
        logger.info("mobile_audit_start", platform=platform, is_base64=is_base64)

        if is_base64:
            image_b64 = screenshot_path_or_base64
            image_ref = "base64_input"
        else:
            if not Path(screenshot_path_or_base64).exists():
                return {
                    "image_ref": screenshot_path_or_base64,
                    "scores": {},
                    "total_score": 0,
                    "verdict": "ERROR",
                    "platform": platform,
                    "summary": f"파일 없음: {screenshot_path_or_base64}",
                    "critical_issues": [],
                    "error": f"파일 없음: {screenshot_path_or_base64}",
                }
            try:
                image_b64 = _image_to_base64(screenshot_path_or_base64)
            except Exception as e:
                return {
                    "image_ref": screenshot_path_or_base64,
                    "scores": {},
                    "total_score": 0,
                    "verdict": "ERROR",
                    "platform": platform,
                    "summary": f"이미지 읽기 실패: {e}",
                    "critical_issues": [],
                    "error": str(e),
                }
            image_ref = screenshot_path_or_base64

        if not image_b64:
            return {
                "image_ref": "empty",
                "scores": {},
                "total_score": 0,
                "verdict": "ERROR",
                "platform": platform,
                "summary": "이미지 데이터 없음",
                "critical_issues": [],
                "error": "empty_image",
            }

        # platform 정보를 프롬프트에 추가
        prompt = MOBILE_AUDIT_PROMPT.replace('"platform": "android"', f'"platform": "{platform}"')

        raw_text = ""
        provider_used = ""
        try:
            raw_text = await _call_gemini_vision(image_b64, prompt, "")
            provider_used = "gemini-2.5-flash"
        except Exception as e:
            logger.warning("mobile_audit_gemini_failed", error=str(e))
            try:
                raw_text = await _call_claude_vision(image_b64, prompt, "")
                provider_used = "claude-sonnet-4-5"
            except Exception as e2:
                logger.error("mobile_audit_all_llm_failed", error=str(e2))
                return {
                    "image_ref": image_ref,
                    "scores": {},
                    "total_score": 0,
                    "verdict": "ERROR",
                    "platform": platform,
                    "summary": f"LLM 호출 실패: {e2}",
                    "critical_issues": [],
                    "error": str(e2),
                }

        try:
            data = _extract_json(raw_text)
            scores_raw = data.get("scores", {})
            scores = {}
            for key in MOBILE_AUDIT_CATEGORIES:
                raw = scores_raw.get(key, {})
                scores[key] = {
                    "score": int(raw.get("score", 0)) if isinstance(raw, dict) else int(raw),
                    "issues": raw.get("issues", []) if isinstance(raw, dict) else [],
                    "fixes": raw.get("fixes", []) if isinstance(raw, dict) else [],
                }
            total_score = int(data.get("total_score", sum(v["score"] for v in scores.values())))
            verdict = data.get("verdict", _calc_mobile_verdict(total_score))
            summary = data.get("summary", "")
            critical_issues = data.get("critical_issues", [])
        except Exception as e:
            logger.error("mobile_audit_json_parse_error", raw=raw_text[:300], error=str(e))
            return {
                "image_ref": image_ref,
                "scores": {},
                "total_score": 0,
                "verdict": "ERROR",
                "platform": platform,
                "summary": f"JSON 파싱 실패: {e}",
                "critical_issues": [],
                "error": str(e),
            }

        logger.info(
            "mobile_audit_done",
            image_ref=image_ref,
            platform=platform,
            total_score=total_score,
            verdict=verdict,
            provider=provider_used,
        )

        return {
            "image_ref": image_ref,
            "scores": scores,
            "total_score": total_score,
            "verdict": verdict,
            "platform": platform,
            "summary": summary,
            "critical_issues": critical_issues,
            "error": None,
            "provider_used": provider_used,
        }

    async def audit_product_images_batch(
        self,
        images: List[str],
        is_base64: bool = False,
    ) -> List[dict]:
        """여러 상품 이미지 일괄 심사."""
        logger.info("image_audit_batch_start", count=len(images))
        tasks = [self.audit_product_image(img, is_base64=is_base64) for img in images]
        results = await asyncio.gather(*tasks, return_exceptions=False)
        logger.info("image_audit_batch_done", count=len(results))
        return list(results)

    async def generate_report(self, audit_results: List[AuditResult]) -> str:
        """마크다운 형식 종합 감리 보고서 생성."""
        now = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
        lines = [
            "# AADS UI/UX 디자인 감리 보고서",
            f"생성일시: {now}",
            f"검수 페이지 수: {len(audit_results)}",
            "",
        ]

        for idx, r in enumerate(audit_results, 1):
            page_name = Path(r.screenshot_path).stem if r.screenshot_path else f"page_{idx}"
            verdict_emoji = {"PASS": "✅", "CONDITIONAL": "⚠️", "FAIL": "❌", "ERROR": "💥"}.get(r.verdict, "❓")

            lines += [
                f"## {idx}. {page_name}  {verdict_emoji} {r.verdict}",
                f"**총점**: {r.total_score}/50  |  **판정**: {r.verdict}",
                f"**요약**: {r.summary}",
                "",
                "### 항목별 점수",
                "| 항목 | 점수 | 이슈 수 |",
                "|------|------|---------|",
            ]

            category_labels = {
                "visual_consistency": "시각 일관성",
                "accessibility": "접근성",
                "interaction_clarity": "인터랙션 명확성",
                "brand_coherence": "브랜드 일관성",
                "polish": "완성도",
            }
            for key, label in category_labels.items():
                cs = r.scores.get(key)
                if cs:
                    lines.append(f"| {label} | {cs.score}/10 | {len(cs.issues)} |")

            if r.critical_issues:
                lines += ["", "### 즉시 수정 필요"]
                for issue in r.critical_issues:
                    lines.append(f"- {issue}")

            # 카테고리별 이슈/수정안
            for key, label in category_labels.items():
                cs = r.scores.get(key)
                if cs and cs.issues:
                    lines += ["", f"#### {label} 이슈 & 수정안"]
                    for issue, fix in zip(cs.issues, cs.fixes or []):
                        lines.append(f"- **이슈**: {issue}")
                        if fix:
                            lines.append(f"  - **수정**: {fix}")

            if r.error:
                lines += ["", f"> **오류**: {r.error}"]

            lines.append("")

        # 종합 통계
        valid = [r for r in audit_results if r.verdict not in ("ERROR",)]
        if valid:
            avg_score = sum(r.total_score for r in valid) / len(valid)
            pass_count = sum(1 for r in valid if r.verdict == "PASS")
            cond_count = sum(1 for r in valid if r.verdict == "CONDITIONAL")
            fail_count = sum(1 for r in valid if r.verdict == "FAIL")
            lines += [
                "---",
                "## 종합 통계",
                f"- **평균 점수**: {avg_score:.1f}/50",
                f"- **PASS**: {pass_count}  |  **CONDITIONAL**: {cond_count}  |  **FAIL**: {fail_count}",
                "",
            ]

        return "\n".join(lines)

    # -----------------------------------------------------------------------
    # Internal: experience_memory 저장
    # -----------------------------------------------------------------------

    async def _save_to_memory(
        self,
        result: AuditResult,
        project_context: str,
        provider_used: str,
    ) -> None:
        """감리 결과를 experience_memory에 저장 (graceful degradation)."""
        try:
            from app.memory.store import memory_store

            content = {
                "title": f"design_audit:{Path(result.screenshot_path).stem}",
                "screenshot_path": result.screenshot_path,
                "total_score": result.total_score,
                "verdict": result.verdict,
                "summary": result.summary,
                "critical_issues": result.critical_issues,
                "scores": {
                    k: {"score": v.score, "issues": v.issues, "fixes": v.fixes}
                    for k, v in result.scores.items()
                },
                "provider_used": provider_used,
                "project_context": project_context[:500] if project_context else "",
                "audited_at": result.audited_at,
            }
            await memory_store.store_experience(
                experience_type="design_audit",
                domain="ui_ux",
                tags=["design_audit", result.verdict.lower(), "visual_qa"],
                content=content,
            )
            logger.info("design_audit_memory_saved", verdict=result.verdict)
        except Exception as e:
            logger.warning("design_audit_memory_save_failed", error=str(e))


# 싱글톤 인스턴스
design_auditor = DesignAuditor()
