import asyncio
import json
import os
import sys
from datetime import datetime
from typing import Any

import httpx


async def main():
    """Run E2E API tests."""
    base_url = os.getenv("E2E_BASE_URL", "http://localhost:8080/api/v1")
    timeout = 30

    tests = []
    passed = 0
    failed = 0
    details = []

    async with httpx.AsyncClient(timeout=timeout) as client:
        # Test 1: GET /ops/health-check
        test_name = "GET /ops/health-check"
        try:
            response = await client.get(f"{base_url}/ops/health-check")
            if response.status_code == 200:
                passed += 1
                details.append({"test": test_name, "status": "passed"})
            else:
                failed += 1
                details.append({
                    "test": test_name,
                    "status": "failed",
                    "error": f"Expected 200, got {response.status_code}"
                })
        except Exception as e:
            failed += 1
            details.append({"test": test_name, "status": "failed", "error": str(e)})

        # Test 2: GET /ops/pipeline-status
        test_name = "GET /ops/pipeline-status"
        try:
            response = await client.get(f"{base_url}/ops/pipeline-status")
            if response.status_code == 200:
                passed += 1
                details.append({"test": test_name, "status": "passed"})
            else:
                failed += 1
                details.append({
                    "test": test_name,
                    "status": "failed",
                    "error": f"Expected 200, got {response.status_code}"
                })
        except Exception as e:
            failed += 1
            details.append({"test": test_name, "status": "failed", "error": str(e)})

        # Test 3: POST /chat/sessions + DELETE /chat/sessions/{id}
        test_name = "POST /chat/sessions + DELETE /chat/sessions/{id}"
        try:
            # First, get workspace_id
            workspace_id = None
            try:
                workspaces_response = await client.get(f"{base_url}/workspaces")
                if workspaces_response.status_code == 200:
                    workspaces_data = workspaces_response.json()
                    if isinstance(workspaces_data, list) and len(workspaces_data) > 0:
                        workspace_id = workspaces_data[0].get("id")
                    elif isinstance(workspaces_data, dict) and "items" in workspaces_data:
                        items = workspaces_data.get("items", [])
                        if len(items) > 0:
                            workspace_id = items[0].get("id")
            except Exception:
                pass

            if workspace_id:
                # Create session
                payload = {"workspace_id": workspace_id}
                response = await client.post(f"{base_url}/chat/sessions", json=payload)

                if response.status_code == 201:
                    session_data = response.json()
                    session_id = session_data.get("id")

                    if session_id:
                        # Delete session
                        delete_response = await client.delete(f"{base_url}/chat/sessions/{session_id}")
                        if delete_response.status_code in [200, 204]:
                            passed += 1
                            details.append({"test": test_name, "status": "passed"})
                        else:
                            failed += 1
                            details.append({
                                "test": test_name,
                                "status": "failed",
                                "error": f"Delete failed with {delete_response.status_code}"
                            })
                    else:
                        failed += 1
                        details.append({
                            "test": test_name,
                            "status": "failed",
                            "error": "No session ID in response"
                        })
                else:
                    failed += 1
                    details.append({
                        "test": test_name,
                        "status": "failed",
                        "error": f"Expected 201, got {response.status_code}"
                    })
            else:
                details.append({"test": test_name, "status": "skipped", "reason": "No workspace found"})
        except Exception as e:
            failed += 1
            details.append({"test": test_name, "status": "failed", "error": str(e)})

        # Test 4: GET /chat/sessions
        test_name = "GET /chat/sessions"
        try:
            response = await client.get(f"{base_url}/chat/sessions")
            if response.status_code == 200:
                passed += 1
                details.append({"test": test_name, "status": "passed"})
            else:
                failed += 1
                details.append({
                    "test": test_name,
                    "status": "failed",
                    "error": f"Expected 200, got {response.status_code}"
                })
        except Exception as e:
            failed += 1
            details.append({"test": test_name, "status": "failed", "error": str(e)})

        # Test 5: GET /pipeline/jobs
        test_name = "GET /pipeline/jobs"
        try:
            response = await client.get(f"{base_url}/pipeline/jobs")
            if response.status_code == 200:
                passed += 1
                details.append({"test": test_name, "status": "passed"})
            else:
                failed += 1
                details.append({
                    "test": test_name,
                    "status": "failed",
                    "error": f"Expected 200, got {response.status_code}"
                })
        except Exception as e:
            failed += 1
            details.append({"test": test_name, "status": "failed", "error": str(e)})

    # Count total (excluding skipped)
    total = sum(1 for d in details if d.get("status") != "skipped")

    # Output JSON result
    result = {
        "total": total,
        "passed": passed,
        "failed": failed,
        "details": details,
        "timestamp": datetime.utcnow().isoformat()
    }

    print(json.dumps(result, indent=2))

    # Exit with appropriate code
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    asyncio.run(main())
