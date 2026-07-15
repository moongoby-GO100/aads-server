from app.api.ops import _codex_usage_fallback, _normalize_codex_usage_payload


def test_normalize_codex_payload_keeps_limit_windows():
    payload = {
        "ok": True,
        "plan_type": "pro",
        "limits": [
            {
                "limit_id": "codex",
                "primary": {"used_percent": 12.5, "window_minutes": 300, "resets_in_sec": 120},
                "secondary": {"used_percent": 6.0, "window_minutes": 10080, "resets_in_sec": 3600},
            }
        ],
    }

    result = _normalize_codex_usage_payload(payload)

    assert result["ok"] is True
    assert result["limits"][0]["limit_id"] == "codex"
    assert result["limits"][0]["primary"]["used_percent"] == 12.5
    assert result["limits"][0]["secondary"]["resets_in_sec"] == 3600


def test_codex_fallback_returns_visible_limit():
    result = _codex_usage_fallback("relay_empty_limits", "empty")

    assert result["ok"] is True
    assert result["fallback"] is True
    assert result["fallback_reason"] == "relay_empty_limits"
    assert result["limits"][0]["limit_id"] == "codex"
    assert result["limits"][0]["primary"]["window_minutes"] == 300
    assert result["limits"][0]["secondary"]["window_minutes"] == 10080
