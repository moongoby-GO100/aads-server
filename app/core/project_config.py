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

import os
from typing import Any, Dict

# ─── 프로젝트별 서버·경로·언어 매핑 ──────────────────────────────────────────
# server: SSH 접속 IP (AADS는 localhost)
# workdir: 프로젝트 루트 디렉터리
# lang: 주 프로그래밍 언어
PROJECT_MAP: Dict[str, Dict[str, Any]] = {
    "AADS":  {"server": "host.docker.internal", "server_name": "contabo116", "workdir": "/root/aads/aads-server", "lang": "python", "display_name": "AADS 자율개발시스템", "aliases": ["AADS", "aads"]},
    "KIS":   {"server": "5.104.86.14", "server_name": "contabo14", "workdir": "/root/kis-autotrade-v4", "lang": "python", "display_name": "KIS 자동매매", "aliases": ["KIS", "kis", "자동매매", "kis-autotrade"]},
    "GO100": {"server": "5.104.86.14", "server_name": "contabo14", "workdir": "/root/kis-autotrade-v4", "lang": "python", "display_name": "백억이 투자분석", "aliases": ["GO100", "go100", "백억이", "백억이투자분석"]},
    "SF":    {"server": "114.207.244.86", "server_name": "cafe24_114", "port": "7916", "workdir": "/",                     "lang": "python", "display_name": "ShortFlow 숏폼자동화", "aliases": ["SF", "sf", "ShortFlow", "shortflow", "숏폼"]},
    "NTV2":  {"server": "114.207.244.86", "server_name": "cafe24_114", "port": "7916", "workdir": "/srv/newtalk-v2", "lang": "php", "workdir_v2": "/srv/newtalk-v2", "display_name": "NewTalk V2", "aliases": ["NTV2", "ntv2", "NewTalk", "newtalk", "NEWTALK", "newtalk-v2"]},
}

# DB/화면에서만 식별 가능한 프로젝트. SSH 서버·workdir을 부여하지 않는다.
# 이 목록에 들어간 값은 프로젝트 라벨로는 유효하지만 실행 대상은 아니다.
DISPLAY_ONLY_PROJECTS = frozenset({
    "FOOD", "NAS", "CEO", "WORK", "LAW", "DESIGN", "KAKAOBOT", "COM",
    "TEST", "QA", "PLAY", "DKSEON", "KNW001", "VIBE", "HARNESS",
    # 2026-09-14 추가. DB 에 워크스페이스가 있는데 이 목록에 없어서
    # 프로젝트 이름으로 인정받지 못하던 것들이다. ACCT 는 같은 날에도
    # 대화가 오갔고(회계프로그램 개발) 러너 쪽에서도 별도로 등록됐다.
    # 여기에 있다고 실행 권한이 생기지는 않는다 — 이름표일 뿐이다.
    "ACCT", "TERM", "FOOD1", "CTO",
})

ALL_PROJECTS = list(PROJECT_MAP.keys())

# 도구 스키마용 프로젝트 enum의 단일 소스.
SSH_PROJECT_ENUM = ALL_PROJECTS
SEARCH_PROJECT_ENUM = ALL_PROJECTS + ["NAS"]

# 외부 프로젝트만 (SSH 접근 대상)
REMOTE_PROJECTS = [k for k, v in PROJECT_MAP.items() if v["server"] not in ("localhost", "host.docker.internal")]


# ─── CEO 통합지시 검색 범위 ──────────────────────────────────────────────────
#
# `[CEO] 통합지시` 워크스페이스에서 물으면 **이 목록의 프로젝트를 가로질러**
# 기억과 대화를 찾는다. 다른 워크스페이스는 자기 프로젝트 안만 본다.
#
# 이 목록은 2026-09-07 에 만들어진 뒤 **네 곳에 복사됐고 이미 서로 갈라졌다.**
#
#     auto_rag.py            7개  (… NAS, CEO)
#     memory_recall.py       7개  (같음)
#     autonomous_executor.py 6개  (CEO 없음)
#     subagent_service.py    5개  (NAS·CEO 없음)
#
# 사본을 만들면 한쪽이 반드시 낡는다. 여기가 정본이다.
#
# **위의 `PROJECT_MAP` 과 성격이 다르다.** PROJECT_MAP 은 어느 서버에 SSH 로
# 붙을지를 정하므로 하드코딩이 안전장치다(LLM 이 명령을 다른 서버로 돌릴 수
# 없다). 이 목록은 **검색 범위만** 정한다 — 실행 대상을 바꾸지 못하므로
# 환경변수로 열어도 같은 위험이 없다.
#
#     CEO_ORCHESTRATOR_PROJECTS=AADS,KIS,GO100,SF,NTV2,NAS,CEO
#
# 모르는 이름이 들어오면 그것만 버리고 나머지를 쓴다. 전부 걸러져 비면
# 기본값으로 돌아간다 — 오타 하나로 통합 검색이 조용히 죽으면 안 된다.
_DEFAULT_ORCHESTRATOR_PROJECTS = ["AADS", "KIS", "GO100", "SF", "NTV2", "NAS", "CEO"]


def _parse_orchestrator_projects() -> list[str]:
    raw = os.getenv("CEO_ORCHESTRATOR_PROJECTS", "").strip()
    if not raw:
        return list(_DEFAULT_ORCHESTRATOR_PROJECTS)

    known = set(PROJECT_MAP) | set(DISPLAY_ONLY_PROJECTS)
    picked: list[str] = []
    dropped: list[str] = []
    for token in raw.replace(";", ",").split(","):
        name = token.strip().upper()
        if not name:
            continue
        if name not in known:
            dropped.append(name)
            continue
        if name not in picked:
            picked.append(name)

    if dropped:
        # 여기서 print 를 쓰는 이유: 이 모듈은 로거보다 먼저 import 된다.
        print(
            f"[project_config] CEO_ORCHESTRATOR_PROJECTS 에서 모르는 프로젝트를 "
            f"버렸다: {', '.join(dropped)}",
            flush=True,
        )
    if not picked:
        print(
            "[project_config] CEO_ORCHESTRATOR_PROJECTS 가 전부 걸러져 "
            "기본값을 쓴다",
            flush=True,
        )
        return list(_DEFAULT_ORCHESTRATOR_PROJECTS)
    return picked


ORCHESTRATOR_PROJECTS = _parse_orchestrator_projects()


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
