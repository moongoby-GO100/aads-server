"""
AADS-181: 서버 레지스트리
서버 접근 정보 및 프로젝트 매핑 정의.
"""
from typing import Dict, List, Any

# 서버별 설정
SERVER_REGISTRY: Dict[str, Dict[str, Any]] = {
    "116": {
        "host": "5.104.86.116",
        "ipv6": "2400:d320:2326:7555::1",
        "type": "local",
        "projects": ["AADS"],
        "directive_base": "/root/.genspark/directives",
        "http_health_urls": ["https://aads.newtalk.kr/api/v1/health"],
        "display_name": "server-116 Contabo (AADS)",
        "location": "Contabo",
        "os": "Ubuntu 24.04",
    },
    "68": {
        "host": "68.183.183.11",
        "type": "ssh",
        "projects": [],
        "directive_base": "/root/.genspark/directives",
        "http_health_urls": [],
        "display_name": "서버 68 legacy rollback only",
        "status_note": "AADS production moved to server-116 / 5.104.86.116 on 2026-06-23 KST.",
    },
    "211": {
        "host": "211.188.51.113",
        "type": "ssh",
        "projects": ["KIS"],
        "directive_base": "/root/.genspark/directives",
        "http_health_urls": [
            "http://211.188.51.113:8200/health",
            "http://211.188.51.113:8100/api/v1/health",
        ],
        "display_name": "서버 211 (legacy/KIS)",
        "status_note": "GO100 migrated to contabo14 on 2026-06-19 KST; 211 is pending decommission for GO100.",
    },
    "contabo14": {
        "host": "5.104.86.14",
        "ipv6": "2400:d320:2338:1565::1",
        "type": "ssh",
        "projects": ["GO100"],
        "directive_base": "/root/.genspark/directives",
        "http_health_urls": [
            "https://go100.newtalk.kr/api/go100/health",
            "https://go100.newtalk.kr/go100/command-center",
        ],
        "display_name": "server-14 Contabo Tokyo (GO100)",
        "location": "Tokyo",
        "os": "Ubuntu 24.04",
    },
    "114": {
        "host": "114.207.244.86",
        "type": "ssh",
        "projects": ["SF", "NTV2", "NAS"],
        "directive_base": "/root/.genspark/directives",
        "http_health_urls": [
            "http://114.207.244.86:7916/api/health",
            "http://114.207.244.86:7916/health",
        ],
        "display_name": "server-114 Cafe24 (NewTalk/SF/NAS)",
    },
}

# 프로젝트 → 서버 매핑
PROJECT_TO_SERVER: Dict[str, str] = {
    "AADS": "116",
    "KIS": "211",
    "GO100": "contabo14",
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
    return PROJECT_TO_SERVER.get(p, "116")


def get_servers_for_projects(projects: List[str]) -> List[str]:
    """프로젝트 목록에 해당하는 서버 ID 목록 반환 (중복 제거)."""
    servers = set()
    for p in projects:
        servers.add(get_server_for_project(p))
    return sorted(servers)
