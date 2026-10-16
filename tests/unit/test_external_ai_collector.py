"""외부 AI 수집기(scripts/external_ai_collector.py) 단위 테스트.

지키려는 것
  1. provider 판별은 URL 호스트로만 한다 — 쿼리·경로에 도메인이 끼어 있어도 속지 않는다.
  2. GenSpark 하드코딩 필터가 없다 — chatgpt 등 다른 provider URL 채널도 대상이 된다.
  3. "무엇을 저장하지 않을지" — 로그인/봇검증 화면은 provider 마다 거부하고,
     provider 문구가 다른 provider 의 정상 대화를 막지 않는다.
  4. content_hash 가 안정적이다 — 같은 화면은 같은 값, 다른 화면은 다른 값.
  5. DB 쓰기는 stdin JSON 으로만 가고 SQL 에 값이 보간되지 않는다.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]


def _load(name: str):
    path = _REPO / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


col = _load("external_ai_collector")

REAL_CHAT = ("사용자: 배포 순서를 알려줘\n" + "assistant: 먼저 릴리스 SHA 를 고정하고 빌드합니다. " * 20)


# ── provider 판별 ─────────────────────────────────────────────────────


@pytest.mark.parametrize("url,expected", [
    ("https://www.genspark.ai/agents?id=abc", "genspark"),
    ("https://genspark.ai/", "genspark"),
    ("https://chatgpt.com/c/123", "chatgpt"),
    ("https://chat.openai.com/c/123", "chatgpt"),
    ("https://gemini.google.com/app/xyz", "gemini"),
    ("https://claude.ai/chat/abc", "claude"),
    ("https://www.perplexity.ai/search/q", "perplexity"),
    ("HTTPS://ChatGPT.com/", "chatgpt"),
])
def test_detect_provider_known_hosts(url, expected):
    assert col.detect_provider(url) == expected


@pytest.mark.parametrize("url", [
    "",
    "not a url",
    "https://example.com/?next=https://chatgpt.com/",
    "https://example.com/genspark.ai",
    "https://evilgenspark.ai/",
    "https://chatgpt.com.evil.example/",
    "https://google.com/",
])
def test_detect_provider_ignores_lookalikes(url):
    assert col.detect_provider(url) is None


# ── 하드코딩 제거 ─────────────────────────────────────────────────────


def _items():
    return [
        {"id": "AADS_MGR", "url": "https://www.genspark.ai/agents?id=1", "project": "AADS"},
        {"id": "GPT_A", "url": "https://chatgpt.com/c/1", "project": "AADS"},
        {"id": "GEM_A", "url": "https://gemini.google.com/app/1"},
        {"id": "CLD_A", "url": "https://claude.ai/chat/1"},
        {"id": "PPX_A", "url": "https://www.perplexity.ai/search/1"},
        {"id": "OTHER", "url": "https://example.com/"},
        {"id": "NOURL"},
    ]


def test_select_channels_includes_non_genspark_providers():
    got = col.select_channels(_items())
    assert [c["id"] for c in got] == ["AADS_MGR", "GPT_A", "GEM_A", "CLD_A", "PPX_A"]
    assert {c["id"]: c["provider"] for c in got}["GPT_A"] == "chatgpt"


def test_select_channels_provider_filter():
    got = col.select_channels(_items(), "chatgpt")
    assert [c["id"] for c in got] == ["GPT_A"]
    assert col.select_channels(_items(), "genspark")[0]["id"] == "AADS_MGR"


def test_select_channels_does_not_mutate_input():
    items = _items()
    col.select_channels(items)
    assert "provider" not in items[0]


def test_no_genspark_only_filter_left_in_source():
    src = (_REPO / "scripts" / "external_ai_collector.py").read_text(encoding="utf-8")
    assert 'if "genspark.ai" in url' not in src


# ── reject_reason ─────────────────────────────────────────────────────


def test_real_conversation_is_accepted_for_every_provider():
    for provider in col.PROVIDER_NAMES:
        assert col.reject_reason(REAL_CHAT, provider) is None


def test_short_body_is_rejected():
    assert "본문" in col.reject_reason("짧다", "chatgpt")
    assert "본문" in col.reject_reason("", "claude")
    assert "본문" in col.reject_reason(None, "gemini")  # type: ignore[arg-type]


def _login_page(phrase: str) -> str:
    return phrase + "\n" + "menu item " * 80


@pytest.mark.parametrize("provider,phrase", [
    ("genspark", "Genspark AI Workspace\nLogin with email\nGoogle\nMicrosoft"),
    ("genspark", "Performing security verification"),
    ("genspark", "메일 주소(으)로 로그인\n암호를 잊으셨습니까?\n계정이 없으세요?지금 등록하세요."),
    ("chatgpt", "Log in or sign up\nStay logged out"),
    ("gemini", "Meet Gemini, your personal AI assistant"),
    ("claude", "Your ideas, amplified\nContinue with email"),
    ("perplexity", "Sign in or create an account\nContinue with SSO"),
])
def test_login_screens_are_rejected_per_provider(provider, phrase):
    reason = col.reject_reason(_login_page(phrase), provider)
    assert reason and "차단 문구" in reason


def test_common_markers_apply_without_provider():
    assert col.reject_reason(_login_page("Login with email"))
    assert col.reject_reason(_login_page("Please verify you are human"), "claude")


def test_provider_markers_are_scoped_to_their_provider():
    claude_login = _login_page("Your ideas, amplified")
    assert col.reject_reason(claude_login, "claude")
    assert col.reject_reason(claude_login, "genspark") is None
    assert col.reject_reason(claude_login, "chatgpt") is None


# ── content_hash ──────────────────────────────────────────────────────


def test_content_hash_is_stable():
    assert col.content_hash(REAL_CHAT) == col.content_hash(REAL_CHAT)
    assert len(col.content_hash(REAL_CHAT)) == 64


def test_content_hash_ignores_whitespace_only_differences():
    assert col.content_hash("a  b\n\nc ") == col.content_hash(" a b c")


def test_content_hash_differs_for_different_content():
    assert col.content_hash("alpha beta") != col.content_hash("alpha gamma")


def test_content_hash_known_value():
    # sha256("hello world")
    assert col.content_hash("hello   world") == (
        "b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9"
    )


# ── DB 쓰기 ───────────────────────────────────────────────────────────


def test_db_record_for_saved_turn():
    ch = {"id": "GPT_A", "url": "https://chatgpt.com/c/1", "project": "aads",
          "name": "GPT 채널", "provider": "chatgpt"}
    rec = col._db_record(ch, "saved", text=REAL_CHAT)
    assert rec["provider"] == "chatgpt"
    assert rec["project"] == "AADS"
    assert rec["channel_key"] == "GPT_A"
    assert rec["turn"]["content_hash"] == col.content_hash(REAL_CHAT)
    assert rec["turn"]["char_count"] == len(REAL_CHAT)
    assert rec["turn"]["source"] == "chatgpt_pc_bridge"


def test_db_record_for_skip_has_no_turn():
    ch = {"id": "X", "url": "https://claude.ai/chat/1", "provider": "claude"}
    rec = col._db_record(ch, "skipped", reject="로그인 화면")
    assert rec["turn"] is None
    assert rec["status"] == "skipped"
    assert rec["reject_reason"] == "로그인 화면"
    assert rec["project"] == "X"


def test_persist_db_sends_body_only_via_stdin(monkeypatch):
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        seen["input"] = kw["input"]
        return subprocess.CompletedProcess(cmd, 0, stdout='noise\n{"channel_id": 7, "inserted": true}\n', stderr="")

    monkeypatch.setattr(col.subprocess, "run", fake_run)
    secret = "'; DROP TABLE x; -- $(rm -rf /)"
    rec = col._db_record({"id": "A", "url": "https://chatgpt.com/c/1", "provider": "chatgpt"},
                         "saved", text=secret * 20)
    assert col.persist_db(rec) == {"channel_id": 7, "inserted": True}
    assert secret not in " ".join(seen["cmd"])
    assert json.loads(seen["input"])["turn"]["snapshot"].startswith(secret)
    assert seen["cmd"][:5] == ["docker", "exec", "-i", col.CONTAINER, "python3"]


def test_persist_db_raises_on_nonzero_exit(monkeypatch):
    monkeypatch.setattr(
        col.subprocess, "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, stdout="", stderr="Traceback\nboom"),
    )
    with pytest.raises(RuntimeError, match="boom"):
        col.persist_db({"provider": "claude", "url": "u", "status": "failed"})


def test_db_writer_uses_bound_parameters_only():
    sql_part = col.DB_WRITER
    assert "$1" in sql_part and "ON CONFLICT (channel_id, content_hash) DO NOTHING" in sql_part
    assert "f\"" not in sql_part and ".format(" not in sql_part and "%s" not in sql_part


# ── 래퍼·읽기 전용 ────────────────────────────────────────────────────


_SHARED_HELPERS = ("log", "mint_token", "api", "resolve_agent_id", "ensure_session", "fetch_page_text")


def test_shared_helpers_are_the_genspark_module_objects():
    gs = sys.modules["genspark_conversation_collector"]
    for name in _SHARED_HELPERS:
        assert getattr(col, name) is getattr(gs, name), name


def test_helpers_are_not_redefined_in_new_module_source():
    src = (_REPO / "scripts" / "external_ai_collector.py").read_text(encoding="utf-8")
    for name in _SHARED_HELPERS:
        assert f"def {name}(" not in src, name


def test_importing_genspark_module_does_not_run_main(monkeypatch):
    gs_path = _REPO / "scripts" / "genspark_conversation_collector.py"
    spec = importlib.util.spec_from_file_location("_gs_probe", gs_path)
    assert spec and spec.loader
    probe = importlib.util.module_from_spec(spec)
    monkeypatch.setattr(sys, "exit", lambda *a, **k: pytest.fail("main executed on import"))
    spec.loader.exec_module(probe)
    assert callable(probe.main)


def test_nav_wait_is_propagated_to_shared_fetch_helper():
    gs = sys.modules["genspark_conversation_collector"]
    assert gs.NAV_WAIT_SEC == col.NAV_WAIT_SEC


def test_collector_issues_only_read_browser_commands():
    src = "\n".join(
        (_REPO / "scripts" / f"{n}.py").read_text(encoding="utf-8")
        for n in ("external_ai_collector", "genspark_conversation_collector")
    )
    assert '"browser_navigate"' in src and '"browser_get_text"' in src
    for write_cmd in ("browser_click", "browser_type", "browser_fill", "browser_press", "browser_submit"):
        assert write_cmd not in src


# ── 리뷰 지적 교정: 인증 헤더 / 종료코드 / enabled ───────────────────


class _FakeResp:
    def __init__(self, body=b'{"status": "ok"}'):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_monitor_key_prefers_env(monkeypatch):
    monkeypatch.setenv("AADS_MONITOR_KEY", " k-env ")
    assert col.monitor_key() == "k-env"


def test_monitor_key_falls_back_to_env_file(monkeypatch, tmp_path):
    monkeypatch.delenv("AADS_MONITOR_KEY", raising=False)
    f = tmp_path / ".env"
    f.write_text("OTHER=1\nAADS_MONITOR_KEY=\"k-file\"\n", encoding="utf-8")
    monkeypatch.setenv("AADS_ENV_FILE", str(f))
    assert col.monitor_key() == "k-file"


def test_monitor_key_missing_raises(monkeypatch, tmp_path):
    monkeypatch.delenv("AADS_MONITOR_KEY", raising=False)
    monkeypatch.setenv("AADS_ENV_FILE", str(tmp_path / "none.env"))
    with pytest.raises(RuntimeError, match="AADS_MONITOR_KEY"):
        col.monitor_key()


def test_post_system_memory_sends_monitor_key_header(monkeypatch):
    monkeypatch.setenv("AADS_MONITOR_KEY", "k-env")
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["url"] = req.full_url
        seen["headers"] = {k.lower(): v for k, v in req.header_items()}
        seen["method"] = req.get_method()
        seen["body"] = json.loads(req.data.decode())
        return _FakeResp()

    monkeypatch.setattr(col.urllib.request, "urlopen", fake_urlopen)
    col.post_system_memory("tok", "conversation:aads", "chat_1", {"a": 1})
    assert seen["url"].endswith("/api/v1/context/system")
    assert seen["method"] == "POST"
    assert seen["headers"]["x-monitor-key"] == "k-env"
    assert seen["headers"]["authorization"] == "Bearer tok"
    assert seen["body"] == {"category": "conversation:aads", "key": "chat_1", "value": {"a": 1}}


def test_save_writes_system_memory_with_monitor_key_then_db(monkeypatch):
    calls = []
    monkeypatch.setattr(col, "post_system_memory", lambda *a, **k: calls.append("sys"))
    monkeypatch.setattr(col, "persist_db", lambda rec: calls.append("db") or {"inserted": True})
    monkeypatch.setattr(col, "api", lambda *a, **k: pytest.fail("legacy Bearer-only api() used for system write"))
    ch = {"id": "GPT_A", "url": "https://chatgpt.com/c/1", "project": "aads"}
    out = col.save("tok", ch, REAL_CHAT, False)
    assert calls == ["sys", "db"]
    assert "저장 chat_" in out


def test_save_same_second_two_channels_get_distinct_system_memory_keys(monkeypatch):
    keys = []
    monkeypatch.setattr(col.time, "time", lambda: 123456)
    monkeypatch.setattr(col, "post_system_memory", lambda token, category, key, value: keys.append(key))
    monkeypatch.setattr(col, "persist_db", lambda rec: {"inserted": True})
    for cid in ("GPT/A", "GPT B"):
        col.save("tok", {"id": cid, "url": "https://chatgpt.com/c/1", "provider": "chatgpt"}, REAL_CHAT, False)
    assert len(set(keys)) == 2
    assert all(key.startswith("chat_123456_chatgpt_") for key in keys)


def test_save_long_channel_id_keeps_system_memory_key_within_varchar_limit(monkeypatch):
    keys = []
    monkeypatch.setattr(col.time, "time", lambda: 123456)
    monkeypatch.setattr(col, "post_system_memory", lambda token, category, key, value: keys.append(key))
    monkeypatch.setattr(col, "persist_db", lambda rec: {"inserted": True})
    channel = {"id": "A" * 70, "url": "https://chatgpt.com/c/1", "provider": "chatgpt"}
    col.save("tok", channel, REAL_CHAT, False)
    assert len(keys) == 1
    assert len(keys[0]) <= 100
    assert keys[0].startswith("chat_123456_chatgpt_")
    assert len(keys[0].rsplit("_", 1)[1]) == 8


def test_save_dry_run_writes_nothing(monkeypatch):
    monkeypatch.setattr(col, "post_system_memory", lambda *a, **k: pytest.fail("write"))
    monkeypatch.setattr(col, "persist_db", lambda rec: pytest.fail("write"))
    assert "DRY-RUN" in col.save("tok", {"id": "A", "url": "https://claude.ai/chat/1"}, REAL_CHAT, True)


def test_filter_enabled_drops_disabled_pairs_only():
    chs = [
        {"id": "A", "url": "https://chatgpt.com/c/1", "provider": "chatgpt"},
        {"id": "B", "url": "https://claude.ai/chat/1", "provider": "claude"},
        {"id": "C", "url": "https://claude.ai/chat/2", "provider": "claude"},
    ]
    kept, off = col.filter_enabled(chs, {("claude", "https://claude.ai/chat/1")})
    assert [c["id"] for c in kept] == ["A", "C"]
    assert [c["id"] for c in off] == ["B"]
    kept, off = col.filter_enabled(chs, set())
    assert len(kept) == 3 and off == []


def test_load_disabled_reads_pairs_via_fixed_script(monkeypatch):
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        seen["input"] = kw["input"]
        return subprocess.CompletedProcess(
            cmd, 0, stdout='{"disabled": [["claude", "https://claude.ai/chat/1"]]}\n', stderr="")

    monkeypatch.setattr(col.subprocess, "run", fake_run)
    assert col.load_disabled() == {("claude", "https://claude.ai/chat/1")}
    assert seen["cmd"][-1] == col.DB_DISABLED_READER
    assert "enabled = false" in col.DB_DISABLED_READER
    assert seen["input"] == ""


def _setup_main(monkeypatch, channels, *, text=REAL_CHAT, disabled=None, save_exc=None, fetch_exc=None):
    fetched = []
    monkeypatch.setattr(col, "mint_token", lambda: "tok")
    monkeypatch.setattr(col, "resolve_agent_id", lambda t: "agent")
    monkeypatch.setattr(col, "ensure_session", lambda *a, **k: None)
    monkeypatch.setattr(col, "load_channels", lambda token, provider="": [dict(c) for c in channels])
    if isinstance(disabled, Exception):
        def _raise():
            raise disabled
        monkeypatch.setattr(col, "load_disabled", _raise)
    else:
        monkeypatch.setattr(col, "load_disabled", lambda: disabled or set())

    def fake_fetch(token, agent, url, wk):
        fetched.append(url)
        if fetch_exc:
            raise fetch_exc
        return text

    def fake_save(token, ch, body, dry):
        if save_exc:
            raise save_exc
        return "저장 ok"

    monkeypatch.setattr(col, "fetch_page_text", fake_fetch)
    monkeypatch.setattr(col, "save", fake_save)
    monkeypatch.setattr(col, "record_status", lambda *a, **k: None)
    return fetched


_CH = [{"id": "GPT_A", "url": "https://chatgpt.com/c/1", "provider": "chatgpt"}]


def test_main_success_returns_zero(monkeypatch):
    _setup_main(monkeypatch, _CH)
    assert col.main([]) == 0


@pytest.mark.parametrize("work_key", ["genspark-collect", "extai-fixed-lane", "extai:fixed-lane"])
def test_main_rejects_shared_or_reserved_work_key_before_browser_use(monkeypatch, work_key):
    monkeypatch.setenv("EXTERNAL_AI_WORK_KEY", work_key)
    monkeypatch.setattr(col, "mint_token", lambda: pytest.fail("must reject before authentication"))
    assert col.main([]) == 2


@pytest.mark.parametrize("work_key", ["GENSPARK-COLLECT", " genspark-collect ", " extai:fixed-lane", "EXTAI-fixed-lane "])
def test_main_rejects_normalized_shared_or_reserved_work_key_before_browser_use(monkeypatch, work_key):
    monkeypatch.setenv("EXTERNAL_AI_WORK_KEY", work_key)
    monkeypatch.setattr(col, "mint_token", lambda: pytest.fail("must reject before authentication"))
    assert col.main([]) == 2


def test_main_uses_collector_specific_default_work_key(monkeypatch):
    monkeypatch.delenv("EXTERNAL_AI_WORK_KEY", raising=False)
    monkeypatch.setenv("GENSPARK_WORK_KEY", "genspark-collect")
    _setup_main(monkeypatch, _CH)
    session_keys = []
    monkeypatch.setattr(col, "ensure_session", lambda token, agent, wk: session_keys.append(wk))
    assert col.main([]) == 0
    assert session_keys == ["external-ai-collect"]


def test_collector_and_legacy_use_distinct_work_keys(monkeypatch):
    monkeypatch.delenv("EXTERNAL_AI_WORK_KEY", raising=False)
    monkeypatch.delenv("GENSPARK_WORK_KEY", raising=False)
    _setup_main(monkeypatch, _CH)
    collector_keys = []
    monkeypatch.setattr(col, "ensure_session", lambda token, agent, key: collector_keys.append(key))
    monkeypatch.setattr(col, "fetch_page_text", lambda token, agent, url, key: collector_keys.append(key) or REAL_CHAT)
    assert col.main([]) == 0
    assert collector_keys == ["external-ai-collect", "external-ai-collect"]
    assert "genspark-collect" not in collector_keys


def test_main_session_failure_sets_channel_failed_status(monkeypatch):
    _setup_main(monkeypatch, _CH)
    monkeypatch.setattr(col, "ensure_session", lambda *a: (_ for _ in ()).throw(TimeoutError("offline")))
    calls = []
    monkeypatch.setattr(col, "record_status", lambda ch, status, reason, dry: calls.append((ch["id"], status, reason)))
    assert col.main([]) == 2
    assert calls and calls[0][0:2] == ("GPT_A", "failed")
    assert "session" in calls[0][2]


def test_main_save_failure_returns_nonzero(monkeypatch):
    _setup_main(monkeypatch, _CH, save_exc=RuntimeError("HTTP Error 401: Unauthorized"))
    assert col.main([]) == 4


def test_main_save_failure_records_failed_status_on_channel(monkeypatch):
    _setup_main(monkeypatch, _CH, save_exc=RuntimeError("HTTP Error 401: Unauthorized"))
    calls = []
    monkeypatch.setattr(
        col, "record_status", lambda ch, status, reason, dry: calls.append((ch["id"], status, reason, dry))
    )
    assert col.main([]) == 4
    assert len(calls) == 1
    cid, status, reason, dry = calls[0]
    assert (cid, status, dry) == ("GPT_A", "failed", False)
    assert "401" in reason


def test_record_status_writes_failed_row_without_turn(monkeypatch):
    sent = []
    monkeypatch.setattr(col, "persist_db", lambda rec: sent.append(rec) or {})
    col.record_status(_CH[0], "failed", "RuntimeError: boom", dry_run=False)
    assert len(sent) == 1
    assert sent[0]["status"] == "failed" and sent[0]["reject_reason"] == "RuntimeError: boom"
    assert sent[0]["turn"] is None


def test_record_status_swallows_db_failure(monkeypatch):
    def boom(rec):
        raise RuntimeError("db down")
    monkeypatch.setattr(col, "persist_db", boom)
    col.record_status(_CH[0], "failed", "x", dry_run=False)


def test_record_status_dry_run_writes_nothing(monkeypatch):
    monkeypatch.setattr(col, "persist_db", lambda rec: pytest.fail("dry-run wrote to DB"))
    col.record_status(_CH[0], "failed", "x", dry_run=True)


def test_main_fetch_failure_returns_nonzero(monkeypatch):
    _setup_main(monkeypatch, _CH, fetch_exc=RuntimeError("timeout"))
    assert col.main([]) == 4


def test_main_partial_failure_still_nonzero(monkeypatch):
    two = _CH + [{"id": "CLD", "url": "https://claude.ai/chat/1", "provider": "claude"}]
    state = {"n": 0}
    _setup_main(monkeypatch, two)

    def flaky_save(token, ch, body, dry):
        state["n"] += 1
        if state["n"] == 2:
            raise RuntimeError("boom")
        return "저장 ok"

    monkeypatch.setattr(col, "save", flaky_save)
    assert col.main([]) == 4


def test_main_all_skipped_returns_three(monkeypatch):
    _setup_main(monkeypatch, _CH, text="짧다")
    assert col.main([]) == 3


def test_main_does_not_open_disabled_channel(monkeypatch):
    fetched = _setup_main(monkeypatch, _CH, disabled={("chatgpt", "https://chatgpt.com/c/1")})
    assert col.main([]) == 0
    assert fetched == []


def test_main_opens_only_enabled_channels(monkeypatch):
    two = _CH + [{"id": "CLD", "url": "https://claude.ai/chat/1", "provider": "claude"}]
    fetched = _setup_main(monkeypatch, two, disabled={("chatgpt", "https://chatgpt.com/c/1")})
    assert col.main([]) == 0
    assert fetched == ["https://claude.ai/chat/1"]


def test_main_fails_closed_when_enabled_lookup_fails(monkeypatch):
    fetched = _setup_main(monkeypatch, _CH, disabled=RuntimeError("db down"))
    assert col.main([]) == 5
    assert fetched == []


# ── cron 설치 스크립트 ────────────────────────────────────────────────


def test_cron_installer_entry_has_marker_timeout_and_flock():
    out = subprocess.run(
        ["bash", str(_REPO / "scripts" / "install_external_ai_collector_cron.sh"), "--print"],
        capture_output=True, text=True, check=True,
    ).stdout.strip().splitlines()
    assert len(out) == 1
    line = out[0]
    assert line.startswith("17 * * * * ")
    assert "# AADS_EXTERNAL_AI_COLLECTOR" in line
    assert "timeout 900" in line and "flock -n" in line
    assert "/var/log/aads-external-ai-collector.log" in line


def test_cron_installer_disabled_entry_is_commented_but_keeps_marker():
    script = str(_REPO / "scripts" / "install_external_ai_collector_cron.sh")
    out = subprocess.run(
        ["bash", script, "--print", "--disabled"], capture_output=True, text=True, check=True,
    ).stdout.strip().splitlines()
    assert len(out) == 1
    assert out[0].startswith("# 17 * * * * ")
    assert out[0].count("# AADS_EXTERNAL_AI_COLLECTOR") == 1


# ── cron 설치 스크립트: crontab 조회 실패 시 기존 작업 보호 ─────────────

_INSTALLER = str(_REPO / "scripts" / "install_external_ai_collector_cron.sh")
_FAKE_CRONTAB = r'''#!/usr/bin/env bash
# 모의 crontab. FAKE_CRON_LIST: ok|none|error. 쓰기는 FAKE_CRON_STATE 파일에 저장한다.
if [ "$1" = "-l" ]; then
    case "$FAKE_CRON_LIST" in
        ok) cat "$FAKE_CRON_STATE" 2>/dev/null; exit 0 ;;
        none) echo "no crontab for root" >&2; exit 1 ;;
        *) echo "crontab: cannot open crontab spool: Permission denied" >&2; exit 1 ;;
    esac
fi
if [ "$1" = "-" ]; then
    cat > "$FAKE_CRON_STATE.new" && mv "$FAKE_CRON_STATE.new" "$FAKE_CRON_STATE"
    exit 0
fi
exit 2
'''


def _run_installer(tmp_path, list_mode, existing, *args):
    import os
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    fake = bin_dir / "crontab"
    fake.write_text(_FAKE_CRONTAB, encoding="utf-8")
    fake.chmod(0o755)
    state = tmp_path / "state"
    if existing is not None:
        state.write_text(existing, encoding="utf-8")
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}",
           "FAKE_CRON_LIST": list_mode, "FAKE_CRON_STATE": str(state)}
    proc = subprocess.run(["bash", _INSTALLER, *args], capture_output=True, text=True, env=env)
    return proc, (state.read_text(encoding="utf-8") if state.exists() else None)


_EXISTING = "0 3 * * * /usr/bin/backup\n30 4 * * * /usr/bin/other\n"


@pytest.mark.parametrize("args", [(), ("--disabled",), ("--remove",)])
def test_cron_installer_aborts_without_writing_when_list_fails(tmp_path, args):
    proc, after = _run_installer(tmp_path, "error", _EXISTING, *args)
    assert proc.returncode != 0
    assert "Permission denied" in proc.stderr
    assert after == _EXISTING


def test_cron_installer_treats_no_crontab_as_empty(tmp_path):
    proc, after = _run_installer(tmp_path, "none", None, "--disabled")
    assert proc.returncode == 0, proc.stderr
    assert after is not None and after.count("# AADS_EXTERNAL_AI_COLLECTOR") == 1
    assert after.lstrip().startswith("# 17 * * * * ")


def test_cron_installer_preserves_existing_jobs_and_is_idempotent(tmp_path):
    proc, after = _run_installer(tmp_path, "ok", _EXISTING, "--disabled")
    assert proc.returncode == 0, proc.stderr
    proc2, after2 = _run_installer(tmp_path, "ok", after, "--disabled")
    assert proc2.returncode == 0, proc2.stderr
    assert after2 == after
    assert "0 3 * * * /usr/bin/backup" in after2 and "30 4 * * * /usr/bin/other" in after2
    assert after2.count("# AADS_EXTERNAL_AI_COLLECTOR") == 1


def test_cron_installer_remove_only_drops_marker_line(tmp_path):
    _, installed = _run_installer(tmp_path, "ok", _EXISTING, "--disabled")
    proc, after = _run_installer(tmp_path, "ok", installed, "--remove")
    assert proc.returncode == 0, proc.stderr
    assert "AADS_EXTERNAL_AI_COLLECTOR" not in after
    assert "0 3 * * * /usr/bin/backup" in after and "30 4 * * * /usr/bin/other" in after
