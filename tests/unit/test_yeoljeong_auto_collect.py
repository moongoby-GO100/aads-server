from types import SimpleNamespace

import scripts.yeoljeong_auto_collect as auto_collect


def test_completion_state_requires_success_or_imported_rows():
    summary = {
        "summary": [
            {
                "service": "baemin",
                "status": "partial",
                "error_code": "AUTHENTICATED_NO_ROWS",
                "counts": {"sales": 0, "settlements": 0, "reviews": 0},
            },
            {
                "service": "yogiyo",
                "status": "succeeded",
                "error_code": "",
                "counts": {"sales": 0, "settlements": 0, "reviews": 0},
            },
        ]
    }

    state = auto_collect._completion_state(summary)

    assert state["complete"] is False
    assert state["pending"] == 1
    assert state["retryable_codes"] == ["AUTHENTICATED_NO_ROWS"]


def test_completion_state_treats_imported_rows_as_complete():
    summary = {
        "summary": [
            {
                "service": "baemin",
                "status": "partial",
                "error_code": "AUTHENTICATED_NO_ROWS",
                "counts": {"sales": 1, "settlements": 0, "reviews": 0},
            },
            {
                "service": "ddangyo",
                "status": "succeeded",
                "error_code": "",
                "counts": {"sales": 0, "settlements": 0, "reviews": 0},
            },
        ]
    }

    state = auto_collect._completion_state(summary)

    assert state["complete"] is True
    assert state["pending"] == 0


def test_until_complete_retries_until_imported_rows(monkeypatch):
    calls = []
    sleeps = []
    results = [
        {
            "summary": [
                {
                    "service": "baemin",
                    "status": "partial",
                    "error_code": "AUTHENTICATED_NO_ROWS",
                    "counts": {"sales": 0, "settlements": 0, "reviews": 0},
                }
            ]
        },
        {
            "summary": [
                {
                    "service": "baemin",
                    "status": "partial",
                    "error_code": "AUTHENTICATED_NO_ROWS",
                    "counts": {"sales": 2, "settlements": 0, "reviews": 0},
                }
            ]
        },
    ]

    def fake_run_sync(payload, user, *, queue_only=False):
        calls.append((payload, user, queue_only))
        return results.pop(0)

    monkeypatch.setattr(auto_collect, "_payload", lambda args: {"business_id": "all"})
    monkeypatch.setattr(auto_collect, "_run_sync", fake_run_sync)
    monkeypatch.setattr(auto_collect, "_sleep", lambda seconds: sleeps.append(seconds))

    args = SimpleNamespace(
        max_attempts=3,
        retry_seconds=7,
        blocked_retry_seconds=19,
        success_sleep_seconds=1800,
        repeat_after_complete=False,
    )

    assert auto_collect._run_until_complete(args, {"email": "system@aads.local", "is_admin": True}) == 0
    assert len(calls) == 2
    assert sleeps == [7]


def test_until_complete_uses_blocked_retry_interval(monkeypatch):
    sleeps = []

    monkeypatch.setattr(auto_collect, "_payload", lambda args: {"business_id": "all"})
    monkeypatch.setattr(
        auto_collect,
        "_run_sync",
        lambda payload, user, *, queue_only=False: {
            "summary": [
                {
                    "service": "ddangyo",
                    "status": "action_required",
                    "error_code": "DDANGYO_NUMERIC_CAPTCHA_REQUIRED",
                    "counts": {"sales": 0, "settlements": 0, "reviews": 0},
                }
            ]
        },
    )
    monkeypatch.setattr(auto_collect, "_sleep", lambda seconds: sleeps.append(seconds))

    args = SimpleNamespace(
        max_attempts=2,
        retry_seconds=7,
        blocked_retry_seconds=19,
        success_sleep_seconds=1800,
        repeat_after_complete=False,
    )

    assert auto_collect._run_until_complete(args, {"email": "system@aads.local", "is_admin": True}) == 2
    assert sleeps == [19]
