"""목표 연결 프로젝트 정책 (순수 함수, DB 없음).

AADS 호스팅 러너 작업은 **명시 연결**(요청 인자 또는 지시서 GOAL_ID 줄)에 한해
아래 프로젝트의 목표에 붙을 수 있다. 방향성이 있다 — 역방향은 허용하지 않는다.
제출 경로·재작업·재조정·소급 연결이 모두 이 한 곳을 본다.
"""
from __future__ import annotations

GOAL_LINK_HOST_PROJECTS: dict[str, frozenset[str]] = {"AADS": frozenset({"ACCT", "FOOD"})}


def is_goal_project_allowed(job_project: str | None, goal_project: str | None) -> bool:
    if not job_project or not goal_project:
        return False
    return goal_project == job_project or goal_project in GOAL_LINK_HOST_PROJECTS.get(job_project, ())
