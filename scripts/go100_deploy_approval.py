#!/usr/bin/env python3
"""GO100 승인 계약 검증 CLI / go100-deploy-approval."""
import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


async def verify(args):
    import asyncpg
    from app.services.go100_deploy_approval import validate_contract

    dsn = os.environ.get("DATABASE_URL") or os.environ.get("SUPABASE_DIRECT_URL")
    if not dsn:
        raise ValueError("database_unconfigured")
    conn = await asyncpg.connect(dsn, timeout=10, command_timeout=15)
    try:
        return await validate_contract(conn, args.run_id, args.owner_instance, args.owner_epoch)
    finally:
        await conn.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", type=int, required=True)
    parser.add_argument("--owner-instance", required=True)
    parser.add_argument("--owner-epoch", required=True)
    args = parser.parse_args(argv)
    try:
        result = asyncio.run(verify(args))
    except Exception as exc:
        from app.services.go100_deploy_approval import ApprovalDenied
        reason = str(exc) if isinstance(exc, ApprovalDenied) else "database_validation_unavailable"
        print(json.dumps({"ok": False, "reason": reason}))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
