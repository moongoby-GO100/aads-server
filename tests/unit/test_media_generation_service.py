from __future__ import annotations

import base64
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.media_generation_service import MediaGenerationService


class _Acquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return _Acquire(self.conn)


class _Conn:
    def __init__(self):
        self.rows: dict[str, dict] = {}
        self.model_routes: dict[str, dict] = {}
        self.default_routes: dict[str, dict] = {}
        self.next_id = 1

    async def fetchrow(self, query: str, *args):
        if "FROM model_routing_preferences" in query:
            return self.default_routes.get(args[0])
        if "FROM llm_models" in query:
            if args:
                return self.model_routes.get(args[0])
            if "prefix_family" in query:
                return self.model_routes.get("imagen-4.0-*")
            return None
        if "INSERT INTO media_generation_jobs" in query:
            job_id = args[0]
            row = {
                "id": self.next_id,
                "job_id": job_id,
                "kind": args[1],
                "provider": args[2],
                "model_id": args[3],
                "prompt": args[4],
                "input_refs": json.loads(args[5]),
                "status": args[6],
                "result_uri": None,
                "result_path": None,
                "result_metadata": {},
                "error_message": None,
                "requested_by": args[7],
                "session_id": args[8],
                "created_at": None,
                "updated_at": None,
                "completed_at": None,
            }
            self.next_id += 1
            self.rows[job_id] = row
            return row
        if "UPDATE media_generation_jobs" in query:
            job_id = args[0]
            row = dict(self.rows[job_id])
            row.update(
                {
                    "status": args[1],
                    "result_uri": args[2] or row.get("result_uri"),
                    "result_path": args[3] or row.get("result_path"),
                    "result_metadata": json.loads(args[4]) if args[4] else {},
                    "error_message": args[5],
                }
            )
            self.rows[job_id] = row
            return row
        if "FROM media_generation_jobs" in query:
            return self.rows.get(args[0])
        return None


def _settings(openai_key: str = "", google_key: str = ""):
    return SimpleNamespace(OPENAI_API_KEY=openai_key, GOOGLE_API_KEY=google_key)


def test_ceo_model_strings_are_recognized():
    svc = MediaGenerationService(settings_obj=_settings())

    assert svc.recognize_model("gpt-image-2") == {
        "kind": "image",
        "provider": "openai",
        "model_id": "gpt-image-2",
    }
    assert svc.recognize_model("imagen-4.0-fast-generate-001")["provider"] == "google"
    assert svc.recognize_model("imagen-4.0-custom-preview")["provider"] == "google"
    assert svc._route_supported("image", "google", "imagen-4.0-custom-preview") is True
    assert svc.recognize_model("gemini-3.1-flash-image-preview")["kind"] == "image"
    assert svc.recognize_model("sora-2-pro")["provider"] == "openai"
    assert svc.recognize_model("veo-3.1-generate-preview")["kind"] == "video"
    assert svc.recognize_model("kling-v2")["provider"] == "kling"
    assert svc._route_supported("image", "kling", "kling-v2-1") is True
    assert svc._route_supported("video", "kling", "kling-v3") is True
    assert svc.recognize_model("gpt-5.5")["provider"] == "codex"
    assert svc.recognize_model("claude-opus-4-8")["provider"] == "anthropic"
    assert svc.recognize_model("gemini-3.1-pro-preview")["provider"] == "gemini"


def test_kling_jwt_is_hs256_access_secret_token():
    token = MediaGenerationService._build_kling_jwt("access-key", "secret-key")

    parts = token.split(".")
    assert len(parts) == 3
    header = json.loads(base64.urlsafe_b64decode(parts[0] + "=="))
    payload = json.loads(base64.urlsafe_b64decode(parts[1] + "=="))
    assert header["alg"] == "HS256"
    assert payload["iss"] == "access-key"
    assert payload["exp"] > payload["nbf"]


@pytest.mark.asyncio
async def test_generate_video_creates_job_and_marks_not_configured():
    conn = _Conn()
    svc = MediaGenerationService(settings_obj=_settings(), pool_provider=lambda: _Pool(conn))

    result = await svc.generate_video("make a product clip", model_id="sora-2", session_id="s1")

    assert result["error"] == "NOT_CONFIGURED"
    assert result["status"] == "failed"
    assert result["provider"] == "openai"
    assert result["job_id"] in conn.rows
    row = conn.rows[result["job_id"]]
    assert row["kind"] == "video"
    assert row["status"] == "failed"
    assert row["result_metadata"]["error_code"] == "NOT_CONFIGURED"


@pytest.mark.asyncio
async def test_job_status_transition_and_video_status():
    conn = _Conn()
    svc = MediaGenerationService(settings_obj=_settings(), pool_provider=lambda: _Pool(conn))
    job = await svc._insert_job(
        kind="video",
        provider="openai",
        model_id="sora-2",
        prompt="clip",
        input_refs={"ratio": "16:9"},
    )

    running = await svc.update_job_status(job["job_id"], "running")
    done = await svc.update_job_status(
        job["job_id"],
        "succeeded",
        result_uri="data:video/mp4;base64,AAAA",
        result_metadata={"provider_job_id": "pv_1"},
    )
    status = await svc.video_status(job["job_id"])

    assert running["status"] == "running"
    assert done["status"] == "succeeded"
    assert status["job_id"] == job["job_id"]
    assert status["status"] == "succeeded"
    assert status["result_metadata"]["provider_job_id"] == "pv_1"


@pytest.mark.asyncio
async def test_generate_image_not_configured_is_graceful():
    conn = _Conn()
    svc = MediaGenerationService(settings_obj=_settings(), pool_provider=lambda: _Pool(conn))

    result = await svc.generate_image("a clean product photo", model_id="gpt-image-2")

    assert result["error"] == "NOT_CONFIGURED"
    assert result["status"] == "failed"
    assert result["model_id"] == "gpt-image-2"
    assert conn.rows[result["job_id"]]["status"] == "failed"


@pytest.mark.asyncio
async def test_route_precedence_explicit_db_default_then_fallback():
    conn = _Conn()
    conn.default_routes["image"] = {
        "route_key": "image",
        "provider": "google",
        "model_id": "imagen-4.0-generate-001",
        "execution_model_id": "imagen-4.0-generate-001",
        "is_enabled": True,
        "is_selectable": True,
        "notes": "db default",
    }
    svc = MediaGenerationService(
        settings_obj=_settings(openai_key="sk-test", google_key="google-test"),
        pool_provider=lambda: _Pool(conn),
    )

    db_route = await svc.resolve_route("image")
    explicit_route = await svc.resolve_route("image", model_id="gpt-image-2")
    fallback_route = await MediaGenerationService(
        settings_obj=_settings(openai_key="sk-test", google_key=""),
        pool_provider=lambda: _Pool(_Conn()),
    ).resolve_route("image")

    assert db_route.source == "db_default"
    assert db_route.provider == "google"
    assert db_route.model_id == "imagen-4.0-generate-001"
    assert explicit_route.source == "explicit"
    assert explicit_route.provider == "openai"
    assert explicit_route.model_id == "gpt-image-2"
    assert fallback_route.source == "fallback"
    assert fallback_route.provider == "openai"
    assert fallback_route.model_id == "gpt-image-2"


@pytest.mark.asyncio
async def test_imagen_prefix_explicit_model_uses_db_family_without_replacing_model():
    conn = _Conn()
    conn.model_routes["imagen-4.0-*"] = {
        "provider": "google",
        "model_id": "imagen-4.0-generate-001",
        "execution_model_id": "imagen-4.0-generate-001",
        "is_enabled": True,
        "is_selectable": True,
        "metadata": {"routing_note": "prefix family"},
        "capabilities": {"prefix_family": "imagen-4.0-*"},
    }
    svc = MediaGenerationService(
        settings_obj=_settings(google_key="google-test"),
        pool_provider=lambda: _Pool(conn),
    )

    route = await svc.resolve_route("image", model_id="imagen-4.0-custom-preview")

    assert route.source == "explicit"
    assert route.provider == "google"
    assert route.model_id == "imagen-4.0-custom-preview"
    assert route.supported is True
    assert route.availability == "available"


@pytest.mark.asyncio
async def test_disabled_db_default_returns_clear_status_before_credentials():
    conn = _Conn()
    conn.default_routes["image"] = {
        "route_key": "image",
        "provider": "openai",
        "model_id": "gpt-image-2",
        "execution_model_id": "gpt-image-2",
        "is_enabled": False,
        "is_selectable": True,
        "notes": "disabled in admin",
    }
    svc = MediaGenerationService(settings_obj=_settings(), pool_provider=lambda: _Pool(conn))

    result = await svc.generate_image("a clean product photo")

    assert result["error"] == "MODEL_DISABLED"
    assert result["availability"] == "disabled"
    assert result["route_source"] == "db_default"
    assert result["message"] == "disabled in admin"


@pytest.mark.asyncio
async def test_db_default_without_credentials_returns_not_configured():
    conn = _Conn()
    conn.default_routes["video"] = {
        "route_key": "video",
        "provider": "openai",
        "model_id": "sora-2",
        "execution_model_id": "sora-2",
        "is_enabled": True,
        "is_selectable": True,
        "notes": "default video",
    }
    svc = MediaGenerationService(settings_obj=_settings(), pool_provider=lambda: _Pool(conn))

    result = await svc.generate_video("make a product clip")

    assert result["error"] == "NOT_CONFIGURED"
    assert result["model_id"] == "sora-2"
    assert result["availability"] == "not_configured"
    assert result["route_source"] == "db_default"


@pytest.mark.asyncio
async def test_generate_kling_video_submits_provider_job(monkeypatch):
    conn = _Conn()
    svc = MediaGenerationService(settings_obj=_settings(), pool_provider=lambda: _Pool(conn))

    async def fake_credentials():
        return "access-key", "secret-key"

    async def fake_request(method, endpoint, payload=None):
        assert method == "POST"
        assert endpoint == "/v1/videos/text2video"
        assert payload["model_name"] == "kling-v2"
        return {
            "code": 0,
            "request_id": "req_1",
            "data": {"task_id": "kling_task_1", "task_status": "submitted"},
        }

    monkeypatch.setattr(svc, "_get_kling_credentials", fake_credentials)
    monkeypatch.setattr(svc, "_kling_request", fake_request)

    result = await svc.generate_video("make a product clip", model_id="kling-v2")

    assert result["status"] == "queued"
    assert result["provider"] == "kling"
    assert result["provider_task_id"] == "kling_task_1"
    assert conn.rows[result["job_id"]]["result_metadata"]["provider_endpoint"] == "/v1/videos/text2video"


@pytest.mark.asyncio
async def test_default_image_route_externalizes_base64_result(monkeypatch, tmp_path: Path):
    conn = _Conn()
    svc = MediaGenerationService(
        settings_obj=_settings(openai_key="sk-test", google_key=""),
        pool_provider=lambda: _Pool(conn),
    )
    monkeypatch.setenv("AADS_MEDIA_STATIC_DIR", str(tmp_path))
    payload = base64.b64encode(b"image-bytes").decode()

    async def fake_generate(prompt, size):
        return {"url": f"data:image/png;base64,{payload}", "provider": "gpt-image-1", "prompt": prompt}

    fake_module = SimpleNamespace(image_service=SimpleNamespace(generate=fake_generate))
    monkeypatch.setitem(sys.modules, "app.services.image_service", fake_module)

    result = await svc.generate_image("a clean product photo")

    assert result["status"] == "succeeded"
    assert result["provider"] == "gpt-image-1"
    assert result["url"].startswith("/static/media/generated/image/")
    assert conn.rows[result["job_id"]]["provider"] == "openai"
    assert conn.rows[result["job_id"]]["result_uri"].startswith("/static/media/generated/image/")
    assert not conn.rows[result["job_id"]]["result_uri"].startswith("data:")
    assert Path(conn.rows[result["job_id"]]["result_path"]).read_bytes() == b"image-bytes"
    assert conn.rows[result["job_id"]]["result_metadata"]["base64_externalized"] is True


@pytest.mark.asyncio
async def test_video_download_saves_data_uri(tmp_path: Path):
    conn = _Conn()
    svc = MediaGenerationService(
        settings_obj=_settings(),
        pool_provider=lambda: _Pool(conn),
        output_dir=tmp_path,
    )
    payload = base64.b64encode(b"video-bytes").decode()
    job = await svc._insert_job(
        kind="video",
        provider="openai",
        model_id="sora-2",
        prompt="clip",
        status="queued",
    )
    await svc.update_job_status(
        job["job_id"],
        "succeeded",
        result_uri=f"data:video/mp4;base64,{payload}",
    )

    result = await svc.video_download(job["job_id"])

    assert result["status"] == "succeeded"
    path = Path(result["result_path"])
    assert path.exists()
    assert path.read_bytes() == b"video-bytes"
    assert str(path).startswith(str(tmp_path))


def test_media_generation_migration_contains_idempotent_job_table():
    sql = Path("migrations/088_media_generation_jobs.sql").read_text()
    local_sql = Path("migrations/095_local_multimodal_model_bridge.sql").read_text()

    assert "BEGIN;" in sql
    assert "COMMIT;" in sql
    assert "CREATE TABLE IF NOT EXISTS media_generation_jobs" in sql
    assert "job_id TEXT NOT NULL UNIQUE" in sql
    assert "CHECK (kind IN ('image', 'edit_image', 'video'))" in sql
    assert "CREATE INDEX IF NOT EXISTS idx_media_generation_jobs_job_id" in sql
    assert "DROP CONSTRAINT IF EXISTS media_generation_jobs_kind_chk" in local_sql
    assert "'music', 'model_3d'" in local_sql


def test_model_routing_migration_seeds_ceo_models_and_preferences():
    sql = Path("migrations/089_model_routing_preferences.sql").read_text()
    hardening_sql = Path("migrations/090_media_llm_routing_admin_hardening.sql").read_text()
    kling_sql = Path("migrations/103_kling_media_models.sql").read_text()

    assert "CREATE TABLE IF NOT EXISTS model_routing_preferences" in sql
    for model_id in (
        "gpt-image-2",
        "imagen-4.0-generate-001",
        "gemini-3.1-flash-image-preview",
        "sora-2",
        "sora-2-pro",
        "veo-3.1-generate-preview",
        "gpt-5.5",
        "claude-opus-4-8",
        "gemini-3.1-pro-preview",
        "kling-2.0",
        "kling-v2",
        "kling-v2-1",
        "kling-v3",
    ):
        assert model_id in sql + hardening_sql + kling_sql
    assert "ON CONFLICT (route_key, provider, model_id)" in sql
    assert "CREATE TABLE IF NOT EXISTS runner_model_config" in hardening_sql
    assert "ON CONFLICT (size) DO NOTHING" in hardening_sql
