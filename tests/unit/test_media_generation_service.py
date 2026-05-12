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
        self.next_id = 1

    async def fetchrow(self, query: str, *args):
        if "FROM llm_models" in query:
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
    assert svc.recognize_model("gemini-3.1-flash-image-preview")["kind"] == "image"
    assert svc.recognize_model("sora-2-pro")["provider"] == "openai"
    assert svc.recognize_model("veo-3.1-generate-preview")["kind"] == "video"
    assert svc.recognize_model("gpt-5.5")["provider"] == "codex"
    assert svc.recognize_model("claude-opus-4-7")["provider"] == "anthropic"
    assert svc.recognize_model("gemini-3.1-pro-preview")["provider"] == "gemini"


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
async def test_default_image_route_preserves_openai_fallback_when_google_missing(monkeypatch):
    conn = _Conn()
    svc = MediaGenerationService(
        settings_obj=_settings(openai_key="sk-test", google_key=""),
        pool_provider=lambda: _Pool(conn),
    )

    async def fake_generate(prompt, size):
        return {"url": "data:image/png;base64,AAA", "provider": "gpt-image-1", "prompt": prompt}

    fake_module = SimpleNamespace(image_service=SimpleNamespace(generate=fake_generate))
    monkeypatch.setitem(sys.modules, "app.services.image_service", fake_module)

    result = await svc.generate_image("a clean product photo")

    assert result["status"] == "succeeded"
    assert result["provider"] == "gpt-image-1"
    assert conn.rows[result["job_id"]]["provider"] == "openai"


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

    assert "BEGIN;" in sql
    assert "COMMIT;" in sql
    assert "CREATE TABLE IF NOT EXISTS media_generation_jobs" in sql
    assert "job_id TEXT NOT NULL UNIQUE" in sql
    assert "CHECK (kind IN ('image', 'edit_image', 'video'))" in sql
    assert "CREATE INDEX IF NOT EXISTS idx_media_generation_jobs_job_id" in sql
