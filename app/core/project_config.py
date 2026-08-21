"""
프로젝트별 서버/경로 중앙 설정 (Single Source of Truth).

모든 프로젝트 매핑은 이 파일에서만 관리.
다른 모듈은 여기서 import해서 사용.

서버 명칭 (2026-08-19 확정):
  contabo116 = 5.104.86.116 (AADS 본체)
  contabo14  = 5.104.86.14  (GO100/KIS)
  cafe24_114 = 114.207.244.86 (SF/NTV2/NAS)

보안: 하드코딩 — LLM 우회 불가.
"""
from __future__ import annotations

from typing import Any, Dict

# ─── 프로젝트별 서버·경로·언어 매핑 ──────────────────────────────────────────
# server: SSH 접속 IP (AADS는 localhost)
# workdir: 프로젝트 루트 디렉터리
# lang: 주 프로그래밍 언어
PROJECT_MAP: Dict[str, Dict[str, Any]] = {
    "KIS":   {"server": "5.104.86.14", "server_name": "contabo14", "workdir": "/root/kis-autotrade-v4", "lang": "python", "display_name": "KIS 자동매매", "aliases": ["KIS", "kis", "자동매매", "kis-autotrade"]},
    "GO100": {"server": "5.104.86.14", "server_name": "contabo14", "workdir": "/root/kis-autotrade-v4", "lang": "python", "display_name": "백억이 투자분석", "aliases": ["GO100", "go100", "백억이", "백억이투자분석"]},
    "SF":    {"server": "114.207.244.86", "server_name": "cafe24_114", "port": "7916", "workdir": "/",                     "lang": "python", "display_name": "ShortFlow 숏폼자동화", "aliases": ["SF", "sf", "ShortFlow", "shortflow", "숏폼"]},
    "NTV2":  {"server": "114.207.244.86", "server_name": "cafe24_114", "port": "7916", "workdir": "/srv/newtalk-v2", "lang": "php", "workdir_v2": "/srv/newtalk-v2", "display_name": "NewTalk V2", "aliases": ["NTV2", "ntv2", "NewTalk", "newtalk", "NEWTALK", "newtalk-v2"]},
    "AADS":  {"server": "host.docker.internal", "server_name": "contabo116", "workdir": "/root/aads/aads-server", "lang": "python", "display_name": "AADS 자율개발시스템", "aliases": ["AADS", "aads"]},
}

# DB/화면에서만 식별 가능한 프로젝트. SSH 서버·workdir을 부여하지 않는다.
# 이 목록에 들어간 값은 프로젝트 라벨로는 유효하지만 실행 대상은 아니다.
DISPLAY_ONLY_PROJECTS = frozenset({
    "FOOD", "NAS", "CEO", "WORK", "LAW", "DESIGN", "KAKAOBOT", "COM",
    "TEST", "QA", "PLAY", "DKSEON", "KNW001", "VIBE", "HARNESS",
})

ALL_PROJECTS = list(PROJECT_MAP.keys())

# 외부 프로젝트만 (SSH 접근 대상)
REMOTE_PROJECTS = [k for k, v in PROJECT_MAP.items() if v["server"] not in ("localhost", "host.docker.internal")]


def _build_project_alias_index() -> Dict[str, str]:
    index: Dict[str, str] = {}
    for project, config in PROJECT_MAP.items():
        index[project.lower()] = project
        for alias in config.get("aliases", []):
            index[str(alias).lower()] = project
        display_name = config.get("display_name")
        if display_name:
            index[str(display_name).lower()] = project
    for project in DISPLAY_ONLY_PROJECTS:
        index[project.lower()] = project
    return index


_PROJECT_ALIAS_INDEX = _build_project_alias_index()


def _extract_bracket_token(value: str) -> str | None:
    if not value.startswith("["):
        return None
    end = value.find("]")
    if end <= 1:
        return None
    token = value[1:end].strip()
    return token or None


def get_display_name(project: str) -> str:
    """정규 키 → 표시명. 미등록이면 입력값 그대로 반환."""
    resolved = resolve_project(project) or project
    return PROJECT_MAP.get(resolved, {}).get("display_name", resolved)


def resolve_project(value: str | None) -> str | None:
    """임의 입력(별칭/표시명/워크스페이스명) → 정규 키. 실패 시 None.

    해석 순서:
      1) None/공백 → None
      2) 정규 키 완전일치(대문자 변환 후) → 그대로
      3) aliases 완전일치(대소문자 무시)
      4) display_name 완전일치(대소문자 무시)
      5) '[XXX] 표시명' 패턴이면 대괄호 안 토큰 추출 후 2~4 재시도
      6) 실패 → None
    """
    if value is None:
        return None

    normalized = value.strip()
    if not normalized:
        return None

    upper = normalized.upper()
    if upper in PROJECT_MAP or upper in DISPLAY_ONLY_PROJECTS:
        return upper

    resolved = _PROJECT_ALIAS_INDEX.get(normalized.lower())
    if resolved:
        return resolved

    bracket_token = _extract_bracket_token(normalized)
    if bracket_token:
        return resolve_project(bracket_token)

    return None


def is_executable_project(value: str | None) -> bool:
    """프로젝트가 실제 서버/작업 디렉터리를 가진 실행 대상인지 반환한다."""
    resolved = resolve_project(value)
    return bool(resolved and resolved in PROJECT_MAP)


def normalize_project_label(value: str | None) -> str | None:
    """DB 정규화용. resolve_project() 성공 시 정규 키.

    실패하고 '[XXX] ...' 패턴이면 대괄호 토큰을 대문자로 반환
    (예: '[FOOD] 열정국밥' → 'FOOD'). 그 외에는 원본을 strip()해서 반환.
    입력이 비면 None.
    """
    if value is None:
        return None

    normalized = value.strip()
    if not normalized:
        return None

    resolved = resolve_project(normalized)
    if resolved:
        return resolved

    bracket_token = _extract_bracket_token(normalized)
    if bracket_token:
        return bracket_token.upper()

    return normalized


def get_workdir(project: str) -> str:
    """프로젝트명 → workdir 반환. 없으면 빈 문자열."""
    return PROJECT_MAP.get(project, {}).get("workdir", "")


def get_server(project: str) -> str:
    """프로젝트명 → 서버 IP 반환. 없으면 빈 문자열."""
    return PROJECT_MAP.get(project, {}).get("server", "")


def get_server_by_number(server_num: str) -> dict:
    """서버 번호/명칭 → {server, workdir} 매핑."""
    _SERVER_NUM_MAP = {
        "contabo116": {"server": "host.docker.internal", "workdir": "/root/aads/aads-server"},
        "contabo14": {"server": "5.104.86.14", "workdir": "/root/kis-autotrade-v4"},
        "cafe24_114": {"server": "114.207.244.86", "port": "7916", "workdir": "/"},
        # 하위호환: 구 번호 → 신 명칭
        "68": {"server": "host.docker.internal", "workdir": "/root/aads/aads-server"},
        "211": {"server": "5.104.86.14", "workdir": "/root/kis-autotrade-v4"},
        "114": {"server": "114.207.244.86", "port": "7916", "workdir": "/"},
    }
    return _SERVER_NUM_MAP.get(server_num, {"server": "", "workdir": "/root"})
