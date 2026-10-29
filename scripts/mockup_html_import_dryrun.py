#!/usr/bin/env python3
"""Read-only dry run of the mockup HTML import path against a public URL.

Fetches through the same allow-list/SSRF/size checks as the API, then runs the same validation and storage code,
but into a throw-away temp root (never data/mockup_assets). Prints a JSON summary.

    python3 scripts/mockup_html_import_dryrun.py https://v2.newtalk.kr/reports/ntv2-order-return-rev5.html [PROJECT]
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    url, project = sys.argv[1], (sys.argv[2] if len(sys.argv) > 2 else "NTV2").upper()
    with tempfile.TemporaryDirectory(prefix="mockup-html-dryrun-") as tmp:
        import os

        os.environ["MOCKUP_REVIEW_ASSET_ROOT"] = tmp
        from fastapi import HTTPException

        from app.services import mockup_html_import as imp
        from app.services import mockup_review_service as svc

        summary: dict = {"url": url, "temp_root": tmp}
        try:
            data = imp.fetch_html_url(url)
        except HTTPException as exc:
            print(json.dumps({**summary, "fetch_error": exc.detail}, ensure_ascii=False, indent=2))
            return 1
        text = data.decode("utf-8", errors="replace")
        digest = svc.sha256_hex(data)
        uri = f"{svc.ASSET_SCHEME}mockup_assets/{project}/html/{digest[:16]}/index.html"
        external, relative = imp.scan_references(uri, text)
        summary.update(byte_size=len(data), sha256=digest, title=imp.extract_title(text),
                       external_ref_count=len(external), external_refs_first20=external[:20],
                       relative_ref_count=len(relative), relative_refs_first20=relative[:20])
        try:
            asset, created = imp.store_html(project, data)
            summary.update(import_result="stored", created=created, asset=asset,
                           stored_files=[str(p.relative_to(tmp)) for p in Path(tmp).rglob("*") if p.is_file()])
        except HTTPException as exc:
            summary.update(import_result="rejected", status=exc.status_code,
                           code=exc.detail.get("code"), message=exc.detail.get("message"),
                           stored_files=[str(p.relative_to(tmp)) for p in Path(tmp).rglob("*") if p.is_file()])
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
