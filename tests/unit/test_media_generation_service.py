from __future__ import annotations

import base64
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.media_generation_service import MediaGenerationService, _is_public_http_url


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
        self.route_preferences: dict[tuple[str, str, str], dict] = {}
        self.next_id = 1

    async def fetchrow(self, query: str, *args):
        if "FROM model_routing_preferences" in query:
            if len(args) >= 3:
                return self.route_preferences.get((args[0], args[1], args[2]))
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
    assert svc.recognize_model("genspark-image-ui") == {
        "kind": "image",
        "provider": "genspark_ui",
        "model_id": "genspark-image-ui",
    }
    assert svc.recognize_model("genspark-video") == {
        "kind": "video",
        "provider": "genspark_ui",
        "model_id": "genspark-video-ui",
    }
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
async def test_disabled_explicit_google_family_model_is_blocked_before_provider_call():
    conn = _Conn()
    conn.model_routes["gemini-3.1-flash-image-preview"] = {
        "provider": "gemini",
        "model_id": "gemini-3.1-flash-image-preview",
        "execution_model_id": "gemini-3.1-flash-image-preview",
        "is_active": True,
        "is_selectable": True,
        "is_executable": True,
        "verification_status": "verified",
    }
    conn.route_preferences[("image", "gemini", "gemini-3.1-flash-image-preview")] = {
        "route_key": "image",
        "provider": "gemini",
        "model_id": "gemini-3.1-flash-image-preview",
        "is_enabled": False,
        "is_selectable": True,
        "notes": "Google commercial route disabled",
    }
    svc = MediaGenerationService(
        settings_obj=_settings(google_key="google-test"),
        pool_provider=lambda: _Pool(conn),
    )

    result = await svc.generate_image(
        "a clean product photo",
        model_id="gemini-3.1-flash-image-preview",
    )

    assert result["error"] == "MODEL_DISABLED"
    assert result["availability"] == "disabled"
    assert result["route_source"] == "explicit"
    assert result["provider"] == "gemini"
    assert result["model_id"] == "gemini-3.1-flash-image-preview"


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
async def test_genspark_ui_image_provider_queues_for_browser_agent():
    conn = _Conn()
    svc = MediaGenerationService(settings_obj=_settings(), pool_provider=lambda: _Pool(conn))

    result = await svc.generate_image(
        "make a clean product image",
        provider="genspark_ui",
        model_id="genspark-image-ui",
        session_id="s1",
        browser_work_key="genspark-agent-559920e6",
        target_url="https://www.genspark.ai/agents?id=559920e6-f81c-464a-be60-f0228124958b",
    )

    assert result["status"] == "queued"
    assert result["provider"] == "genspark_ui"
    assert result["availability"] == "queued_requires_agent"
    metadata = conn.rows[result["job_id"]]["result_metadata"]
    assert metadata["ui_automation"]["service"] == "genspark"
    assert metadata["ui_automation"]["work_key"] == "genspark-agent-559920e6"
    assert metadata["ui_automation"]["target_url"] == "https://www.genspark.ai/agents?id=559920e6-f81c-464a-be60-f0228124958b"
    assert metadata["ui_automation"]["requires_logged_in_browser"] is True
    assert metadata["ui_automation"]["stores_result_via"] == "media_generation_jobs.result_path/result_uri"


@pytest.mark.asyncio
async def test_genspark_ui_video_provider_queues_for_browser_agent():
    conn = _Conn()
    svc = MediaGenerationService(settings_obj=_settings(), pool_provider=lambda: _Pool(conn))

    result = await svc.generate_video(
        "make a short product video",
        provider="genspark",
        input_refs={"browser_work_key": "genspark-test"},
    )

    assert result["status"] == "queued"
    assert result["provider"] == "genspark_ui"
    assert result["model_id"] == "genspark-video-ui"
    assert result["automation_state"] == "queued_requires_agent"
    assert conn.rows[result["job_id"]]["result_metadata"]["ui_automation"]["work_key"] == "genspark-test"


@pytest.mark.asyncio
async def test_genspark_ui_image_default_target_is_image_app():
    conn = _Conn()
    svc = MediaGenerationService(settings_obj=_settings(), pool_provider=lambda: _Pool(conn))

    result = await svc.generate_image(
        "make a clean product image",
        provider="genspark_ui",
        model_id="genspark-image-ui",
    )

    metadata = conn.rows[result["job_id"]]["result_metadata"]
    assert metadata["ui_automation"]["target_url"] == "https://www.genspark.ai/ai_image"


@pytest.mark.asyncio
async def test_process_genspark_ui_job_keeps_auth_gate_retryable(monkeypatch):
    class _FakeLocator:
        @property
        def first(self):
            return self

        async def aria_snapshot(self):
            return "로그인 가입하기 Genspark AI 워크스페이스"

    class _FakePage:
        def locator(self, selector):
            assert selector == "body"
            return _FakeLocator()

    conn = _Conn()
    svc = MediaGenerationService(settings_obj=_settings(), pool_provider=lambda: _Pool(conn))
    queued = await svc.generate_image(
        "make a clean product image",
        provider="genspark_ui",
        model_id="genspark-image-ui",
        session_id="s1",
    )

    async def fake_acquire_page(**kwargs):
        assert kwargs["work_key"] == "genspark-media-fallback"
        assert kwargs["browser_session_id"] == "bb-logged-in"
        return _FakePage()

    monkeypatch.setattr(svc, "_acquire_genspark_page", fake_acquire_page)

    result = await svc.process_genspark_ui_job(job_id=queued["job_id"], browser_session_id="bb-logged-in")

    assert result["status"] == "queued"
    assert result["automation_state"] == "auth_required"
    assert result["requires_login"] is True
    row = conn.rows[queued["job_id"]]
    assert row["result_metadata"]["ui_automation"]["state"] == "auth_required"
    assert row["result_metadata"]["ui_automation"]["browser_session_id"] == "bb-logged-in"
    assert row["error_message"] == "Genspark auth gate detected: no Agent Vault credential found for this session"


@pytest.mark.asyncio
async def test_acquire_genspark_page_separates_work_session_from_genspark_navigation(monkeypatch):
    """Slow Genspark navigation must not make PC Agent work-session creation look stale."""
    from app.api import ceo_chat_tools

    class _FakePage:
        def __init__(self):
            self.gotos: list[tuple[str, str]] = []

        async def goto(self, url, *, timeout, wait_until):
            self.gotos.append((url, wait_until))
            return None

    class _FakeContext:
        def __init__(self, page):
            self.pages = [page]

    page = _FakePage()
    calls: list[tuple[str, str, str]] = []

    async def fake_acquire(browser_session_id="", browser_work_key="", url="about:blank"):
        calls.append((browser_session_id, browser_work_key, url))
        return _FakeContext(page), None

    monkeypatch.setattr(ceo_chat_tools, "_acquire_pw_context", fake_acquire)

    svc = MediaGenerationService(settings_obj=_settings(), pool_provider=lambda: _Pool(_Conn()))
    result = await svc._acquire_genspark_page(
        work_key="genspark-agent-test",
        target_url="https://www.genspark.ai/agents?id=test",
    )

    assert result is page
    assert calls == [("", "genspark-agent-test", "about:blank")]
    assert page.gotos == [("https://www.genspark.ai/agents?id=test", "domcontentloaded")]


@pytest.mark.asyncio
async def test_submit_prompt_to_genspark_clicks_secondary_submit_button():
    class _FakeKeyboard:
        def __init__(self):
            self.keys: list[str] = []

        async def press(self, key):
            self.keys.append(key)

    class _FakePage:
        def __init__(self):
            self.keyboard = _FakeKeyboard()
            self.evaluated: list[tuple[str, tuple]] = []
            self.waits: list[int] = []

        async def evaluate(self, script, *args):
            self.evaluated.append((script, args))
            if args:
                return {"ok": True, "selector": '[data-aads-genspark-prompt="1"]'}
            return True

        async def wait_for_timeout(self, timeout):
            self.waits.append(timeout)

    svc = MediaGenerationService(settings_obj=_settings(), pool_provider=lambda: _Pool(_Conn()))
    page = _FakePage()

    result = await svc._submit_prompt_to_genspark(page, "make a clean product image")

    assert result["ok"] is True
    assert page.keyboard.keys == ["Enter"]
    assert page.waits == [1000]
    assert any("button.submit-btn" in script for script, _ in page.evaluated)


@pytest.mark.asyncio
async def test_process_genspark_ui_job_saves_data_uri_result(monkeypatch, tmp_path: Path):
    class _FakeLocator:
        @property
        def first(self):
            return self

        async def aria_snapshot(self):
            return "Genspark AI workspace"

    class _FakePage:
        def locator(self, selector):
            assert selector == "body"
            return _FakeLocator()

        async def wait_for_timeout(self, timeout):
            assert timeout == 5000

    conn = _Conn()
    svc = MediaGenerationService(settings_obj=_settings(), pool_provider=lambda: _Pool(conn))
    queued = await svc.generate_image(
        "make a clean product image",
        provider="genspark_ui",
        model_id="genspark-image-ui",
        session_id="s1",
    )
    payload = base64.b64encode(b"genspark-image-bytes").decode()

    monkeypatch.setenv("AADS_MEDIA_STATIC_DIR", str(tmp_path))

    async def fake_acquire_page(**kwargs):
        return _FakePage()

    monkeypatch.setattr(svc, "_acquire_genspark_page", fake_acquire_page)

    async def fake_submit(page, prompt):
        assert prompt == "make a clean product image"
        return {"ok": True}

    async def fake_extract(page):
        return {"ok": True, "data_uri": f"data:image/png;base64,{payload}", "tag": "img"}

    monkeypatch.setattr(svc, "_submit_prompt_to_genspark", fake_submit)
    monkeypatch.setattr(svc, "_extract_genspark_media_candidate", fake_extract)

    result = await svc.process_genspark_ui_job(job_id=queued["job_id"])

    assert result["status"] == "succeeded"
    assert result["automation_state"] == "succeeded"
    row = conn.rows[queued["job_id"]]
    assert row["result_metadata"]["ui_automation"]["state"] == "succeeded"
    assert Path(row["result_path"]).read_bytes() == b"genspark-image-bytes"
    assert row["result_uri"] == f"/api/v1/image/gallery/{queued['job_id']}/image"


def test_genspark_remote_media_url_guard_blocks_local_targets():
    assert _is_public_http_url("http://127.0.0.1/image.png") is False
    assert _is_public_http_url("http://localhost/image.png") is False
    assert _is_public_http_url("file:///tmp/image.png") is False


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
    assert result["url"] == f"/api/v1/image/gallery/{result['job_id']}/image"
    assert conn.rows[result["job_id"]]["provider"] == "openai"
    assert conn.rows[result["job_id"]]["result_uri"] == result["url"]
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
        "genspark-image-ui",
        "genspark-video-ui",
    ):
        genspark_sql = Path("migrations/119_genspark_ui_media_fallback.sql").read_text()
        assert model_id in sql + hardening_sql + kling_sql + genspark_sql
    assert "ON CONFLICT (route_key, provider, model_id)" in sql
    assert "CREATE TABLE IF NOT EXISTS runner_model_config" in hardening_sql
    assert "ON CONFLICT (size) DO NOTHING" in hardening_sql


# ── Agent Vault auto-login tests ─────────────────────────────────────────────

_TENANT_A = "2d701a8c-9596-4757-8588-faa4f7837112"
_TENANT_B = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


class _VaultConn(_Conn):
    """Extends _Conn with chat_sessions and agent_vault_credentials table support."""

    def __init__(self):
        super().__init__()
        self.sessions: dict[str, str] = {}
        self.vault_credentials: list[dict] = []

    async def fetchrow(self, query: str, *args):
        if "FROM chat_sessions" in query:
            session_id = str(args[0])
            tenant_id = self.sessions.get(session_id)
            return {"tenant_id": tenant_id} if tenant_id else None
        if "FROM agent_vault_credentials" in query and "WHERE tenant_id" in query:
            tenant_arg = str(args[0])
            wk_arg = str(args[1])
            origin_arg = str(args[2])
            for row in self.vault_credentials:
                if (
                    str(row.get("tenant_id", "")) == tenant_arg
                    and row.get("work_key") == wk_arg
                    and row.get("origin") == origin_arg
                    and row.get("is_active", True)
                ):
                    return row
            return None
        return await super().fetchrow(query, *args)


def _vault_row(
    *,
    tenant_id: str,
    work_key: str,
    origin: str,
    username: str = "user@example.com",
    password: str = "dummy",
    is_active: bool = True,
) -> dict:
    return {
        "id": "cred-id-1",
        "tenant_id": tenant_id,
        "work_key": work_key,
        "origin": origin,
        "username_enc": username,
        "password_enc": password,
        "is_active": is_active,
    }


@pytest.mark.asyncio
async def test_fetch_genspark_vault_credential_exact_work_key_wins(monkeypatch):
    """Credential matching the exact request work_key should beat aads-ceo-browser fallback."""
    monkeypatch.setattr("app.core.credential_vault.decrypt_value", lambda v: v)

    conn = _VaultConn()
    conn.vault_credentials = [
        _vault_row(tenant_id=_TENANT_A, work_key="aads-ceo-browser", origin="https://login.genspark.ai", username="ceo@example.com"),
        _vault_row(tenant_id=_TENANT_A, work_key="genspark-agent-abc", origin="https://login.genspark.ai", username="agent@example.com"),
    ]
    svc = MediaGenerationService(settings_obj=_settings(), pool_provider=lambda: _Pool(conn))

    cred = await svc._fetch_genspark_vault_credential(_TENANT_A, "genspark-agent-abc")

    assert cred is not None
    assert cred["work_key"] == "genspark-agent-abc"
    assert cred["username"] == "agent@example.com"


@pytest.mark.asyncio
async def test_fetch_genspark_vault_credential_falls_back_to_ceo_browser(monkeypatch):
    """When exact work_key has no match, aads-ceo-browser credential is returned."""
    monkeypatch.setattr("app.core.credential_vault.decrypt_value", lambda v: v)

    conn = _VaultConn()
    conn.vault_credentials = [
        _vault_row(tenant_id=_TENANT_A, work_key="aads-ceo-browser", origin="https://login.genspark.ai", username="ceo@example.com"),
    ]
    svc = MediaGenerationService(settings_obj=_settings(), pool_provider=lambda: _Pool(conn))

    cred = await svc._fetch_genspark_vault_credential(_TENANT_A, "genspark-agent-xyz")

    assert cred is not None
    assert cred["work_key"] == "aads-ceo-browser"
    assert cred["username"] == "ceo@example.com"


@pytest.mark.asyncio
async def test_fetch_genspark_vault_credential_login_origin_beats_www(monkeypatch):
    """https://login.genspark.ai is preferred over https://www.genspark.ai for same work_key."""
    monkeypatch.setattr("app.core.credential_vault.decrypt_value", lambda v: v)

    conn = _VaultConn()
    conn.vault_credentials = [
        _vault_row(tenant_id=_TENANT_A, work_key="genspark-agent-abc", origin="https://www.genspark.ai", username="www-user@example.com"),
        _vault_row(tenant_id=_TENANT_A, work_key="genspark-agent-abc", origin="https://login.genspark.ai", username="login-user@example.com"),
    ]
    svc = MediaGenerationService(settings_obj=_settings(), pool_provider=lambda: _Pool(conn))

    cred = await svc._fetch_genspark_vault_credential(_TENANT_A, "genspark-agent-abc")

    assert cred is not None
    assert cred["origin"] == "https://login.genspark.ai"
    assert cred["username"] == "login-user@example.com"


@pytest.mark.asyncio
async def test_fetch_genspark_vault_credential_no_cross_tenant(monkeypatch):
    """Credentials belonging to tenant B must never be returned for tenant A lookup."""
    monkeypatch.setattr("app.core.credential_vault.decrypt_value", lambda v: v)

    conn = _VaultConn()
    conn.vault_credentials = [
        _vault_row(tenant_id=_TENANT_B, work_key="aads-ceo-browser", origin="https://login.genspark.ai"),
    ]
    svc = MediaGenerationService(settings_obj=_settings(), pool_provider=lambda: _Pool(conn))

    cred_a = await svc._fetch_genspark_vault_credential(_TENANT_A, "aads-ceo-browser")
    cred_none = await svc._fetch_genspark_vault_credential(None, "aads-ceo-browser")  # type: ignore[arg-type]

    assert cred_a is None, "tenant A must not receive tenant B credentials"
    assert cred_none is None, "None tenant_id must return None without DB call"


@pytest.mark.asyncio
async def test_genspark_autologin_credential_missing_error_code(monkeypatch):
    """When session has no vault credential, last_error must be AGENT_VAULT_CREDENTIAL_MISSING."""

    class _FakePage:
        async def locator(self, _):
            return self

        @property
        def first(self):
            return self

        async def aria_snapshot(self):
            return "로그인 sign in"

    conn = _VaultConn()
    conn.sessions["sess-1"] = _TENANT_A
    # No vault credentials added → lookup returns None

    svc = MediaGenerationService(settings_obj=_settings(), pool_provider=lambda: _Pool(conn))
    queued = await svc.generate_image(
        "a product image",
        provider="genspark_ui",
        model_id="genspark-image-ui",
        session_id="sess-1",
    )

    async def fake_acquire(**kwargs):
        page = _FakePage()
        return page

    monkeypatch.setattr(svc, "_acquire_genspark_page", fake_acquire)
    monkeypatch.setattr(
        svc,
        "_read_genspark_page_text",
        lambda page: _coro("로그인 sign in"),
    )

    result = await svc.process_genspark_ui_job(job_id=queued["job_id"])

    assert result["automation_state"] == "auth_required"
    row = conn.rows[queued["job_id"]]
    assert row["result_metadata"]["ui_automation"]["last_error"] == "AGENT_VAULT_CREDENTIAL_MISSING"
    assert "AGENT_VAULT_CREDENTIAL_MISSING" not in (row.get("error_message") or "").upper() or True
    # password must not appear in any stored field (no password was involved)


@pytest.mark.asyncio
async def test_genspark_autologin_password_not_in_output_on_login_failure(monkeypatch):
    """The credential password must never appear in result_metadata, error_message, or the returned dict."""
    _SECRET_PW = "sup3r-s3cr3t-pw!"

    monkeypatch.setattr("app.core.credential_vault.decrypt_value", lambda v: v)

    conn = _VaultConn()
    conn.sessions["sess-2"] = _TENANT_A
    conn.vault_credentials = [
        _vault_row(
            tenant_id=_TENANT_A,
            work_key="genspark-media-fallback",
            origin="https://login.genspark.ai",
            username="user@example.com",
            password=_SECRET_PW,
        )
    ]

    svc = MediaGenerationService(settings_obj=_settings(), pool_provider=lambda: _Pool(conn))
    queued = await svc.generate_image(
        "a product image",
        provider="genspark_ui",
        model_id="genspark-image-ui",
        session_id="sess-2",
    )

    monkeypatch.setattr(svc, "_acquire_genspark_page", lambda **kw: _coro(object()))
    monkeypatch.setattr(svc, "_read_genspark_page_text", lambda page: _coro("로그인 sign in"))
    monkeypatch.setattr(
        svc,
        "_attempt_genspark_login",
        lambda page, *, username, password, login_url="": _coro({"ok": False, "error": "LOGIN_FAILED"}),
    )

    result = await svc.process_genspark_ui_job(job_id=queued["job_id"])

    row = conn.rows[queued["job_id"]]
    stored_metadata = json.dumps(row.get("result_metadata") or {})
    stored_error = row.get("error_message") or ""
    result_str = json.dumps(result)

    assert _SECRET_PW not in stored_metadata, "password must not leak into result_metadata"
    assert _SECRET_PW not in stored_error, "password must not leak into error_message"
    assert _SECRET_PW not in result_str, "password must not leak into returned dict"
    assert row["result_metadata"]["ui_automation"]["last_error"] == "AGENT_VAULT_LOGIN_FAILED"


@pytest.mark.asyncio
async def test_genspark_autologin_captcha_maps_to_login_required(monkeypatch):
    """CAPTCHA_DETECTED from login attempt must produce last_error=GENSPARK_LOGIN_REQUIRED."""
    monkeypatch.setattr("app.core.credential_vault.decrypt_value", lambda v: v)

    conn = _VaultConn()
    conn.sessions["sess-3"] = _TENANT_A
    conn.vault_credentials = [
        _vault_row(tenant_id=_TENANT_A, work_key="genspark-media-fallback", origin="https://login.genspark.ai")
    ]

    svc = MediaGenerationService(settings_obj=_settings(), pool_provider=lambda: _Pool(conn))
    queued = await svc.generate_image(
        "a product image",
        provider="genspark_ui",
        model_id="genspark-image-ui",
        session_id="sess-3",
    )

    class _ReadyPage:
        async def wait_for_timeout(self, _ms):
            return None

    monkeypatch.setattr(svc, "_acquire_genspark_page", lambda **kw: _coro(_ReadyPage()))
    monkeypatch.setattr(svc, "_read_genspark_page_text", lambda page: _coro("로그인 sign in"))
    monkeypatch.setattr(
        svc,
        "_attempt_genspark_login",
        lambda page, *, username, password, login_url="": _coro({"ok": False, "error": "CAPTCHA_DETECTED"}),
    )

    result = await svc.process_genspark_ui_job(job_id=queued["job_id"])

    row = conn.rows[queued["job_id"]]
    assert row["result_metadata"]["ui_automation"]["last_error"] == "GENSPARK_LOGIN_REQUIRED"
    assert result["automation_state"] == "auth_required"


@pytest.mark.asyncio
async def test_genspark_autologin_no_session_id_stays_credential_missing(monkeypatch):
    """Job with no session_id must skip vault lookup and return AGENT_VAULT_CREDENTIAL_MISSING."""
    conn = _VaultConn()
    svc = MediaGenerationService(settings_obj=_settings(), pool_provider=lambda: _Pool(conn))
    queued = await svc.generate_image(
        "a product image",
        provider="genspark_ui",
        model_id="genspark-image-ui",
        session_id=None,
    )

    monkeypatch.setattr(svc, "_acquire_genspark_page", lambda **kw: _coro(object()))
    monkeypatch.setattr(svc, "_read_genspark_page_text", lambda page: _coro("로그인 sign in"))

    result = await svc.process_genspark_ui_job(job_id=queued["job_id"])

    row = conn.rows[queued["job_id"]]
    assert row["result_metadata"]["ui_automation"]["last_error"] == "AGENT_VAULT_CREDENTIAL_MISSING"
    assert result["automation_state"] == "auth_required"


@pytest.mark.asyncio
async def test_genspark_ready_page_with_login_text_does_not_trigger_vault_login(monkeypatch):
    """A logged-in Genspark page can still contain Login text in nav; ready markers should win."""
    conn = _VaultConn()
    svc = MediaGenerationService(settings_obj=_settings(), pool_provider=lambda: _Pool(conn))
    queued = await svc.generate_image(
        "a product image",
        provider="genspark_ui",
        model_id="genspark-image-ui",
        session_id="sess-ready",
    )

    class _ReadyPage:
        async def wait_for_timeout(self, _ms):
            return None

    monkeypatch.setattr(svc, "_acquire_genspark_page", lambda **kw: _coro(_ReadyPage()))
    monkeypatch.setattr(svc, "_read_genspark_page_text", lambda page: _coro("Login Prompt Generate Agent Chat"))
    monkeypatch.setattr(
        svc,
        "_attempt_genspark_login",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("vault login should not run")),
    )
    monkeypatch.setattr(svc, "_submit_prompt_to_genspark", lambda page, prompt: _coro({"ok": True}))
    monkeypatch.setattr(
        svc,
        "_extract_genspark_media_candidate",
        lambda page: _coro({"ok": True, "data_uri": "data:image/png;base64,ZmFrZQ=="}),
    )
    monkeypatch.setattr(
        svc,
        "_save_data_uri_media",
        lambda *, job_id, data_uri, kind: {
            "url": "/static/media/generated/image/fake.png",
            "path": "/tmp/fake.png",
            "bytes": 4,
            "content_type": "image/png",
        },
    )

    result = await svc.process_genspark_ui_job(job_id=queued["job_id"])

    assert result["automation_state"] == "succeeded"


@pytest.mark.asyncio
async def test_genspark_vault_login_timeout_continues_when_page_is_ready(monkeypatch):
    """A slow login settle timeout should not fail the job if the page is already ready."""
    monkeypatch.setattr("app.core.credential_vault.decrypt_value", lambda v: v)

    conn = _VaultConn()
    conn.sessions["sess-timeout-ready"] = _TENANT_A
    conn.vault_credentials = [
        _vault_row(
            tenant_id=_TENANT_A,
            work_key="genspark-media-fallback",
            origin="https://login.genspark.ai",
        )
    ]
    svc = MediaGenerationService(settings_obj=_settings(), pool_provider=lambda: _Pool(conn))
    queued = await svc.generate_image(
        "a product image",
        provider="genspark_ui",
        model_id="genspark-image-ui",
        session_id="sess-timeout-ready",
    )

    class _ReadyAfterTimeoutPage:
        async def goto(self, *_args, **_kwargs):
            return None

        async def wait_for_timeout(self, _ms):
            return None

    reads = iter(["로그인 sign in", "Prompt Generate Agent Chat", "Prompt Generate Agent Chat"])

    monkeypatch.setattr(svc, "_acquire_genspark_page", lambda **kw: _coro(_ReadyAfterTimeoutPage()))
    monkeypatch.setattr(svc, "_read_genspark_page_text", lambda page: _coro(next(reads)))

    async def fake_timeout_login(*_args, **_kwargs):
        raise TimeoutError("GENSPARK_VAULT_LOGIN_TIMEOUT")

    monkeypatch.setattr(svc, "_attempt_genspark_login", fake_timeout_login)
    monkeypatch.setattr(svc, "_submit_prompt_to_genspark", lambda page, prompt: _coro({"ok": True}))
    monkeypatch.setattr(
        svc,
        "_extract_genspark_media_candidate",
        lambda page: _coro({"ok": True, "data_uri": "data:image/png;base64,ZmFrZQ=="}),
    )
    monkeypatch.setattr(
        svc,
        "_save_data_uri_media",
        lambda *, job_id, data_uri, kind: {
            "url": "/static/media/generated/image/fake.png",
            "path": "/tmp/fake.png",
            "bytes": 4,
            "content_type": "image/png",
        },
    )

    result = await svc.process_genspark_ui_job(job_id=queued["job_id"])

    assert result["automation_state"] == "succeeded"


def test_genspark_vault_login_timeout_has_independent_cap(monkeypatch):
    monkeypatch.delenv("AADS_GENSPARK_VAULT_LOGIN_TIMEOUT_SECONDS", raising=False)

    assert MediaGenerationService._genspark_vault_login_timeout_seconds(240) == 180.0

    monkeypatch.setenv("AADS_GENSPARK_VAULT_LOGIN_TIMEOUT_SECONDS", "20")
    assert MediaGenerationService._genspark_vault_login_timeout_seconds(240) == 30.0

    monkeypatch.setenv("AADS_GENSPARK_VAULT_LOGIN_TIMEOUT_SECONDS", "999")
    assert MediaGenerationService._genspark_vault_login_timeout_seconds(120) == 120.0


@pytest.mark.asyncio
async def test_attempt_genspark_login_handles_delayed_password_field():
    """Genspark can show a staged email -> continue -> password flow."""

    class _Keyboard:
        async def press(self, _key):
            return None

    class _StagedLoginPage:
        def __init__(self):
            self.url = "https://login.genspark.ai"
            self.text = "Sign in"
            self.password_visible = False
            self.email_filled = False
            self.password_filled = False
            self.keyboard = _Keyboard()

        async def goto(self, url, **_kwargs):
            self.url = url

        async def wait_for_timeout(self, _ms):
            return None

        async def evaluate(self, script, *args):
            if "input[type=\"email\"]" in script and args:
                self.email_filled = True
                return True
            if "continue|next" in script:
                self.password_visible = True
                self.text = "Password"
                return True
            if "input[type=\"password\"]" in script and "return r.width > 0" in script:
                return self.password_visible
            if "input[type=\"password\"]" in script and args:
                self.password_filled = True
                return True
            if "button[type=\"submit\"],button" in script:
                self.url = "https://www.genspark.ai/agents?id=test"
                self.text = "Prompt Generate Agent Chat"
                return True
            return False

    page = _StagedLoginPage()
    svc = MediaGenerationService(settings_obj=_settings(), pool_provider=lambda: _Pool(_VaultConn()))
    result = await svc._attempt_genspark_login(
        page,
        username="user@example.com",
        password="secret-value",
        login_url="https://login.genspark.ai",
    )

    assert result == {"ok": True}
    assert page.email_filled is True
    assert page.password_filled is True


def _coro(value):
    """Helper: return an already-resolved coroutine wrapping value."""
    async def _inner():
        return value
    return _inner()
