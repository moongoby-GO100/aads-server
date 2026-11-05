"""Claude 구독 슬롯의 단일 정의.

슬롯 번호를 코드 여러 곳에 흩어 두면 새 슬롯을 들일 때마다 한 곳씩 빠진다.
슬롯 3·4 때 이름 매핑이 빠져 레코드가 slot="" 로 떨어졌고, 빈 슬롯은 주소 지정이
불가능해 폴백에서 조용히 사라졌다(두 번). 슬롯을 늘릴 때는 이 파일만 고친다.
표준 라이브러리만 쓴다 — 호스트 릴레이/스크립트도 가져다 쓴다.
"""
from __future__ import annotations

from typing import Dict, Optional

# 슬롯 번호 → llm_api_keys(provider=anthropic).key_name
SLOT_KEY_NAMES: Dict[str, str] = {
    "1": "ANTHROPIC_AUTH_TOKEN",
    "2": "ANTHROPIC_AUTH_TOKEN_2",
    "3": "ANTHROPIC_AUTH_TOKEN_3",
    "4": "ANTHROPIC_AUTH_TOKEN_4",
    "5": "ANTHROPIC_AUTH_TOKEN_5",
}

CLAUDE_SLOTS = tuple(SLOT_KEY_NAMES)

# 로그인 성공 뒤 "이 계정이 맞는가" 를 엄격히 확인하는 슬롯. 신원이 다르면 활성화하지 않는다.
SLOT_EXPECTED_ACCOUNTS: Dict[str, str] = {
    "4": "thelylon14@gmail.com",
    "5": "obys.aads@gmail.com",
}
STRICT_LOGIN_SLOTS = frozenset(SLOT_EXPECTED_ACCOUNTS)

# DB 행이 로그인 전에는 없을 수 있는 슬롯(키퍼가 로그인 검증 뒤 비활성 행으로 만든다).
# 라벨은 _binding_account_mismatch 가 이메일 전체로 비교하므로 계정 이메일이어야 한다.
SLOT_REGISTER_LABELS: Dict[str, str] = {"5": SLOT_EXPECTED_ACCOUNTS["5"]}

KEY_NAME_TO_SLOT: Dict[str, str] = {name: slot for slot, name in SLOT_KEY_NAMES.items()}


def key_name_for_slot(slot: str) -> Optional[str]:
    return SLOT_KEY_NAMES.get(str(slot))


def is_known_slot(slot: str) -> bool:
    return str(slot) in SLOT_KEY_NAMES
