"""
멀티모달 메모리 저장 모듈 — 이미지 분석 결과를 ai_observations 테이블에 축적.

CEO가 공유한 스크린샷, UI 캡처, 차트 이미지 등을 분석하여
category='visual_memory'로 저장. 실제 이미지 바이너리는 저장하지 않으며
분석 텍스트만 메모리화한다.

DB 스키마 변경 없이 ai_observations 기존 컬럼 활용:
  - category: 'visual_memory'
  - key: 이미지 식별자 (URL SHA-256 앞 16자리)
  - value: 이미지 분석 결과 텍스트
  - project: 프로젝트 필터
  - confidence: 기본 0.7
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

_VISUAL_CONFIDENCE = 0.7


def _get_pool():
    """DB 커넥션 풀 반환."""
    from app.core.db_pool import get_pool
    return get_pool()


def _make_image_key(image_url: str) -> str:
    """이미지 URL을 SHA-256 해시 앞 16자리로 단축하여 key 생성."""
    return "img_" + hashlib.sha256(image_url.encode()).hexdigest()[:16]


async def store_visual_memory(
    image_url: str,
    analysis_text: str,
    project: str,
    category: str = "visual_memory",
) -> bool:
    """이미지 분석 결과를 ai_observations 테이블에 저장.

    Args:
        image_url: 원본 이미지 URL 또는 식별자 (메타데이터용, 바이너리 저장 안 함).
        analysis_text: LLM이 생성한 이미지 설명 텍스트.
        project: 프로젝트명 (AADS/KIS/GO100/SF/NTV2/NAS). 대문자 정규화됨.
        category: ai_observations 카테고리. 기본 'visual_memory'.

    Returns:
        True = 저장 성공, False = 실패.
    """
    if not analysis_text or not analysis_text.strip():
        logger.warning("store_visual_memory: analysis_text 비어있어 저장 스킵")
        return False

    _project = project.upper().strip() if project else None
    _key = _make_image_key(image_url)

    try:
        async with _get_pool().acquire() as conn:
            await conn.execute(
                """
                INSERT INTO ai_observations
                    (category, key, value, confidence, project, updated_at)
                VALUES ($1, $2, $3, $4, $5, NOW())
                ON CONFLICT (category, key, COALESCE(project, ''))
                DO UPDATE SET
                    value      = EXCLUDED.value,
                    confidence = GREATEST(EXCLUDED.confidence, ai_observations.confidence),
                    updated_at = NOW()
                """,
                category, _key, analysis_text, _VISUAL_CONFIDENCE, _project,
            )
        logger.info(
            "store_visual_memory: 저장 완료 key=%s project=%s chars=%d",
            _key, _project, len(analysis_text),
        )
        return True

    except Exception as e:
        logger.warning(
            "store_visual_memory: 저장 실패 key=%s project=%s error=%s",
            _key, _project, e,
        )
        return False
