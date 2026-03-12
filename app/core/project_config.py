"""
프로젝트별 서버/경로 중앙 설정 (Single Source of Truth).

모든 프로젝트 매핑은 이 파일에서만 관리.
다른 모듈은 여기서 import해서 사용.

보안: 하드코딩 — LLM 우회 불가.
"""
from __future__ import annotations

from typing import Dict

# ─── 프로젝트별 서버·경로·언어 매핑 ──────────────────────────────────────────
# server: SSH 접속 IP (AADS는 localhost)
# workdir: 프로젝트 루트 디렉터리
# lang: 주 프로그래밍 언어
PROJECT_MAP: Dict[str, Dict[str, str]] = {
    "KIS":   {"server": "211.188.51.113", "workdir": "/root/webapp",          "lang": "python"},
    "GO100": {"server": "211.188.51.113", "workdir": "/root/go100",           "lang": "python"},
    "SF":    {"server": "114.207.244.86", "port": "7916", "workdir": "/data/shortflow",       "lang": "python"},
    "NTV2":  {"server": "114.207.244.86", "port": "7916", "workdir": "/home/danharoo/www", "lang": "php", "workdir_v2": "/srv/newtalk-v2"},
    "AADS":  {"server": "host.docker.internal", "workdir": "/root/aads/aads-server", "lang": "python"},
}

ALL_PROJECTS = list(PROJECT_MAP.keys())

# 외부 프로젝트만 (SSH 접근 대상)
REMOTE_PROJECTS = [k for k, v in PROJECT_MAP.items() if v["server"] not in ("localhost", "host.docker.internal")]


def get_workdir(project: str) -> str:
    """프로젝트명 → workdir 반환. 없으면 빈 문자열."""
    return PROJECT_MAP.get(project, {}).get("workdir", "")


def get_server(project: str) -> str:
    """프로젝트명 → 서버 IP 반환. 없으면 빈 문자열."""
    return PROJECT_MAP.get(project, {}).get("server", "")


def get_server_by_number(server_num: str) -> dict:
    """서버 번호(211, 114, 68) → {server, workdir} 매핑."""
    _SERVER_NUM_MAP = {
        "68": {"server": "host.docker.internal", "workdir": "/root/aads/aads-server"},
        "211": {"server": "211.188.51.113", "workdir": "/root/webapp"},
        "114": {"server": "114.207.244.86", "port": "7916", "workdir": "/data/shortflow"},
    }
    return _SERVER_NUM_MAP.get(server_num, {"server": "", "workdir": "/root"})
