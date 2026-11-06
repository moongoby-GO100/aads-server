"""기존 신분증·등본 판독 백필. 기본 dry-run은 건수/상태만 출력한다."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


async def run(args):
    counts = Counter()
    if args.input:
        rows = json.loads(Path(args.input).read_text())
        if args.apply:
            raise ValueError("fixture input is dry-run only")
        connection = None
    else:
        from app.services import yeoljeong_finance_service as svc
        import asyncpg
        dsn = os.getenv("YEOLJEONG_FINANCE_DATABASE_URL") or os.getenv("OBYS_DATABASE_URL")
        if not dsn:
            raise RuntimeError("OBYS database configuration required")
        connection = await asyncpg.connect(dsn)
        rows = await connection.fetch("""SELECT * FROM yeoljeong_onboarding_documents
            WHERE deleted_at IS NULL AND document_type IN ('id_card', 'resident_register')
            AND status NOT IN ('superseded', 'deleted', 'rejected', 'missing')""")
        rows = [svc._db_row_to_record("onboarding_documents", dict(row)) for row in rows]
    try:
        for row in rows:
            if row.get("document_type") not in {"id_card", "resident_register"} or row.get("status") in {"superseded", "deleted", "rejected", "missing"} or row.get("deleted_at"):
                continue
            counts["eligible"] += 1
            counts[str(row.get("extract_status") or "not_extracted")] += 1
            if not args.apply:
                continue
            from app.services import obys_identity_extract as identity
            path = svc._onboarding_document_path(row)
            if path is None:
                counts["file_missing"] += 1
                continue
            result = await identity.extract_identity_bounded(await asyncio.to_thread(path.read_bytes),
                document_type=row["document_type"], employee_name=row.get("employee_name", ""))
            identity.apply_to_record(row, result)
            # Update metadata only; never touch contracts. Fence against supersession during OCR.
            changed = await connection.execute("""UPDATE yeoljeong_onboarding_documents
                SET metadata = COALESCE(metadata, '{}'::jsonb) || $3::jsonb
                WHERE id::text=$1 AND tenant_id::text=$2 AND deleted_at IS NULL
                AND status NOT IN ('superseded', 'deleted', 'rejected', 'missing')""",
                str(row["id"]), str(row["tenant_id"]), json.dumps({k: v for k, v in row.items()
                if k.startswith("extract") or k == "extracted_at"}))
            counts["saved" if changed == "UPDATE 1" else "skipped_changed"] += 1
    finally:
        if connection:
            await connection.close()
    print(json.dumps({"mode": "apply" if args.apply else "dry-run", "counts": dict(counts)}, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--dry-run", action="store_true")
    parser.add_argument("--input", help="dry-run fixture JSON; never prints field values")
    asyncio.run(run(parser.parse_args()))
