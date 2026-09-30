#!/usr/bin/env python3
"""승인된 오비서 직원을 고용주 테넌트 멤버십(role=member)으로 소급 연결한다 (기본은 dry-run).

2026-09-30 진아서버 실측: 승인 직원 7명 중 고용주 테넌트(tenant_memberships)
소속은 0명이라 어떤 직원도 계약서 서명을 끝낼 수 없었다. 이후 승인분은
review_join_request_with_membership 가 연결하지만, 그 이전 승인분은 이
스크립트로만 연결한다 — 마이그레이션·자동 백필로 임의 삽입하지 않는다.
계정 없이 승인된 직원(pending_account)이 나중에 가입했을 때도 이것을 다시 돌린다.

규칙 (서비스 경로와 같다 — app.auth.upsert_tenant_membership 을 그대로 쓴다):
  - 대상: 오비서 DB yeoljeong_employee_join_requests 중 status=approved, tenant_id 있음, 삭제 안 됨.
  - 고용주 테넌트가 active customer 가 아니면 건너뛴다.
  - saas_users 에 활성 계정이 없으면 pending_account 로 출력만 한다.
  - 계정은 가입요청의 requester_user_id(본인 제출분)가 있으면 그것, 없으면 이메일로 찾는다
    (이메일 조회는 그 가입요청의 tenant_id 를 컨텍스트로, SQL 에 테넌트 조건을 건다).
    온라인 승인 경로(link_employee_tenant_membership)와 같은 순서다 — 본인 제출 고정이
    없는 요청은 승인자가 보증한 이메일로 찾는다(basis=email). 운영자는 dry-run 출력의
    basis 열로 어느 쪽으로 찾았는지 확인한다.
  - 이미 활성 owner/admin 이면 역할을 그대로 둔다(강등 금지). 이미 활성 member 면 unchanged.
  - 그 밖에는 role=member, status=active 로 upsert 한다(invited_by = 승인자 계정, 없으면 NULL).
    default 가 비었거나 본인 혼자인 개인 워크스페이스면 고용주 테넌트로 옮긴다(서비스 경로와 같은 함수).
  - --apply 로 연결한 행은 가입요청 request_payload.membership_link 에 표시를 남긴다 —
    이후 반려·퇴사가 회수할 수 있는 것은 이 표시가 있는 멤버십뿐이다.
  - --apply 로 바꾼 행은 yeoljeong_hr_tenant_attribution_audit 에 source=backfill_script 로 남긴다.

DSN:
  --obys-dsn  기본 $YEOLJEONG_FINANCE_DATABASE_URL 또는 $OBYS_DATABASE_URL (가입요청·감사)
  --auth-dsn  기본 $DATABASE_URL (saas_users·tenants·tenant_memberships)

사용 (운영 컨테이너 안):
    python3 scripts/obys_employee_membership_backfill.py                       # dry-run
    python3 scripts/obys_employee_membership_backfill.py --tenant <uuid>       # 테넌트 한정 dry-run
    python3 scripts/obys_employee_membership_backfill.py --apply               # 실제 반영
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

APPROVED_JOIN_REQUESTS_SQL = """
SELECT id::text AS id,
       tenant_id::text AS tenant_id,
       business_id,
       lower(trim(COALESCE(NULLIF(request_payload->>'email', ''), employee_email))) AS email,
       COALESCE(NULLIF(request_payload->>'name', ''), employee_name) AS name,
       CASE WHEN lower(trim(COALESCE(request_payload->>'requester_email', '')))
                 = lower(trim(COALESCE(NULLIF(request_payload->>'email', ''), employee_email)))
            THEN NULLIF(request_payload->>'requester_user_id', '')
       END AS requester_user_id,
       lower(trim(COALESCE(NULLIF(request_payload->>'reviewed_by', ''), reviewed_by, ''))) AS reviewed_by
  FROM yeoljeong_employee_join_requests
 WHERE deleted_at IS NULL
   AND tenant_id IS NOT NULL
   AND lower(COALESCE(NULLIF(request_payload->>'status', ''), status)) = 'approved'
   AND ($1::uuid IS NULL OR tenant_id = $1::uuid)
 ORDER BY tenant_id, email, id
"""

MARK_LINK_SQL = """
UPDATE yeoljeong_employee_join_requests
   SET request_payload = COALESCE(request_payload, '{}'::jsonb)
                         || jsonb_build_object('membership_link', $2::jsonb)
 WHERE id::text = $1
"""

ELEVATED_ROLES = {"owner", "admin"}


def dedupe_targets(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """(tenant, email) 당 1건. 같은 직원의 가입요청이 여럿이면 첫 행만 쓴다."""
    seen: set[tuple[str, str]] = set()
    out: list[dict[str, Any]] = []
    for row in rows:
        key = (str(row.get("tenant_id") or ""), str(row.get("email") or ""))
        if not all(key) or key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out


def plan_action(*, tenant_kind: str | None, user_id: str | None, before: dict[str, Any] | None) -> str:
    """dry-run·apply 가 같은 판정을 쓴다. 반환값은 서비스 경로의 status 와 같은 어휘다."""
    if str(tenant_kind or "").lower() != "customer":
        return "skipped"
    if not user_id:
        return "pending_account"
    active = bool(before) and before.get("status") == "active" and not before.get("deleted")
    if active and str(before.get("role") or "").lower() in ELEVATED_ROLES:
        return "kept_elevated"
    if active and str(before.get("role") or "").lower() == "member":
        return "unchanged"
    return "link"


def _state(value: dict[str, Any] | None) -> str:
    if not value:
        return "-"
    deleted = " deleted" if value.get("deleted") else ""
    return f"{value.get('role')}/{value.get('status')}{deleted}"


async def run(args: argparse.Namespace) -> int:
    import asyncpg

    from app import auth as auth_module

    obys_dsn = args.obys_dsn or os.getenv("YEOLJEONG_FINANCE_DATABASE_URL") or os.getenv("OBYS_DATABASE_URL") or ""
    auth_dsn = args.auth_dsn or os.getenv("DATABASE_URL") or ""
    if not obys_dsn or not auth_dsn:
        print("오류: --obys-dsn / --auth-dsn (또는 OBYS_DATABASE_URL / DATABASE_URL) 가 필요하다", file=sys.stderr)
        return 2

    obys = await asyncpg.connect(obys_dsn, timeout=10)
    auth = await asyncpg.connect(auth_dsn, timeout=10)
    try:
        rows = [dict(r) for r in await obys.fetch(APPROVED_JOIN_REQUESTS_SQL, args.tenant)]
        targets = dedupe_targets(rows)
        mode = "APPLY" if args.apply else "DRY-RUN"
        print(f"[{mode}] 승인 가입요청 {len(rows)}건 → 대상 (tenant,email) {len(targets)}건")
        print(f"{'tenant_id':36}  {'email':32}  {'user':12}  {'before':18}  {'action':15}  after")
        counts: dict[str, int] = {}
        for target in targets:
            tenant_id = str(target["tenant_id"])
            email = str(target["email"])
            tenant_kind = await auth.fetchval(
                "SELECT kind FROM tenants WHERE id = $1::uuid AND status = 'active' AND deleted_at IS NULL",
                tenant_id,
            )
            requester_id = str(target.get("requester_user_id") or "")
            if requester_id:
                basis = "user_id"
                bound_email = await auth_module._active_user_email_by_id(auth, requester_id)
                user_id = requester_id if bound_email == email else None
            else:
                basis = "email"
                # 스크립트의 테넌트 컨텍스트는 가입요청 행 자신의 tenant_id 다. 조회 SQL 이
                # 테넌트 조건을 걸어 다른 고객 테넌트 소속 계정은 뽑지 않는다(온라인 경로와 같다).
                user_id = await auth_module._active_user_id_by_email(
                    auth, email, tenant_id=tenant_id, context_tenant_id=tenant_id
                )
            approver_id = (
                await auth_module._active_user_id_by_email(
                    auth, target["reviewed_by"], tenant_id=tenant_id, context_tenant_id=tenant_id
                )
                if target.get("reviewed_by")
                else None
            )
            after: dict[str, Any] | None = None
            async with auth.transaction():
                if args.apply:
                    # 온라인 승인과 같은 (테넌트, 이메일) advisory xact lock — 동시에 도는 승인·반려와 겹치지 않는다.
                    await auth_module._lock_employee_membership(auth, tenant_id, email)
                before = (
                    await auth_module._membership_state_for_update(auth, tenant_id, str(user_id)) if user_id else None
                )
                action = plan_action(tenant_kind=tenant_kind, user_id=user_id, before=before)
                if args.apply and action == "link":
                    after = await auth_module.upsert_tenant_membership(
                        auth,
                        tenant_id=tenant_id,
                        user_id=str(user_id),
                        role="member",
                        invited_by=approver_id,
                        preserve_elevated_role=True,
                    )
                    await auth_module._move_default_off_personal_workspace(auth, str(user_id), tenant_id)
            planned_after = _state(after) if after else ("member/active" if action == "link" else _state(before))
            counts[action] = counts.get(action, 0) + 1
            print(
                f"{tenant_id:36}  {email:32}  {('yes/' + basis) if user_id else 'no':12}  {_state(before):18}  "
                f"{action:15}  {planned_after}{'' if args.apply or action != 'link' else ' (예정)'}"
            )
            if args.apply and action == "link" and after:
                await obys.execute(
                    MARK_LINK_SQL,
                    str(target["id"]),
                    json.dumps(
                        {
                            "tenant_id": tenant_id,
                            "user_id": str(user_id),
                            "membership_id": str(after.get("membership_id") or ""),
                            "linked_by": "script:obys_employee_membership_backfill",
                            "linked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                        },
                        ensure_ascii=False,
                    ),
                )
            if args.apply and action in {"link", "pending_account"}:
                await _record_audit(obys_dsn, target, action, tenant_id, email, user_id, before, after)
        print("요약:", json.dumps(counts, ensure_ascii=False, sort_keys=True))
        if not args.apply:
            print("dry-run 이다. 반영하려면 --apply 를 붙여 다시 실행한다.")
        return 0
    finally:
        await obys.close()
        await auth.close()


async def _record_audit(
    obys_dsn: str,
    target: dict[str, Any],
    action: str,
    tenant_id: str,
    email: str,
    user_id: str | None,
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
) -> None:
    os.environ.setdefault("OBYS_DATABASE_URL", obys_dsn)
    from app.services import yeoljeong_finance_service as svc

    link = {
        "status": "linked" if action == "link" else action,
        "tenant_id": tenant_id,
        "employee_email": email,
        "user_id": user_id,
        "before": before,
        "after": after,
    }
    row = svc._membership_audit_row(
        link,
        action="approved",
        source="backfill_script",
        record={"id": target["id"], "business_id": target.get("business_id"), "email": email},
        actor_user_id="",
        actor_email="script:obys_employee_membership_backfill",
    )
    if not await svc.record_membership_audit(row):
        print(f"  경고: 감사 기록 실패 tenant={tenant_id} email={email}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="실제로 반영한다 (기본은 dry-run)")
    parser.add_argument("--dry-run", action="store_true", help="명시적 dry-run (기본값과 같다)")
    parser.add_argument("--tenant", default=None, help="고용주 테넌트 UUID 로 한정")
    parser.add_argument("--obys-dsn", default="", help="오비서 업무 DB DSN")
    parser.add_argument("--auth-dsn", default="", help="AADS 인증 DB DSN")
    args = parser.parse_args(argv)
    if args.apply and args.dry_run:
        parser.error("--apply 와 --dry-run 은 함께 쓸 수 없다")
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
