#!/usr/bin/env python3
"""AADS-192: /ops/deploy/status 페이로드에 error_summary 가 실제로 실려 나오는지 검증.

실행: docker exec aads-server python3 /app/scripts/aads192_verify_payload.py
테스트용 deploy_runs 행을 넣고 확인한 뒤 반드시 삭제한다.
"""
import asyncio
import json
import sys

from app.api.ops import _get_conn
from app.services.deploy_observability import get_deploy_status

SENTINEL = "aads192-verify-sentinel"
TEST_SHA = "verifysha192"


async def main() -> int:
    conn = await _get_conn()
    try:
        row_id = await conn.fetchval(
            """
            INSERT INTO deploy_runs(
                project, component, deploy_type, status, release_sha,
                created_at, updated_at, phase_started_at, phase_completed_at,
                requested_at, request_source, error_summary
            ) VALUES (
                'AADS', 'dashboard', 'bluegreen', 'failed', $1,
                NOW(), NOW(), NOW(), NOW(), NOW(), 'deploy.sh', $2
            ) RETURNING id
            """,
            TEST_SHA,
            SENTINEL,
        )
        print(f"[setup] deploy_runs #{row_id} 삽입 (status=failed)")
        try:
            payload = await get_deploy_status(conn)
            items = payload.get("project_deployments") or []
            hit = None
            for item in items:
                if item.get("release_sha") == TEST_SHA or item.get("error_summary") == SENTINEL:
                    hit = item
                    break
            has_key = any("error_summary" in item for item in items)
            print(f"[check] project_deployments 항목 수 = {len(items)}")
            print(f"[check] error_summary 키 존재 = {has_key}")
            if hit is not None:
                print("[check] 테스트 행 payload =")
                print(json.dumps(
                    {k: hit.get(k) for k in ("project", "status", "release_sha", "error_summary")},
                    ensure_ascii=False,
                ))
            if not has_key:
                print("FAIL: payload 에 error_summary 키가 없습니다")
                return 1
            if hit is not None and hit.get("error_summary") != SENTINEL:
                print("FAIL: error_summary 값이 전달되지 않았습니다")
                return 1
            print("PASS: error_summary 가 API 페이로드까지 전달됩니다")
            return 0
        finally:
            deleted = await conn.fetchval(
                "DELETE FROM deploy_runs WHERE id=$1 AND release_sha=$2 RETURNING id",
                row_id,
                TEST_SHA,
            )
            print(f"[cleanup] 삭제된 테스트 행 = {deleted}")
    finally:
        await conn.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
