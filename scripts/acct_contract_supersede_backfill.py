#!/usr/bin/env python3
"""급여 명세 → 급여월 기준 적용 계약 연결 백필 (ACCT-OBYS-CONTRACT-PAYROLL-SYNC-20261105).

CEO 규칙: 계약서의 계약일자(contract_date)가 계약조건 적용일이다. 급여월 M 의 적용 계약은
같은 사업자·같은 직원의 서명 계약 중 계약일자 <= M 말일 인 것 가운데 가장 늦은 것이고,
변경계약이 원계약을 대체하면 원계약은 변경계약 계약일자 전날까지만 적용된다.

기본은 dry-run 이다. --apply 를 명시해야 쓴다.
 - draft 급여: contract_id / source_contract_id / 적용 기간 연결과 계약 이탈(contract_deviation)만 갱신한다.
 - confirmed·paid 급여: contract_id 등 연결 정보가 비어 있을 때 그 정보만 추가한다. source_contract_id 는 건드리지 않는다.
 - 어떤 경우에도 금액(총지급액·공제·실수령)과 상태는 바꾸지 않는다.
 - 비과세 식대 근거가 없으면(None) 이탈 재계산도 하지 않는다 — 0원으로 간주하지 않는다.
사업자별로 따로 계산한다(겸직 직원의 계약 기간을 섞지 않는다).

    OBYS_DATABASE_URL=... python3 scripts/acct_contract_supersede_backfill.py --tenant <uuid>
    OBYS_DATABASE_URL=... python3 scripts/acct_contract_supersede_backfill.py --tenant <uuid> --apply
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services import yeoljeong_finance_service as svc  # noqa: E402

ACTOR = "system:contract-supersede-backfill"
LINK_FIELDS = ("contract_id", "contract_applies_from", "contract_applies_to", "contract_payroll_month", "non_tax_meal_allowance_basis")


def _system_user(tenant_id: str) -> dict:
    return {
        "email": ACTOR,
        "user_role": "system",
        "tenant_id": tenant_id,
        "current_membership": {"tenant_id": tenant_id, "status": "active", "role": "owner"},
    }


def compute_new_payload(statement: dict[str, Any], derived: dict[str, Any] | None) -> dict[str, Any] | None:
    """바뀔 필드만 담은 dict. 바꿀 것이 없으면 None. 금액·상태 필드는 절대 담지 않는다."""
    if not derived:
        return None
    month = str(statement.get("payroll_month") or "")
    link = svc._payroll_contract_link(derived, month)
    state = svc._payroll_state(statement)
    changes: dict[str, Any] = {}
    if state in svc.PAYROLL_EDITABLE_STATUSES:
        link = {**link, "source_contract_id": derived["contract_id"]}
        for key, value in link.items():
            if statement.get(key) != value:
                changes[key] = value
        # 비과세 식대 근거가 없으면 이탈을 다시 계산하지 않는다(None → 0원 오인 방지).
        if link.get("non_tax_meal_allowance_basis") is not None:
            plan = svc._payroll_defaults_from_employment(derived)
            _, _, deviation = svc._apply_payroll_contract_defaults(dict(statement), plan)
            if (statement.get("contract_deviation") or {}) != deviation:
                changes["contract_deviation"] = deviation
    else:
        # 확정·지급 명세: 연결 정보가 비어 있을 때만 채운다. 이미 연결됐으면 그대로 둔다.
        if not statement.get("contract_id"):
            changes.update(link)
    return changes or None


def _belongs_to(row: dict[str, Any], *, business_id: str, request_id: str, email: str) -> bool:
    if not svc._row_in_business(row, business_id):
        return False
    if str(row.get("employee_request_id") or "") == request_id:
        return True
    return not row.get("employee_request_id") and bool(email) and str(row.get("employee_email") or "").lower() == email


def _apply_one(user: dict, *, email: str, request_id: str, business_id: str, payroll_id: str) -> bool:
    """직원 쓰기 잠금 안에서 급여 행과 계약을 모두 다시 읽고, 그 최신 값으로 적용 계약을 다시 판정해 저장한다.

    잠금은 급여 저장·확정·지급(_payroll_locked)과 같은 DB advisory lock(obys-employee-write:<email>)이다.
    dry-run 이후 관리자가 금액·급여월·직원·사업자를 고쳤거나 새 계약이 서명됐어도, 오래된 판정이나 값으로 덮어쓰지 않는다.
    """
    def op() -> bool:
        fresh = next((r for r in svc._read_hr("payroll_statements", user) if str(r.get("id") or "") == payroll_id), None)
        if fresh is None or not _belongs_to(fresh, business_id=business_id, request_id=request_id, email=email):
            return False
        month = str(fresh.get("payroll_month") or "")
        if not month:
            return False
        derived = svc._derive_current_employment(
            svc._read_hr("contracts", user),
            employee_email=email, employee_request_id=request_id, business_id=business_id, payroll_month=month,
        )
        changes = compute_new_payload(fresh, derived)
        if not changes:
            return False
        svc._write_hr_record("payroll_statements", {**fresh, **changes}, user)
        return True
    return bool(svc._serialized_employee_write(email, op))


def run(tenant_id: str, *, apply: bool = False, employee_id: str = "") -> dict[str, int]:
    user = _system_user(tenant_id)
    contracts = svc._read_hr("contracts", user)
    payroll = svc._read_hr("payroll_statements", user)
    employees = [
        row for row in svc._read_hr("employee_join_requests", user)
        if str(row.get("status") or "").lower() == "approved" and (not employee_id or str(row.get("id") or "") == employee_id)
    ]
    counts = {"planned": 0, "applied": 0, "unchanged": 0, "no_contract": 0, "failed": 0}
    for employee in employees:
        business_id = svc._record_business_id(employee)  # 사업자별로 따로 판정한다.
        email = str(employee.get("email") or "").strip().lower()
        request_id = str(employee.get("id") or "")
        rows = [row for row in payroll if _belongs_to(row, business_id=business_id, request_id=request_id, email=email)]
        for row in rows:
            month = str(row.get("payroll_month") or "")
            if not month:
                continue
            derived = svc._derive_current_employment(
                contracts, employee_email=email, employee_request_id=request_id, business_id=business_id, payroll_month=month
            )
            changes = compute_new_payload(row, derived)
            if derived is None:
                counts["no_contract"] += 1
                continue
            if not changes:
                counts["unchanged"] += 1
                continue
            counts["planned"] += 1
            print(
                f"[supersede-backfill] {'APPLY' if apply else 'DRY-RUN'} payroll={row.get('id')} month={month} "
                f"status={svc._payroll_state(row) or 'draft'} business={business_id} "
                f"contract {row.get('source_contract_id') or '-'} -> {derived['contract_id']} fields={sorted(changes)}"
            )
            if apply:
                try:
                    outcome = _apply_one(
                        user, email=email, request_id=request_id, business_id=business_id, payroll_id=str(row.get("id") or "")
                    )
                    counts["applied" if outcome else "unchanged"] += 1
                    if not outcome:
                        counts["planned"] -= 1  # 조회 후 이미 바뀌어 쓸 것이 없어졌다
                except Exception as exc:  # noqa: BLE001 — 한 건 실패가 나머지를 막지 않게
                    counts["failed"] += 1
                    print(f"[supersede-backfill] FAILED payroll={row.get('id')} {type(exc).__name__}: {exc}", file=sys.stderr)
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tenant", required=True, help="대상 테넌트 UUID (한 번에 한 테넌트)")
    parser.add_argument("--employee", default="", help="특정 직원 request_id 만")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", default=True, help="변경 계획만 출력 (기본)")
    mode.add_argument("--apply", action="store_true", help="실제로 연결 정보를 쓴다")
    args = parser.parse_args(argv)
    try:
        tenant_id = str(UUID(args.tenant))
    except ValueError:
        print(f"[supersede-backfill] 잘못된 테넌트 UUID: {args.tenant}", file=sys.stderr)
        return 2
    counts = run(tenant_id, apply=bool(args.apply), employee_id=args.employee)
    print("[supersede-backfill] summary " + " ".join(f"{k}={v}" for k, v in counts.items()))
    return 1 if counts["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
