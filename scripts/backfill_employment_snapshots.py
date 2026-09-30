#!/usr/bin/env python3
"""서명 완료 계약서 → 직원 고용조건 스냅샷 백필 (AADS-OBYS-CONTRACT-TO-EMPLOYMENT-PAYROLL-20260930).

신규 서명분은 sign_contract_and_deliver 훅이 스냅샷을 만든다. 이 스크립트는 그
훅 이전에 서명된 계약서가 있는 직원만을 위한 것이다. 마이그레이션에서 자동으로
돌리지 않는다.

기본은 --dry-run 이다. 무엇을 바꿀지 출력만 하고 아무것도 쓰지 않는다.
실제로 쓰려면 --apply 를 명시한다. 쓰기는 서비스의 _sync_employee_employment 를
그대로 거치므로 스냅샷 생성마다 yeoljeong_audit_logs 에 trigger=backfill 로 남는다.

    OBYS_DATABASE_URL=... python3 scripts/backfill_employment_snapshots.py --tenant <uuid>
    OBYS_DATABASE_URL=... python3 scripts/backfill_employment_snapshots.py --tenant <uuid> --apply

서명 계약서·기존 스냅샷을 지우거나 고치지 않는다. 원본(서명 계약)이 없는 직원은 건너뛴다.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services import yeoljeong_finance_service as svc  # noqa: E402

ACTOR = "system:employment-backfill"


def _system_user(tenant_id: str) -> dict:
    return {
        "email": ACTOR,
        "user_role": "system",
        "tenant_id": tenant_id,
        "current_membership": {"tenant_id": tenant_id, "status": "active", "role": "owner"},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tenant", required=True, help="대상 테넌트 UUID (한 번에 한 테넌트)")
    parser.add_argument("--employee", default="", help="특정 직원 request_id 만")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", default=True, help="변경 계획만 출력 (기본)")
    mode.add_argument("--apply", action="store_true", help="실제로 스냅샷을 쓴다")
    args = parser.parse_args(argv)

    try:
        tenant_id = str(UUID(args.tenant))
    except ValueError:
        print(f"[backfill] 잘못된 테넌트 UUID: {args.tenant}", file=sys.stderr)
        return 2
    user = _system_user(tenant_id)
    label = "APPLY" if args.apply else "DRY-RUN"
    store = "DB" if svc._db_available() else f"FILE({svc.DATA_DIR})"
    print(f"[backfill] mode={label} tenant={tenant_id} store={store}")

    contracts = svc._read_hr("contracts", user)
    employees = [
        row
        for row in svc._read_hr("employee_join_requests", user)
        if str(row.get("status") or "").lower() == "approved"
        and (not args.employee or str(row.get("id") or "") == args.employee)
    ]
    counts = {"planned": 0, "applied": 0, "in_sync": 0, "no_source": 0, "failed": 0}
    for employee in employees:
        request_id = str(employee.get("id") or "")
        derived = svc._derive_current_employment(
            contracts, employee_email=str(employee.get("email") or ""), employee_request_id=request_id
        )
        if derived is None:
            counts["no_source"] += 1
            continue
        if svc._employment_snapshot_is_current(employee, derived):
            counts["in_sync"] += 1
            continue
        counts["planned"] += 1
        terms = derived["employment"]
        print(
            f"[backfill] {label} employee={request_id} email={svc._mask_email(str(employee.get('email') or ''))} "
            f"current={employee.get('current_employment_contract_id') or '-'} -> contract={derived['contract_id']} "
            f"type={terms.get('contract_type')} wage_type={terms.get('wage_type')} wage={terms.get('wage')} "
            f"start={terms.get('start_date')}"
        )
        if args.apply:
            try:
                result = svc._sync_employee_employment(employee, user, actor=ACTOR, trigger="backfill")
                counts["applied"] += 1 if result.get("status") == "updated" else 0
            except Exception as exc:  # noqa: BLE001 — 한 직원 실패가 나머지를 막지 않게
                counts["failed"] += 1
                print(f"[backfill] FAILED employee={request_id} {type(exc).__name__}: {exc}", file=sys.stderr)
    print("[backfill] summary " + " ".join(f"{key}={value}" for key, value in counts.items()))
    return 1 if counts["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
