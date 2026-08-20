import os
from pathlib import Path

os.environ.setdefault("JWT_SECRET_KEY", "test-jwt-secret-for-yeoljeong-isolation")

from app import yeoljeong_main
from app.services import yeoljeong_finance_service as service


def test_yeoljeong_app_excludes_aads_core_routes() -> None:
    routes = {getattr(route, "path", "") for route in yeoljeong_main.app.routes}

    assert "/health/live" in routes
    assert "/api/v1/health/live" in routes
    assert any(path.startswith("/api/v1/yeoljeong-finance") for path in routes)
    assert not any(path.startswith("/api/v1/chat") for path in routes)
    assert not any(path.startswith("/api/v1/pipeline") for path in routes)
    assert not any(path.startswith("/api/v1/admin") for path in routes)
    assert not any(path.startswith("/api/v1/mcp") for path in routes)


def test_yeoljeong_compose_uses_dedicated_runtime_boundaries() -> None:
    compose = (Path(__file__).resolve().parents[2] / "docker-compose.prod.yml").read_text(encoding="utf-8")

    assert "container_name: yeoljeong-finance" in compose
    assert "container_name: yeoljeong-finance-worker" in compose
    assert 'command: ["uvicorn", "app.yeoljeong_main:app", "--host", "0.0.0.0", "--port", "8080"]' in compose
    assert "YEOLJEONG_FINANCE_DATABASE_URL=${YEOLJEONG_FINANCE_DATABASE_URL:-" in compose
    assert "YEOLJEONG_FINANCE_DATA_DIR=/app/yeoljeong-data" in compose
    assert "/root/aads/aads-server/app/data/yeoljeong_finance:/app/yeoljeong-data:rw" in compose
    worker_compose = compose.split("  yeoljeong-finance-worker:", 1)[1].split("  aads-litellm:", 1)[0]
    assert "/root/aads/aads-server/.active_port:/app/.active_port:ro" in worker_compose
    assert "/root/aads/aads-server/.active_container:/app/.active_container:ro" in worker_compose
    assert "YEOLJEONG_DELIVERY_PC_AGENT_ID=${YEOLJEONG_DELIVERY_PC_AGENT_ID:-" in worker_compose
    assert "YEOLJEONG_AUTO_COLLECT_INTERVAL_SECONDS" in compose


def test_delivery_public_collection_status_contract() -> None:
    expected = {
        "queued": "queued",
        "running": "running",
        "succeeded": "succeeded",
        "completed": "succeeded",
        "no_records": "partial",
        "authenticated_no_rows": "partial",
        "credential_required": "action_required",
        "portal_action_required": "action_required",
        "upload_required": "action_required",
        "stale": "failed",
        "error": "failed",
        "unexpected": "failed",
        "": "failed",
    }

    for raw, public in expected.items():
        assert service._delivery_public_collection_status(raw) == public

    public_values = {service._delivery_public_collection_status(raw) for raw in expected}
    assert public_values <= service.DELIVERY_COLLECTION_STATUSES
