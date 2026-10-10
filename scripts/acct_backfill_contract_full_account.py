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
  - 반영 규칙(대상 판정·masked 일치·requested 재발행·감사 로그)은 업로드 시 자동 반영과 같은 공용 함수
    yeoljeong_finance_service.autofill_contract_bank_account 하나다. 여기에 복제하지 않는다.
  - requested 계약은 draft 로 되돌려 기존 서명 토큰을 죽이고 같은 서명요청 경로로 새 요청을 자동 재발행한다.
  - 출력에는 전체 번호를 찍지 않는다. 끝 4자리만.
  - 운영 DB 쓰기는 --apply 일 때만 일어난다. --tenant 는 필수다(테넌트 간 섞임 방지).

사용 (운영 컨테이너 안 — 서비스와 같은 환경변수로 DB 에 붙는다):
    python3 scripts/acct_backfill_contract_full_account.py --tenant <uuid>             # dry-run
    python3 scripts/acct_backfill_contract_full_account.py --tenant <uuid> --apply     # 실제 반영
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any, Awaitable, Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

BANKBOOK_TYPE = "bankbook"
SKIPPED_DOC_STATUSES = frozenset({"missing", "superseded"})
SOURCE = "backfill_script"

ReadNumber = Callable[[dict[str, Any]], Awaitable[str]]


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


async def process(
    contracts: list[dict[str, Any]],
    docs: list[dict[str, Any]],
    *,
    read_number: ReadNumber,
    apply: bool,
    user: dict[str, Any],
) -> list[dict[str, Any]]:
    from app.services import yeoljeong_finance_service as service

    tenant_id = service._tenant_id(user)
    rows: list[dict[str, Any]] = []
    number_cache: dict[str, str] = {}
    for contract in contracts:
        reason = service.contract_account_autofill_skip_reason(contract, None, tenant_id=tenant_id)
        doc = None if reason else bankbook_for(contract, docs)
        if not reason and doc is None:
            reason = "no_bankbook"
        if reason:
            rows.append({
                "contract_id": str(contract.get("id") or ""), "employee": str(contract.get("employee_name") or ""),
                "status": str(contract.get("status") or ""), "action": "skip", "reason": reason,
                "last4": "", "revoked_request": False, "reissued": False, "reissue_error": "",
            })
            continue
        doc_id = str(doc.get("id") or "")
        if doc_id not in number_cache:
            try:
                number_cache[doc_id] = str(await read_number(doc) or "").strip()
            except Exception as exc:  # noqa: BLE001 — 한 건의 판독 실패가 나머지를 막지 않는다
                print(f"  판독 오류 doc={doc_id[:8]}: {type(exc).__name__}", file=sys.stderr)
                number_cache[doc_id] = ""
        # 서비스 DB 헬퍼(_run_db)는 실행 중인 이벤트 루프 안에서는 조용히 None 을 돌려준다 — 루프 밖 스레드에서 쓴다.
        rows.append(await asyncio.to_thread(
            service.autofill_contract_bank_account, contract, number_cache[doc_id], user, source=SOURCE, apply=apply,
        ))
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
            note = "서명요청 토큰 무효화 → 새 서명요청 자동 재발행" if row.get("reissued") else (
                "서명요청 토큰 무효화 → draft, 재발행 실패 — 서명 요청 다시 보내기 필요" if row["action"] == "fill"
                else "서명요청 토큰 무효화 후 재발행 예정"
            )
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
        user=user,
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
