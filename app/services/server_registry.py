"""
AADS-181: 서버 레지스트리
3대 서버(68/211/114) 접근 정보 및 프로젝트 매핑 정의.
"""
from typing import Dict, List, Any

# 서버별 설정
SERVER_REGISTRY: Dict[str, Dict[str, Any]] = {
    "68": {
        "host": "68.183.183.11",
        "type": "local",                    # 로컬 직접 접근
        "projects": ["AADS"],
        "directive_base": "/root/.genspark/directives",
        "http_health_urls": ["http://localhost:8100/api/v1/health"],
        "display_name": "서버 68 (AADS Backend)",
    },
    "211": {
        "host": "211.188.51.113",
        "type": "ssh",
        "projects": ["KIS", "GO100"],
        "directive_base": "/root/.genspark/directives",
        "http_health_urls": [
            "http://211.188.51.113:8200/health",
            "http://211.188.51.113:8100/api/v1/health",
        ],
        "display_name": "서버 211 (Hub/KIS/GO100)",
    },
    "114": {
        "host": "116.120.58.155",
        "type": "ssh",
        "projects": ["SF", "NTV2", "NAS"],
        "directive_base": "/root/.genspark/directives",
        "http_health_urls": [
            "http://116.120.58.155:7916/api/health",
            "http://116.120.58.155:7916/health",
        ],
        "display_name": "서버 114 (SF/NTV2/NAS)",
    },
}

# 프로젝트 → 서버 매핑
PROJECT_TO_SERVER: Dict[str, str] = {
    "AADS": "68",
    "KIS": "211",
    "GO100": "211",
    "SF": "114",
    "NTV2": "114",
    "NAS": "114",
}

# 프로젝트 별칭 정규화 (ShortFlow → SF, NewTalk → NTV2 등)
PROJECT_ALIAS: Dict[str, str] = {
    "SHORTFLOW": "SF",
    "NEWTALK": "NTV2",
    "NEWTALK_V2": "NTV2",
    "NT": "NTV2",
}

ALL_PROJECTS: List[str] = list(PROJECT_TO_SERVER.keys())
ALL_STATUSES = ["pending", "running", "done", "archived"]


def normalize_project(project: str) -> str:
    """프로젝트명 정규화 (대소문자, 별칭 처리)."""
    p = (project or "").strip().upper()
    return PROJECT_ALIAS.get(p, p)


def get_server_for_project(project: str) -> str:
    """프로젝트명으로 담당 서버 ID 반환."""
    p = normalize_project(project)
    return PROJECT_TO_SERVER.get(p, "68")


def get_servers_for_projects(projects: List[str]) -> List[str]:
    """프로젝트 목록에 해당하는 서버 ID 목록 반환 (중복 제거)."""
    servers = set()
    for p in projects:
        servers.add(get_server_for_project(p))
    return sorted(servers)
