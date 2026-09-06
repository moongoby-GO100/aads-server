from pathlib import Path


CHAT_SERVICE = Path("app/services/chat_service.py")


def test_stream_requires_done_event_before_final_save():
    source = CHAT_SERVICE.read_text()
    assert "_stream_done_seen = False" in source
    assert "missing_done_event_stream_closed" in source
    assert "stream_missing_done_retry" in source
