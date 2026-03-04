"""
자동 보정 엔진 — T-027.

AutoCorrector:
  - analyze_failures(audit_result) → list[dict] (7점 미만 항목)
  - generate_correction_params(failures, current_params, benchmark_spec) → dict
  - create_correction_directive(project_id, video_id, corrections) → dict

심사 항목 → FFmpeg 파라미터 delta 적용 후 재렌더링 지시서 생성.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import Any, Dict, List, Optional

import structlog

logger = structlog.get_logger()


class AutoCorrector:
    """영상 QA 심사 결과 → 자동 보정 파라미터 생성."""

    # 심사 항목 → FFmpeg 파라미터 매핑
    CORRECTION_MAP = {
        "subtitle_readability": {
            "low_score_action": "increase_font_size_and_box_opacity",
            "params": {"fontsize_delta": +8, "box_alpha_delta": +0.2},
        },
        "background_quality": {
            "low_score_action": "switch_to_higher_res_stock",
            "params": {"min_resolution": "1080x1920"},
        },
        "composition": {
            "low_score_action": "adjust_margins",
            "params": {"margin_bottom_delta": +5},
        },
        "brand_consistency": {
            "low_score_action": "apply_color_grading",
            "params": {},
        },
        # 영상 QA 6항목 → 기존 웹 QA 5항목 + 영상 전용 1항목
        "visual_consistency": {
            "low_score_action": "increase_font_size_and_box_opacity",
            "params": {"fontsize_delta": +4, "box_alpha_delta": +0.1},
        },
        "accessibility": {
            "low_score_action": "increase_contrast",
            "params": {"fontsize_delta": +4, "fontcolor": "#ffffff"},
        },
        "interaction_clarity": {
            "low_score_action": "adjust_margins",
            "params": {"margin_bottom_delta": +3},
        },
        "brand_coherence": {
            "low_score_action": "apply_color_grading",
            "params": {},
        },
        "polish": {
            "low_score_action": "increase_font_size_and_box_opacity",
            "params": {"box_alpha_delta": +0.15},
        },
    }

    # 판정 기준 (영상 QA — 10점 × 6항목 = 60점 만점 기준)
    VIDEO_QA_PASS_THRESHOLD = 51       # 85% × 60 = 51
    VIDEO_QA_CONDITIONAL_THRESHOLD = 42  # 70% × 60 = 42
    # 항목별 실패 기준
    ITEM_FAIL_SCORE = 7

    async def analyze_failures(self, audit_result: dict) -> List[dict]:
        """
        심사 결과에서 7점 미만 항목 추출 → 보정 액션 목록.

        Args:
            audit_result: {
                "scores": {
                    "subtitle_readability": {"score": 5, ...},
                    ...
                },
                "total_score": 42,
                "verdict": "AUTO_REJECT"
            }

        Returns:
            [{"item": "subtitle_readability", "score": 5, "action": "increase_font_size_and_box_opacity", "params": {...}}, ...]
        """
        scores = audit_result.get("scores", {})
        failures = []

        for item_key, score_data in scores.items():
            score = score_data if isinstance(score_data, (int, float)) else score_data.get("score", 10)
            if score < self.ITEM_FAIL_SCORE:
                correction = self.CORRECTION_MAP.get(item_key, {
                    "low_score_action": "general_quality_boost",
                    "params": {"fontsize_delta": +4, "box_alpha_delta": +0.1},
                })
                failures.append({
                    "item": item_key,
                    "score": score,
                    "action": correction["low_score_action"],
                    "correction_params": correction["params"],
                })
                logger.info("failure_detected", item=item_key, score=score, action=correction["low_score_action"])

        logger.info("analyze_failures_done", failure_count=len(failures))
        return failures

    async def generate_correction_params(
        self,
        failures: List[dict],
        current_params: dict,
        benchmark_spec: dict,
    ) -> dict:
        """
        보정 파라미터 생성 (현재 + delta, 벤치마크 사양 참조).

        Args:
            failures: analyze_failures() 결과
            current_params: 현재 FFmpeg 파라미터 dict
            benchmark_spec: 벤치마크 사양 (spec_to_ffmpeg_params 결과)

        Returns:
            보정된 FFmpeg 파라미터 dict
        """
        corrected = dict(current_params)

        # 벤치마크 사양을 기준값으로 병합 (현재값 우선, 없으면 벤치마크)
        for key, val in benchmark_spec.items():
            if key not in corrected:
                corrected[key] = val

        for failure in failures:
            action = failure.get("action", "")
            params = failure.get("correction_params", {})

            if action == "increase_font_size_and_box_opacity":
                fontsize_delta = params.get("fontsize_delta", 8)
                box_alpha_delta = params.get("box_alpha_delta", 0.2)

                current_fontsize = corrected.get("fontsize", 48)
                corrected["fontsize"] = min(int(current_fontsize) + int(fontsize_delta), 72)  # 최대 72px

                current_alpha = corrected.get("box_alpha", 0.8)
                corrected["box_alpha"] = min(float(current_alpha) + float(box_alpha_delta), 1.0)

                logger.info("correction_applied_font_box", fontsize=corrected["fontsize"], box_alpha=corrected["box_alpha"])

            elif action == "switch_to_higher_res_stock":
                min_res = params.get("min_resolution", "1080x1920")
                corrected["resolution"] = min_res
                corrected["background_style"] = "stock_video"
                logger.info("correction_applied_resolution", resolution=min_res)

            elif action == "adjust_margins":
                margin_delta = params.get("margin_bottom_delta", 5)
                current_margin_raw = corrected.get("margin_bottom", "10%")
                try:
                    current_margin_val = int(str(current_margin_raw).replace("%", "").strip())
                    new_margin = max(5, current_margin_val + int(margin_delta))
                    corrected["margin_bottom"] = f"{new_margin}%"
                except (ValueError, TypeError):
                    corrected["margin_bottom"] = "15%"
                logger.info("correction_applied_margin", margin_bottom=corrected["margin_bottom"])

            elif action == "apply_color_grading":
                # 벤치마크 색상 팔레트 적용
                benchmark_palette = benchmark_spec.get("color_palette", [])
                if benchmark_palette:
                    corrected["color_palette"] = benchmark_palette
                corrected["color_grading"] = True
                logger.info("correction_applied_color_grading")

            elif action == "increase_contrast":
                corrected["fontcolor"] = params.get("fontcolor", "#ffffff")
                fontsize_delta = params.get("fontsize_delta", 4)
                current_fontsize = corrected.get("fontsize", 48)
                corrected["fontsize"] = min(int(current_fontsize) + int(fontsize_delta), 72)
                logger.info("correction_applied_contrast")

            elif action == "general_quality_boost":
                fontsize_delta = params.get("fontsize_delta", 4)
                box_alpha_delta = params.get("box_alpha_delta", 0.1)
                corrected["fontsize"] = min(corrected.get("fontsize", 48) + fontsize_delta, 72)
                corrected["box_alpha"] = min(corrected.get("box_alpha", 0.8) + box_alpha_delta, 1.0)
                logger.info("correction_applied_general_boost")

        logger.info("generate_correction_params_done", corrections=corrected)
        return corrected

    async def create_correction_directive(
        self,
        project_id: str,
        video_id: str,
        corrections: dict,
    ) -> dict:
        """
        ShortFlow에 전달할 재렌더링 지시서 생성.

        반환: {"action":"re-render", "video_id":"...", "params":{...}, "max_retries":2}
        """
        directive = {
            "action": "re-render",
            "project_id": project_id,
            "video_id": video_id,
            "params": corrections,
            "max_retries": 2,
            "created_at": datetime.utcnow().isoformat(),
            "source": "aads_auto_corrector",
        }
        logger.info(
            "correction_directive_created",
            project_id=project_id,
            video_id=video_id,
            action="re-render",
        )
        return directive


# 싱글톤 인스턴스
auto_corrector = AutoCorrector()
