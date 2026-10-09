#!/usr/bin/env python3
"""미서명 근로계약서에 전체 계좌번호를 소급해서 채운다 (기본은 dry-run).

ACCT-CONTRACT-FULL-ACCOUNT-NO-20261010 — 근로계약서에는 마스킹 없이 전체 계좌번호가 찍혀야 한다(CEO 승인 2026-10-10).
이 변경 이전에 만든 draft/requested 계약은 bank_account_masked 만 갖고 있다.

규칙
  - 대상: status 가 draft 또는 requested 인 계약만. signed(서명완료)·그 밖의 상태는 읽지도 고치지도 않는다.
  - 전체 번호의 출처는 그 직원의 현재 통장사본이다(대체되지 않은 최신 1건). 서류에 이미 저장된
    bank_account_number 가 있으면 그것을, 없으면 파일을 다시 OCR 판독한다.
  - 계약에 적힌 masked 값과 자릿수가 같고, 보이는 숫자가 같은 자리에서 일치하고, 끝 4자리가 보일 때만 채운다.
    판독 실패·불일치·masked 없음은 건너뛰고 목록으로 출력한다.
  - requested 계약은 내용이 바뀌므로 저장 경로(save_contract)와 같게 draft 로 되돌리고 기존 서명 토큰을
    무효화한다. 관리자가 기존 "서명 요청" 경로로 새 토큰을 받아 다시 보낸다(이 스크립트는 알림을 보내지 않는다).
  - 출력에는 전체 번호를 찍지 않는다. 끝 4자리만.
  - 운영 DB 쓰기는 --apply 일 때만 일어난다. --tenant 는 필수다(테넌트 간 섞임 방지).

사용 (운영 컨테이너 안 — 서비스와 같은 환경변수로 DB 에 붙는다):
    python3 scripts/acct_backfill_contract_full_account.py --tenant <uuid>             # dry-run
    python3 scripts/acct_backfill_contract_full_account.py --tenant <uuid> --apply     # 실제 반영
"""
from __future__ import annotations

import argparse
import asyncio
import re
import sys
from pathlib import Path
from typing import Any, Awaitable, Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

TARGET_STATUSES = frozenset({"draft", "requested"})
BANKBOOK_TYPE = "bankbook"
SKIPPED_DOC_STATUSES = frozenset({"missing", "superseded"})

ReadNumber = Callable[[dict[str, Any]], Awaitable[str]]


def last4(number: str) -> str:
    digits = re.sub(r"\D", "", str(number or ""))
    return f"…{digits[-4:]}" if len(digits) >= 4 else "…"


def skip_reason(contract: dict[str, Any]) -> str:
    """대상이 아니면 사유 코드, 대상이면 빈 문자열."""
    if contract.get("deleted_at"):
        return "deleted"
    status = str(contract.get("status") or "").strip().lower()
    if status not in TARGET_STATUSES:
        return f"status_{status or 'unknown'}"
    if str(contract.get("bank_account_number") or "").strip():
        return "already_has_number"
    if not str(contract.get("bank_account_masked") or "").strip():
        return "no_masked"
    return ""


def bankbook_for(contract: dict[str, Any], docs: list[dict[str, Any]]) -> dict[str, Any] | None:
    """직원의 현재 통장사본(대체되지 않은 최신 1건)."""
    request_id = str(contract.get("employee_request_id") or "").strip()
    email = str(contract.get("employee_email") or "").strip().lower()
    matched = [
        doc
        for doc in docs
        if str(doc.get("document_type") or "") == BANKBOOK_TYPE
        and not doc.get("deleted_at")
        and str(doc.get("status") or "").strip().lower() not in SKIPPED_DOC_STATUSES
        and (
            (request_id and str(doc.get("employee_request_id") or "").strip() == request_id)
            or (email and str(doc.get("employee_email") or "").strip().lower() == email)
        )
    ]
    matched.sort(key=lambda doc: str(doc.get("uploaded_at") or doc.get("updated_at") or ""), reverse=True)
    return matched[0] if matched else None


def decide(contract: dict[str, Any], number: str) -> str:
    """채워도 되면 빈 문자열, 아니면 건너뛰는 사유."""
    from app.services import obys_bankbook_extract as bankbook

    if not number:
        return "unreadable"
    if not bankbook.account_matches_masked(number, str(contract.get("bank_account_masked") or "")):
        return "mismatch"
    return ""


async def process(
    contracts: list[dict[str, Any]],
    docs: list[dict[str, Any]],
    *,
    read_number: ReadNumber,
    apply: bool,
    write: Callable[[dict[str, Any]], None],
) -> list[dict[str, Any]]:
    from app.services import yeoljeong_finance_service as service

    rows: list[dict[str, Any]] = []
    number_cache: dict[str, str] = {}
    for contract in contracts:
        row = {
            "contract_id": str(contract.get("id") or ""),
            "employee": str(contract.get("employee_name") or ""),
            "status": str(contract.get("status") or ""),
            "action": "skip",
            "reason": "",
            "last4": "",
            "revoked_request": False,
        }
        rows.append(row)
        reason = skip_reason(contract)
        if reason:
            row["reason"] = reason
            continue
        doc = bankbook_for(contract, docs)
        if doc is None:
            row["reason"] = "no_bankbook"
            continue
        doc_id = str(doc.get("id") or "")
        if doc_id not in number_cache:
            try:
                number_cache[doc_id] = str(await read_number(doc) or "").strip()
            except Exception as exc:  # noqa: BLE001 — 한 건의 판독 실패가 나머지를 막지 않는다
                print(f"  판독 오류 doc={doc_id[:8]}: {type(exc).__name__}", file=sys.stderr)
                number_cache[doc_id] = ""
        number = number_cache[doc_id]
        reason = decide(contract, number)
        if reason:
            row["reason"] = reason
            continue
        row["last4"] = last4(number)
        row["action"] = "fill" if apply else "would_fill"
        row["revoked_request"] = str(contract.get("status") or "").strip().lower() == "requested"
        if not apply:
            continue
        updated = dict(contract)
        updated["bank_account_number"] = number
        if row["revoked_request"]:
            updated["status"] = "draft"
            service._revoke_contract_signature_request(updated)
        updated["updated_at"] = service._now()
        # 서비스 DB 헬퍼(_run_db)는 실행 중인 이벤트 루프 안에서는 조용히 None 을 돌려준다 — 루프 밖 스레드에서 쓴다.
        await asyncio.to_thread(write, updated)
    return rows


def system_user(tenant_id: str) -> dict[str, Any]:
    return {
        "email": "acct-backfill@system.local",
        "user_role": "system",
        "is_admin": True,
        "tenant_id": tenant_id,
        "current_membership": {"tenant_id": tenant_id, "status": "active", "role": "admin"},
    }


def _print_report(rows: list[dict[str, Any]], apply: bool) -> None:
    mode = "APPLY" if apply else "DRY-RUN"
    print(f"[{mode}] 계약 {len(rows)}건 검토")
    print(f"{'contract':10}  {'employee':12}  {'status':10}  {'action':10}  {'last4':6}  note")
    for row in rows:
        note = row["reason"]
        if row["revoked_request"]:
            note = "서명요청 토큰 무효화 → draft, 서명 요청 재발송 필요"
        print(
            f"{row['contract_id'][:8]:10}  {row['employee']:12}  {row['status']:10}  "
            f"{row['action']:10}  {row['last4']:6}  {note}"
        )
    skipped = [row for row in rows if row["action"] == "skip" and not row["reason"].startswith("status_")]
    if skipped:
        print(f"\n건너뜀 {len(skipped)}건 — 직접 확인 필요: " + ", ".join(
            f"{row['contract_id'][:8]}({row['reason']})" for row in skipped
        ))


async def run(args: argparse.Namespace) -> int:
    from app.services import obys_bankbook_extract as bankbook
    from app.services import yeoljeong_finance_service as service

    user = system_user(args.tenant)
    # _read_hr 를 루프 안에서 직접 부르면 _run_db 가 None 을 돌려 0건으로 보인다(2026-10-10 dry-run 실측).
    contracts = await asyncio.to_thread(service._read_hr, "contracts", user)
    docs = await asyncio.to_thread(service._read_hr, "onboarding_documents", user)
    if args.contract_id:
        wanted = set(args.contract_id)
        contracts = [c for c in contracts if str(c.get("id") or "") in wanted or str(c.get("id") or "")[:8] in wanted]

    async def read_number(doc: dict[str, Any]) -> str:
        stored = (doc.get("extracted_fields") or {}).get("bank_account_number") if isinstance(doc.get("extracted_fields"), dict) else ""
        if stored:
            return str(stored)
        _, path = await asyncio.to_thread(service.get_onboarding_document, str(doc.get("id") or ""), user)
        result = await bankbook.extract_bankbook_bounded(await asyncio.to_thread(path.read_bytes))
        return str(result.get("bank_account_number") or "") if result.get("extract_status") == bankbook.STATUS_EXTRACTED else ""

    rows = await process(
        contracts,
        docs,
        read_number=read_number,
        apply=args.apply,
        write=lambda record: service._write_hr_record("contracts", record, user),
    )
    _print_report(rows, args.apply)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tenant", required=True, help="대상 테넌트 UUID")
    parser.add_argument("--apply", action="store_true", help="실제로 쓴다 (기본은 dry-run)")
    parser.add_argument("--contract-id", action="append", default=[], help="이 계약만 (id 전체 또는 앞 8자리, 반복 가능)")
    args = parser.parse_args(argv)
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
