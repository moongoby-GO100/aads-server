#!/usr/bin/env python3
"""정본문서 목표(ac431290) 업무항목 90건 권장 역할 배정 도구.

- 기본(--stage plan)은 오프라인이다: 등록명세·등록결과 JSON 만 읽어 역할별 건수(PM 36/Developer 30/QA 24)와
  ID 집합 지문(md5)을 검증한다. 네트워크 없음.
- 쓰기 단계(preview 외 propose/decide/execute/rollback)는 공식 서버 API 만 쓴다. 판단은 전부 서버가 한다:
  승인 증거(결정·원 메시지), 대상 ID 집합, 보유 세션, actor 소유권을 서버가 DB 에서 다시 검증한다.
  이 스크립트의 인자·환경변수는 승인 증거가 아니다.
- 인증은 AADS_BEARER_TOKEN(사용자 JWT) + 명시적 AADS_ACTOR_SESSION_ID 뿐이다. 세션 기본값 없음.
  서비스 principal(AADS_MONITOR_KEY 등)이 설정돼 있으면 쓰기를 거부한다 — register_work_hierarchy.py 의 차단을 풀지 않는다.
- 승인 참조(--decision-ref/--approval-message-id)가 비어 있으면 서버 호출 전에 거부한다.
- 보유 세션이 없을 때의 "신규 생성" 권장 경로: create-sessions 단계가 공식 POST /chat/sessions 로 역할별 세션을 만든다.
  기존 보유 세션 재사용과 신규 생성은 서버 응답(holders[].mode = existing|new)에서 구분된다.
- 토큰·세션 ID 는 출력하지 않는다.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

HERE = Path(__file__).parent
GOAL_ID = "ac431290-66be-4d00-8bbb-9bf162917240"
ROLES = ("PM", "Developer", "QA")
EXPECTED = {"PM": 36, "Developer": 30, "QA": 24}
PRODUCTION_IDS_MD5 = "c523a6d044c5d0be31f3469161a66beb"
SPEC_PATH = HERE / "20261014_AADS_업무계층등록명세.json"
RESULT_PATH = HERE / "20261014_AADS_업무계층등록결과.json"
SERVICE_PRINCIPAL_ENV = ("AADS_USE_SERVICE_PRINCIPAL", "AADS_MONITOR_KEY", "AADS_SERVICE_PRINCIPAL_APPROVAL_REF")
ALLOWED_BASES = ("https://aads.newtalk.kr",)
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
ROLE_RE = re.compile(r"권장 역할:\s*(PM|Developer|QA)\b")


def offline_plan() -> dict:
    spec = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
    created = json.loads(RESULT_PATH.read_text(encoding="utf-8"))["created_ids"]
    counts = {r: 0 for r in ROLES}
    by_role: dict[str, list[str]] = {r: [] for r in ROLES}
    for item in spec["items"]:
        found = ROLE_RE.findall(item["description"])
        assert len(found) == 1, f"{item['ref']}: 권장 역할 표기가 정확히 1개가 아니다"
        counts[found[0]] += 1
        by_role[found[0]].append(created[item["ref"]])
    ids = sorted(created.values())
    md5 = hashlib.md5(",".join(ids).encode(), usedforsecurity=False).hexdigest()
    assert counts == EXPECTED, counts
    assert md5 == PRODUCTION_IDS_MD5, md5
    return {"goal_id": GOAL_ID, "total": len(ids), "role_counts": counts, "ids_md5": md5,
            "scoped_role_keys": {r: f"{r}@{GOAL_ID[:8]}" for r in ROLES}}


def _is_allowed_base(base: str) -> bool:
    parsed = urlparse(base)
    if parsed.username is not None or parsed.password is not None:
        return False
    if parsed.scheme == "http":
        return parsed.hostname in ("127.0.0.1", "localhost", "::1")
    return parsed.scheme == "https" and f"https://{parsed.hostname}" in ALLOWED_BASES and parsed.port in (None, 443)


def resolve_auth(env: dict[str, str]) -> tuple[tuple[str, str, str] | None, str | None]:
    """(base, bearer, session) 또는 차단 사유. 네트워크·파일 접근 없음."""
    if any(env.get(k, "").strip() for k in SERVICE_PRINCIPAL_ENV):
        return None, "서비스 principal 설정이 보이면 쓰기를 거부한다(CEO 승인·소유권은 사용자 JWT 로만 성립)."
    bearer = env.get("AADS_BEARER_TOKEN", "").strip()
    if not bearer:
        return None, "AADS_BEARER_TOKEN 이 없어 실행하지 않는다."
    session = env.get("AADS_ACTOR_SESSION_ID", "").strip()
    if not UUID_RE.match(session):
        return None, "AADS_ACTOR_SESSION_ID(UUID)를 명시해야 한다. 기본 세션은 없다."
    base = env.get("AADS_API_BASE", "https://aads.newtalk.kr/api/v1").rstrip("/")
    if not _is_allowed_base(base):
        return None, "AADS_API_BASE 는 https://aads.newtalk.kr 또는 로컬 루프백만 허용한다(토큰 외부 전송 금지)."
    return (base, bearer, session), None


def check_refs(decision_ref: str, message_id: str) -> str | None:
    if not decision_ref.strip() or not message_id.strip():
        return "--decision-ref 와 --approval-message-id 는 비어 있을 수 없다(빈 승인은 승인이 아니다)."
    if not UUID_RE.match(message_id.strip()):
        return "--approval-message-id 는 UUID 여야 한다."
    return None


def call(base: str, bearer: str, session: str, method: str, path: str, body: dict | None = None):
    req = urllib.request.Request(
        base + path, method=method, data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {bearer}", "X-Chat-Session-ID": session, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read() or b"{}")
        except json.JSONDecodeError:
            return exc.code, {}


def holders_from_args(args: argparse.Namespace) -> tuple[dict[str, str] | None, str | None]:
    holders = {"PM": args.pm_session, "Developer": args.developer_session, "QA": args.qa_session}
    if not all(UUID_RE.match(v or "") for v in holders.values()):
        return None, "--pm-session/--developer-session/--qa-session 은 모두 UUID 여야 한다(역할당 서로 다른 세션)."
    if len(set(holders.values())) != 3:
        return None, "세 역할의 보유 세션은 서로 달라야 한다."
    return holders, None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--stage", default="plan",
                    choices=("plan", "create-sessions", "preview", "propose", "decide", "execute", "rollback"))
    ap.add_argument("--decision-ref", default="")
    ap.add_argument("--approval-message-id", default="")
    ap.add_argument("--pm-session", default="")
    ap.add_argument("--developer-session", default="")
    ap.add_argument("--qa-session", default="")
    ap.add_argument("--workspace-id", default="", help="create-sessions: 대상 프로젝트(AADS) 워크스페이스 UUID")
    ap.add_argument("--change-set-id", default="")
    ap.add_argument("--execution-key", default="")
    ap.add_argument("--rollback-key", default="")
    ap.add_argument("--reason", default="")
    ap.add_argument("--reject", action="store_true", help="decide: 승인 대신 반려")
    args = ap.parse_args()

    plan = offline_plan()
    if args.stage == "plan":
        print(json.dumps(plan, ensure_ascii=False))
        return 0

    resolved, reason = resolve_auth(dict(os.environ))
    if resolved is None:
        print(reason, file=sys.stderr)
        return 2
    base, bearer, session = resolved

    if args.stage == "create-sessions":
        if not UUID_RE.match(args.workspace_id):
            print("--workspace-id(UUID)가 필요하다.", file=sys.stderr)
            return 2
        out = {}
        for role in ROLES:
            status, body = call(base, bearer, session, "POST", "/chat/sessions",
                                {"workspace_id": args.workspace_id, "title": f"{role} · 정본문서 업무계층", "role_key": role})
            out[role] = {"http": status, "session_id": body.get("id") if status == 201 else None}
        print(json.dumps(out))
        return 0 if all(v["http"] == 201 for v in out.values()) else 1

    if args.stage in {"preview", "propose", "execute"}:
        problem = check_refs(args.decision_ref, args.approval_message_id)
        if problem:
            print(problem, file=sys.stderr)
            return 2

    if args.stage in {"preview", "propose"}:
        holders, problem = holders_from_args(args)
        if holders is None:
            print(problem, file=sys.stderr)
            return 2
        body = {"decision_ref": args.decision_ref, "approval_message_id": args.approval_message_id, "holders": holders}
        suffix = "preview" if args.stage == "preview" else "proposals"
        status, res = call(base, bearer, session, "POST", f"/goals/{GOAL_ID}/canonical-assignment/{suffix}", body)
    else:
        if not UUID_RE.match(args.change_set_id):
            print("--change-set-id(UUID)가 필요하다.", file=sys.stderr)
            return 2
        prefix = f"/work-item-change-sets/{args.change_set_id}/assignment"
        if args.stage == "decide":
            status, res = call(base, bearer, session, "POST", f"{prefix}/decision",
                               {"approve": not args.reject, "reason": args.reason})
        elif args.stage == "execute":
            if not args.execution_key.strip():
                print("--execution-key 가 필요하다(재실행은 같은 키로).", file=sys.stderr)
                return 2
            status, res = call(base, bearer, session, "POST", f"{prefix}/execute", {
                "decision_ref": args.decision_ref, "approval_message_id": args.approval_message_id,
                "execution_key": args.execution_key})
        else:
            if not args.rollback_key.strip() or not args.reason.strip():
                print("--rollback-key 와 --reason 이 필요하다.", file=sys.stderr)
                return 2
            status, res = call(base, bearer, session, "POST", f"{prefix}/rollback",
                               {"rollback_key": args.rollback_key, "reason": args.reason})
    print(json.dumps({"http": status, "response": res}, ensure_ascii=False, default=str))
    return 0 if status in (200, 201) else 1


if __name__ == "__main__":
    sys.exit(main())
