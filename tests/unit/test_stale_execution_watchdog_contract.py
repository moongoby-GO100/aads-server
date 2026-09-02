from pathlib import Path


def test_stale_execution_watchdog_selects_stranded_auto_resume_flag():
    source = Path("app/main.py").read_text(encoding="utf-8")

    assert "AS stranded_auto_resume" in source
    assert 'os.getenv("AADS_WATCHDOG_AUTO_RETRY", "1") == "1"' in source
    assert "watchdog_auto_retry_scheduled" in source
    assert "watchdog_retry_source" in source


def test_stale_execution_watchdog_records_settle_diagnostics():
    source = Path("app/main.py").read_text(encoding="utf-8")

    assert "watchdog_settled_without_retry" in source
    assert "watchdog_settle_reason" in source
    assert "watchdog_retry_enabled" in source


def test_streaming_status_uses_resume_attempt_cap_constant():
    source = Path("app/routers/chat.py").read_text(encoding="utf-8")

    assert "te.retry_count < $2" in source
    assert "svc._EXECUTION_RESUME_MAX_ATTEMPTS" in source
    assert "te.retry_count < 5" not in source
