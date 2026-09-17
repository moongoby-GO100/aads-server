"""주도 미지정 목표에 **링크를 붙여넣어** 담당을 세우는 경로 (2026-09-17).

대표님 지시 — "목표카드에 주도가 등록 안 되어 있으면 내가 직접 세션링크를
등록할 수 있게 해줘".

막혀 있던 곳은 두 군데였고, 여기서 그 둘을 검사한다.

  1. `resolve_session_ref` — 주소창을 그대로 붙여넣어도 세션 id 를 꺼낸다.
     UUID 만 받으면 대표님이 링크에서 id 를 손으로 오려내셔야 한다.
  2. 후보 목록의 워크스페이스 폴백 — 원안은 **이미 붙어 있는 세션**에서만
     워크스페이스를 알아냈다. 담당이 0명인 목표는 후보가 영원히 비어서
     "+ 주도 지정" 이 막다른 길이었다 (2026-09-17 실측, 활성 목표 11개 중 6개).

링크를 잘못 받으면 엉뚱한 창이 목표의 주도가 된다. 그래서 형식 검사는
느슨하게 하되, **못 찾으면 400 으로 멈춘다** — 조용히 빈 값을 쓰지 않는다.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.routers.goals import resolve_session_ref

SID = "5090a247-47f7-4a05-a965-da89f844ad2f"


@pytest.mark.parametrize(
    "ref",
    [
        SID,
        f"  {SID}  ",
        f"https://aads.newtalk.kr/chat#{SID}",
        f"https://aads.newtalk.kr/chat?session={SID}",
        f"https://aads.newtalk.kr/chat/{SID}",
        f"http://5.104.86.116:3000/chat#{SID}",
        f"/chat#{SID}?tab=goals",
    ],
)
def test_주소창을_그대로_붙여넣어도_세션id를_꺼낸다(ref: str) -> None:
    assert resolve_session_ref(ref) == SID


def test_대문자_링크도_같은_세션으로_읽는다() -> None:
    assert resolve_session_ref(f"/chat#{SID.upper()}") == SID


def test_워크스페이스와_세션이_함께_붙은_링크는_뒤쪽_세션을_쓴다() -> None:
    """`/chat?workspace=<ws>#<session>` 형태. 세션 id 가 뒤에 온다."""
    ws = "15b5a427-7f0a-4fdc-8e54-5d664cfa911a"
    assert resolve_session_ref(f"/chat?workspace={ws}#{SID}") == SID


@pytest.mark.parametrize("ref", ["", "   ", None])
def test_빈값은_400으로_멈춘다(ref) -> None:
    with pytest.raises(HTTPException) as err:
        resolve_session_ref(ref)
    assert err.value.status_code == 400


@pytest.mark.parametrize(
    "ref",
    [
        "https://aads.newtalk.kr/chat",
        "세션 링크 주세요",
        "1234-5678",
        "5090a247-47f7-4a05-a965",  # 잘린 UUID
    ],
)
def test_세션id가_없는_문자열은_400이고_조용히_통과하지_않는다(ref: str) -> None:
    with pytest.raises(HTTPException) as err:
        resolve_session_ref(ref)
    assert err.value.status_code == 400
    assert "세션 ID" in str(err.value.detail)
