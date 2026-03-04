"""
AADS E2E Full Cycle Test
로그인 → 프로젝트 생성 → 파이프라인 실행 → 상태 확인 → 비용 확인 → 메모리 확인
"""
import httpx
import asyncio
import time
import json
import sys
import os

# BUG FIX: 서버는 docker-compose 기준 호스트 포트 8100에서 실행 (8080:내부, 8100:외부)
BASE_URL = os.environ.get("AADS_BASE_URL", "http://localhost:8100/api/v1")

async def test_full_cycle():
    async with httpx.AsyncClient(timeout=300.0) as client:
        results = {}
        print("=" * 60)
        print("AADS E2E FULL CYCLE TEST")
        print("=" * 60)

        # 1. Health Check
        print("\n[1/8] Health Check...")
        r = await client.get(f"{BASE_URL}/health")
        results["health"] = r.status_code == 200
        health = r.json()
        print(f"  Status: {r.status_code}")
        print(f"  Graph Ready: {health.get('graph_ready', 'unknown')}")
        print(f"  Version: {health.get('version', 'unknown')}")

        # 2. Login
        print("\n[2/8] Login...")
        try:
            # BUG FIX: __from_env__ 플레이스홀더 → 환경변수에서 실제 값 읽기 (R-003)
            admin_password = os.environ.get("AADS_ADMIN_PASSWORD", "__from_env__")
            r = await client.post(f"{BASE_URL}/auth/login", json={
                "email": "admin@aads.dev",
                "password": admin_password
            })
            results["login"] = r.status_code == 200
            if r.status_code == 200:
                token = r.json().get("token", "")
                headers = {"Authorization": f"Bearer {token}"}
                print(f"  Login: SUCCESS")
            else:
                headers = {}
                print(f"  Login: FAILED ({r.status_code}) - continuing without auth")
        except Exception as e:
            results["login"] = False
            headers = {}
            print(f"  Login: ERROR - {e}")

        # 3. Chat API Test
        print("\n[3/8] Chat API Test...")
        r = await client.post(f"{BASE_URL}/chat", json={
            "message": "간단한 계산기 만들어줘",
            "sender": "e2e_test"
        })
        results["chat_api"] = r.status_code == 200
        if r.status_code == 200:
            chat_result = r.json()
            print(f"  Intent: {chat_result.get('intent')}")
            print(f"  Action: {chat_result.get('action')}")
        else:
            print(f"  Chat API: FAILED ({r.status_code})")

        # 4. Create Project
        print("\n[4/8] Create Project...")
        try:
            r = await client.post(f"{BASE_URL}/projects", json={
                "description": "E2E Test - Simple Python Calculator CLI"
            }, headers=headers)
            results["create_project"] = r.status_code in [200, 201]
            if r.status_code in [200, 201]:
                project = r.json()
                project_id = project.get("project_id", "")
                print(f"  Project ID: {project_id}")
                print(f"  Status: {project.get('status')}")
            else:
                project_id = ""
                print(f"  Create: FAILED ({r.status_code})")
                print(f"  Body: {r.text[:500]}")
        except Exception as e:
            results["create_project"] = False
            project_id = ""
            print(f"  Create: ERROR - {e}")

        # 5. Check Project Status (poll)
        print("\n[5/8] Monitor Pipeline...")
        stage = "unknown"
        if project_id:
            # BUG FIX: 프로젝트는 checkpoint에서 대기하므로 auto_run 직접 호출 (sync)
            print(f"  Running auto_run for project {project_id} (synchronous, up to 5min)...")
            try:
                ar_result = await client.post(
                    f"{BASE_URL}/projects/{project_id}/auto_run",
                    headers=headers
                )
                print(f"  auto_run: {ar_result.status_code} {ar_result.text[:200]}")
            except Exception as e:
                print(f"  auto_run error: {e}")

            # 최종 상태 확인
            try:
                r = await client.get(f"{BASE_URL}/projects/{project_id}/status", headers=headers)
                if r.status_code == 200:
                    status_data = r.json()
                    stage = status_data.get("checkpoint_stage", "unknown")
                    progress = status_data.get("progress_percent", 0)
                    print(f"  Final Stage: {stage}, Progress: {progress}%")
                else:
                    print(f"  Status check failed: {r.status_code}")
            except Exception as e:
                print(f"  Status check error: {e}")

            results["pipeline"] = stage == "completed" if project_id else False
        else:
            results["pipeline"] = False
            print("  Skipped (no project_id)")

        # 6. Check Costs
        print("\n[6/8] Check Costs...")
        if project_id:
            try:
                r = await client.get(f"{BASE_URL}/projects/{project_id}/costs", headers=headers)
                results["costs"] = r.status_code == 200
                if r.status_code == 200:
                    costs = r.json()
                    print(f"  Total Cost: ${costs.get('total_cost_usd', 'N/A')}")
                    print(f"  LLM Calls: {costs.get('llm_calls_count', 'N/A')}")
                else:
                    print(f"  Costs: FAILED ({r.status_code})")
            except Exception as e:
                results["costs"] = False
                print(f"  Costs: ERROR - {e}")
        else:
            results["costs"] = False

        # 7. Check Memory (Context API)
        print("\n[7/8] Check Memory...")
        try:
            # BUG FIX: __from_env__ 플레이스홀더 → 환경변수에서 실제 값 읽기 (R-003)
            # BUG FIX: /context/system/status → /context/system (올바른 라우트)
            monitor_key = os.environ.get("AADS_MONITOR_KEY", "__from_env__")
            r = await client.get(f"{BASE_URL}/context/system",
                headers={"X-Monitor-Key": monitor_key})
            results["memory"] = r.status_code == 200
            print(f"  Memory API: {r.status_code}")
        except Exception as e:
            results["memory"] = False
            print(f"  Memory: ERROR - {e}")

        # 8. Sandbox Health
        print("\n[8/8] Sandbox Health...")
        r = await client.get(f"{BASE_URL}/health")
        if r.status_code == 200:
            h = r.json()
            sandbox = h.get("sandbox", {})
            results["sandbox"] = sandbox.get("status") == "ok" if sandbox else False
            print(f"  Sandbox: {sandbox}")
        else:
            results["sandbox"] = False

        # === SUMMARY ===
        print("\n" + "=" * 60)
        print("E2E TEST RESULTS")
        print("=" * 60)
        total = len(results)
        passed = sum(1 for v in results.values() if v)
        for name, result in results.items():
            status = "PASS" if result else "FAIL"
            print(f"  [{status}] {name}")
        print(f"\n  TOTAL: {passed}/{total} PASSED")

        verdict = "LAUNCH READY" if passed >= 5 else "NOT READY"
        print(f"  VERDICT: {verdict}")
        print("=" * 60)

        return results

if __name__ == "__main__":
    asyncio.run(test_full_cycle())
