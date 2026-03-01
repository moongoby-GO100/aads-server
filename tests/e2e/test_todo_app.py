"""
투두 앱 E2E 테스트.
실행 조건: 모든 API 키 세팅 필요 + 서버 기동 중.
"""
import os
import pytest
import httpx

BASE_URL = os.getenv("AADS_TEST_URL", "http://localhost:8080/api/v1")


@pytest.mark.asyncio
async def test_health():
    async with httpx.AsyncClient() as client:
        r = await client.get(f"{BASE_URL}/health")
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "ok"
        assert data["graph_ready"] is True


@pytest.mark.asyncio
async def test_todo_app_flow():
    async with httpx.AsyncClient(timeout=120.0) as client:
        # 1. 프로젝트 생성
        r = await client.post(
            f"{BASE_URL}/projects",
            json={"description": "간단한 투두 앱을 만들어줘. Python으로 CLI 기반 CRUD."},
        )
        assert r.status_code == 200
        data = r.json()
        project_id = data["project_id"]
        assert data["status"] == "checkpoint_pending"
        assert data["interrupt_payload"] is not None, "PM interrupt 없음 — 체크포인트 미작동"

        # interrupt payload 구조 확인
        payload = data["interrupt_payload"]
        assert "value" in payload
        assert payload["value"].get("stage") == "requirements"
        assert "task_spec" in payload["value"]

        # 2. 요구사항 승인
        r = await client.post(
            f"{BASE_URL}/projects/{project_id}/checkpoint",
            json={"action": "approve"},
        )
        assert r.status_code == 200
        data = r.json()

        # 3. 결과 확인
        assert len(data.get("generated_files", [])) > 0, "코드가 생성되지 않음"
        assert data["total_cost_usd"] < 10.0, f"작업 비용 초과: ${data['total_cost_usd']}"

        # 4. 상태 조회
        r = await client.get(f"{BASE_URL}/projects/{project_id}")
        assert r.status_code == 200
        state = r.json()

        print(f"\n=== E2E Test Results ===")
        print(f"Total LLM calls: {state['llm_calls_count']}")
        print(f"Total cost: ${state['total_cost_usd']:.4f}")
        print(f"Generated files: {len(state['generated_files'])}")
        print(f"Checkpoint stage: {state['checkpoint_stage']}")
        if state["generated_files"]:
            print(f"Main file preview:\n{state['generated_files'][0]['content'][:300]}...")

        assert state["llm_calls_count"] <= 15, f"R-012 위반: {state['llm_calls_count']} calls"
