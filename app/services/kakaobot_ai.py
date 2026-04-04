"""KakaoBot AI 문구 생성 엔진 — call_llm_with_fallback 사용 (R-AUTH 준수)."""
from __future__ import annotations

import logging
from typing import List, Optional

from app.core.anthropic_client import call_llm_with_fallback

logger = logging.getLogger(__name__)


async def generate_messages(
    occasion: str,
    recipient_name: str,
    relationship: str = "",
    tone: str = "friendly",
    extra_context: str = "",
    count: int = 3,
) -> List[str]:
    """기념일/상황에 맞는 메시지 후보 생성.

    Args:
        occasion: 기념일/상황 (예: 생일, 결혼기념일, 추석)
        recipient_name: 수신자 이름
        relationship: 관계 (예: 어머니, 친구, 거래처)
        tone: 톤앤매너 (friendly, formal, casual, witty)
        extra_context: 추가 맥락
        count: 생성할 후보 수

    Returns:
        메시지 후보 리스트
    """
    tone_map = {
        "friendly": "친근하고 따뜻하게",
        "formal": "정중하고 격식있게",
        "casual": "편하고 자연스럽게",
        "witty": "재치있고 유머러스하게",
    }
    tone_desc = tone_map.get(tone, tone_map["friendly"])

    system_prompt = (
        "너는 한국어 문구 작성 전문가야.\n"
        "카카오톡이나 SMS로 보낼 축하/인사/감사 메시지를 작성해.\n"
        "- 자연스러운 한국어 사용\n"
        "- AI가 쓴 티 나지 않게\n"
        "- 각 메시지는 200자 이내\n"
        "- 이모티콘/이모지 적절히 포함\n"
        f"- 톤앤매너: {tone_desc}"
    )

    rel_part = f" (관계: {relationship})" if relationship else ""
    ctx_part = f"\n추가 맥락: {extra_context}" if extra_context else ""

    user_prompt = (
        f"{recipient_name}님{rel_part}에게 보낼 '{occasion}' 메시지 {count}개를 작성해줘.\n"
        f"각 메시지를 번호(1. 2. 3.)로 구분해서 작성해.{ctx_part}"
    )

    try:
        result = await call_llm_with_fallback(
            prompt=user_prompt,
            model="qwen-turbo",
            max_tokens=800,
            system=system_prompt,
        )
        if result is None:
            return []
        return _parse_numbered_messages(result, count)
    except Exception as e:
        logger.error("kakaobot_ai generate 실패: %s", e)
        return []


async def improve_message(
    original: str,
    instruction: str = "",
    tone: str = "friendly",
) -> str:
    """기존 메시지를 개선/수정.

    Args:
        original: 원본 메시지
        instruction: 수정 지시 (예: "더 격식있게", "이모지 추가")
        tone: 톤앤매너

    Returns:
        개선된 메시지
    """
    tone_map = {
        "friendly": "친근하고 따뜻하게",
        "formal": "정중하고 격식있게",
        "casual": "편하고 자연스럽게",
        "witty": "재치있고 유머러스하게",
    }
    tone_desc = tone_map.get(tone, tone_map["friendly"])

    system_prompt = (
        "너는 한국어 문구 수정 전문가야.\n"
        "카카오톡/SMS 메시지를 더 좋게 다듬어.\n"
        "- 200자 이내\n"
        "- AI가 쓴 티 나지 않게\n"
        f"- 톤앤매너: {tone_desc}\n"
        "- 개선된 메시지만 출력 (설명 없이)"
    )

    instr_part = f"\n수정 지시: {instruction}" if instruction else ""
    user_prompt = f"다음 메시지를 더 좋게 다듬어줘:{instr_part}\n\n원본: {original}"

    try:
        result = await call_llm_with_fallback(
            prompt=user_prompt,
            model="qwen-turbo",
            max_tokens=400,
            system=system_prompt,
        )
        return result or original
    except Exception as e:
        logger.error("kakaobot_ai improve 실패: %s", e)
        return original


def _parse_numbered_messages(text: str, expected: int) -> List[str]:
    """번호 매긴 메시지 텍스트를 파싱."""
    lines = text.strip().split("\n")
    messages = []
    current = []

    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        # 번호 시작 감지 (1. 2. 3. 등)
        if len(stripped) > 2 and stripped[0].isdigit() and stripped[1] in ".)" and current:
            messages.append(" ".join(current))
            current = [stripped[2:].strip()]
        elif len(stripped) > 3 and stripped[:2].isdigit() and stripped[2] in ".)" and current:
            messages.append(" ".join(current))
            current = [stripped[3:].strip()]
        else:
            if not current and stripped[0].isdigit() and len(stripped) > 2 and stripped[1] in ".)":
                current = [stripped[2:].strip()]
            elif not current and len(stripped) > 3 and stripped[:2].isdigit() and stripped[2] in ".)":
                current = [stripped[3:].strip()]
            else:
                current.append(stripped)

    if current:
        messages.append(" ".join(current))

    return messages[:expected] if messages else [text.strip()]
