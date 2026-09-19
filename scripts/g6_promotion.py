#!/usr/bin/env python3
"""Admin CLI for the authenticated G6 activation endpoints."""
from __future__ import annotations

import argparse
import json
import os
import urllib.request


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("promote", "rollback"))
    parser.add_argument("skill_id")
    parser.add_argument("--version")
    parser.add_argument("--payload", required=True, help="JSON evidence/gate request file")
    parser.add_argument("--base-url", default=os.environ.get("AADS_API_URL", "http://127.0.0.1:8100"))
    parser.add_argument("--token", default=os.environ.get("AADS_ADMIN_TOKEN", ""))
    args = parser.parse_args()
    if not args.token:
        parser.error("--token or AADS_ADMIN_TOKEN is required")
    if args.action == "promote" and not args.version:
        parser.error("--version is required for promote")
    suffix = f"/versions/{args.version}/promote" if args.action == "promote" else "/rollback"
    with open(args.payload, "rb") as handle:
        body = handle.read()
    request = urllib.request.Request(
        f"{args.base_url.rstrip('/')}/api/v1/ohvis/harness/skills/{args.skill_id}{suffix}",
        data=body, method="POST",
        headers={"Authorization": f"Bearer {args.token}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        print(json.dumps(json.load(response), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
