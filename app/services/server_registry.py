"""
AADS-181: 서버 레지스트리 (Single Source of Truth)
서버별 접근 정보 및 프로젝트 매핑 정의.

2026-08-19 서버 인프라 재편 (CEO 확정 명칭):
  contabo116 = 5.104.86.116   (AADS 본체)   ← 구 "68" / 68.183.183.11 폐기
  contabo14  = 5.104.86.14    (GO100 + KIS) ← 구 "211" / 211.188.51.113 폐기
  cafe24_114 = 114.207.244.86 (SF/NTV2/NAS) ← 구 "114" / 116.120.58.155 구 IP

하위호환: 구 서버 ID("68","211","114")는 SERVER_ID_ALIAS 및
SERVER_REGISTRY 내 동일 객체 참조로 계속 동작한다.
전체 서버 순회는 반드시 CANONICAL_SERVER_IDS 를 사용할 것
(SERVER_REGISTRY 직접 순회 시 별칭 때문에 중복 집계됨).
"""
from typing import Dict, List, Any

# ─── 서버 정의 (정규 명칭) ────────────────────────────────────────────────
_CONTABO116: Dict[str, Any] = {
    "id": "contabo116",
    "host": "5.104.86.116",
    "ssh_alias": "server-116",
    "ssh_port": 22,
    "type": "local",                    # AADS 본체 = 로컬 직접 접근
    "provider": "Contabo",
    "projects": ["AADS"],
    "directive_base": "/root/.genspark/directives",
    "http_health_urls": ["http://localhost:8100/api/v1/health"],
    "display_name": "contabo116 (AADS 본체)",
    "legacy_ids": ["68"],
}

_CONTABO14: Dict[str, Any] = {
    "id": "contabo14",
    "host": "5.104.86.14",
    "ssh_alias": "contabo14",
    "ssh_port": 22,
    "type": "ssh",
    "provider": "Contabo",
    "projects": ["GO100", "KIS"],
    "directive_base": "/root/.genspark/directives",
    "http_health_urls": [
        "https://go100.newtalk.kr/health",
        "http://5.104.86.14:8002/health",
    ],
    "display_name": "contabo14 (GO100/KIS)",
    "legacy_ids": ["211"],
}

_CAFE24_114: Dict[str, Any] = {
    "id": "cafe24_114",
    "host": "114.207.244.86",
    "ssh_alias": "server-114",
    "ssh_port": 7916,
    "type": "ssh",
    "provider": "Cafe24",
    "projects": ["SF", "NTV2", "NAS"],
    "directive_base": "/root/.genspark/directives",
    "http_health_urls": [
        "https://v2.newtalk.kr/",
        "http://114.207.244.86:7916/api/health",
    ],
    "display_name": "cafe24_114 (SF/NTV2/NAS)",
    "legacy_ids": ["114"],
}

# 정규 서버 ID 목록 — 순회/집계는 항상 이 리스트 기준
CANONICAL_SERVER_IDS: List[str] = ["contabo116", "contabo14", "cafe24_114"]

# 구 ID → 신 ID 별칭
SERVER_ID_ALIAS: Dict[str, str] = {
    "68": "contabo116",
    "server68": "contabo116",
    "116": "contabo116",
    "211": "contabo14",
    "server211": "contabo14",
    "14": "contabo14",
    "114": "cafe24_114",
    "server114": "cafe24_114",
}

# 서버 레지스트리 (정규 키 + 하위호환 별칭 키가 동일 객체를 참조)
SERVER_REGISTRY: Dict[str, Dict[str, Any]] = {
    "contabo116": _CONTABO116,
    "contabo14": _CONTABO14,
    "cafe24_114": _CAFE24_114,
    # ── 하위호환 별칭 (기존 코드 보호용, 신규 사용 금지) ──
    "68": _CONTABO116,
    "211": _CONTABO14,
    "114": _CAFE24_114,
}

# 프로젝트 → 서버 매핑
PROJECT_TO_SERVER: Dict[str, str] = {
    "AADS": "contabo116",
    "KIS": "contabo14",
    "GO100": "contabo14",
    "SF": "cafe24_114",
    "NTV2": "cafe24_114",
    "NAS": "cafe24_114",
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


def resolve_server_id(server_id: str) -> str:
    """구 서버 ID/별칭 → 정규 서버 ID 반환."""
    s = (server_id or "").strip()
    if s in SERVER_REGISTRY and s in CANONICAL_SERVER_IDS:
        return s
    return SERVER_ID_ALIAS.get(s, SERVER_ID_ALIAS.get(s.lower(), s))


def get_server_for_project(project: str) -> str:
    """프로젝트명으로 담당 서버 ID 반환 (정규 명칭)."""
    p = normalize_project(project)
    return PROJECT_TO_SERVER.get(p, "contabo116")


def get_server_config(server_id: str) -> Dict[str, Any]:
    """서버 ID(구/신 모두 허용) → 서버 설정 반환."""
    return SERVER_REGISTRY.get(resolve_server_id(server_id), {})


def get_server_host(server_id: str) -> str:
    """서버 ID(구/신 모두 허용) → 접속 호스트 IP 반환."""
    return get_server_config(server_id).get("host", "")


def get_servers_for_projects(projects: List[str]) -> List[str]:
    """프로젝트 목록에 해당하는 서버 ID 목록 반환 (중복 제거)."""
    servers = set()
    for p in projects:
        servers.add(get_server_for_project(p))
    return sorted(servers)


def list_servers() -> List[Dict[str, Any]]:
    """정규 서버 목록 반환 (대시보드/헬스체크 공용)."""
    return [SERVER_REGISTRY[sid] for sid in CANONICAL_SERVER_IDS]
