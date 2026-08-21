from types import SimpleNamespace

import pytest

import scripts.yeoljeong_auto_collect as auto_collect


@pytest.fixture(autouse=True)
def isolate_platform_financial_accounts(monkeypatch):
    monkeypatch.setattr(auto_collect, "list_platform_accounts", lambda user, business_id=None: [])
    monkeypatch.setattr(
        auto_collect,
        "list_bank_accounts",
        lambda user, business_id=None, *, branch_id=None, status=None: [],
    )


def test_payload_passes_force_recreate_sessions_flag():
    args = auto_collect.build_parser().parse_args(
        [
            "--services",
            "baemin,ddangyo",
            "--business-id",
            "biz-junghwa",
            "--branch",
            "중화점",
            "--force-recreate-sessions",
        ]
    )

    payload = auto_collect._payload(args)

    assert payload["services"] == ["baemin", "ddangyo"]
    assert payload["business_id"] == "biz-junghwa"
    assert payload["branch"] == "중화점"
    assert payload["force_recreate_portal_sessions"] is True
    assert payload["close_portal_browser_on_complete"] is True


def test_payload_can_keep_browser_open_for_manual_debugging():
    args = auto_collect.build_parser().parse_args(["--keep-browser-open"])

    payload = auto_collect._payload(args)

    assert payload["close_portal_browser_on_complete"] is False


def test_until_complete_payload_skips_financial_accounts_by_default():
    args = auto_collect.build_parser().parse_args(["--until-complete"])

    payload = auto_collect._payload(args)

    assert payload["skip_financial_accounts"] is True


def test_run_collectors_collects_auto_sync_bank_accounts(monkeypatch):
    calls = []

    monkeypatch.setattr(
        auto_collect,
        "_run_sync",
        lambda payload, user, *, queue_only=False: {
            "synced_at": "2026-08-20T19:40:00+09:00",
            "business_id": "biz-mia",
            "branch": "열정국밥_미아점",
            "date_from": "2026-08-01",
            "date_to": "2026-08-20",
            "summary": [
                {
                    "service": "baemin",
                    "status": "succeeded",
                    "error_code": "",
                    "counts": {"sales": 1, "settlements": 0, "reviews": 0},
                }
            ],
        },
    )
    monkeypatch.setattr(
        auto_collect,
        "list_bank_accounts",
        lambda user, business_id=None, *, branch_id=None, status=None: [
            {
                "id": "bank-1",
                "business_id": business_id,
                "branch_id": branch_id,
                "connection_type": "mock",
                "auto_sync": True,
            },
            {
                "id": "bank-2",
                "business_id": business_id,
                "branch_id": branch_id,
                "connection_type": "mock",
                "auto_sync": False,
            },
        ],
    )

    def fake_collect(account_id, payload, user):
        calls.append((account_id, payload))
        return {
            "collection": {
                "bank_account_id": account_id,
                "business_id": payload["business_id"],
                "branch_id": payload["branch_id"],
                "status": "completed",
                "connector_status": "CONFIGURED",
                "connection_type": "mock",
                "collected_rows": 2,
                "imported_rows": 2,
                "duplicate_rows": 0,
                "message": "은행 거래 수집이 완료되었습니다.",
            },
            "transactions": [],
        }

    monkeypatch.setattr(auto_collect, "collect_bank_account_transactions", fake_collect)

    result = auto_collect._run_collectors(
        {
            "services": ["baemin"],
            "business_id": "biz-mia",
            "branch": "열정국밥_미아점",
            "date_from": "2026-08-01",
            "date_to": "2026-08-20",
        },
        {"email": "system@aads.local", "is_admin": True},
    )

    assert [call[0] for call in calls] == ["bank-1"]
    assert calls[0][1]["branch_id"] == "branch-gangbuk-mia"
    assert result["bank_totals"]["accounts"] == 1
    assert result["bank_totals"]["imported_rows"] == 2
    assert result["bank_collections"][0]["status"] == "completed"


def test_run_collectors_collects_legacy_platform_financial_accounts(monkeypatch):
    calls = []

    monkeypatch.setattr(
        auto_collect,
        "_run_sync",
        lambda payload, user, *, queue_only=False: {
            "synced_at": "2026-08-20T19:40:00+09:00",
            "business_id": "biz-junghwa",
            "branch": "중화점",
            "date_from": "2026-08-01",
            "date_to": "2026-08-20",
            "summary": [
                {
                    "service": "baemin",
                    "status": "succeeded",
                    "error_code": "",
                    "counts": {"sales": 1, "settlements": 0, "reviews": 0},
                }
            ],
        },
    )
    monkeypatch.setattr(auto_collect, "list_bank_accounts", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        auto_collect,
        "list_platform_accounts",
        lambda user, business_id=None: [
            {
                "id": "legacy-bank-1",
                "service": "ibk_business",
                "business_id": business_id,
                "branch": "중화점",
                "collection_mode": "bank-quick-service",
                "auto_sync": True,
            }
        ],
    )

    def fake_sync_financial_transactions(payload, user):
        calls.append(payload)
        return {
            "synced_at": "2026-08-20T19:41:00+09:00",
            "business_id": payload["business_id"],
            "branch": payload["branch"],
            "summary": [
                {
                    "service": "ibk_business",
                    "status": "connector_not_configured",
                    "message": "커넥터가 아직 연결되지 않았습니다.",
                    "account_id": payload["account_id"],
                    "collection_mode": "bank-quick-service",
                    "imported_rows": 0,
                }
            ],
            "transactions": [],
            "totals": {"transactions": 0},
        }

    monkeypatch.setattr(auto_collect, "sync_financial_transactions", fake_sync_financial_transactions)

    result = auto_collect._run_collectors(
        {
            "services": ["baemin"],
            "business_id": "biz-junghwa",
            "branch": "중화점",
            "date_from": "2026-08-01",
            "date_to": "2026-08-20",
        },
        {"email": "system@aads.local", "is_admin": True},
    )

    assert calls == [
        {
            "services": ["ibk_business"],
            "account_id": "legacy-bank-1",
            "business_id": "biz-junghwa",
            "branch": "중화점",
            "date_from": "2026-08-01",
            "date_to": "2026-08-20",
        }
    ]
    assert result["bank_totals"]["accounts"] == 1
    assert result["bank_collections"][0]["source_ledger"] == "platform_accounts"
    assert result["bank_collections"][0]["error_code"] == "BANK_CONNECTOR_NOT_CONFIGURED"


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


def test_completion_state_blocks_on_bank_browser_session_required():
    summary = {
        "summary": [
            {
                "service": "baemin",
                "status": "succeeded",
                "error_code": "",
                "counts": {"sales": 1, "settlements": 0, "reviews": 0},
            }
        ],
        "bank_collections": [
            {
                "service": "bank",
                "status": "action_required",
                "error_code": "BANK_BROWSER_SESSION_REQUIRED",
                "counts": {"transactions": 0},
            }
        ],
    }

    state = auto_collect._completion_state(summary)

    assert state["complete"] is False
    assert state["blocked"] is True
    assert state["pending"] == 1
    assert state["total"] == 2
    assert state["blocking_codes"] == ["BANK_BROWSER_SESSION_REQUIRED"]


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
    monkeypatch.setattr(auto_collect, "_initial_force_recreate_portal_sessions", lambda payload, user: False)

    args = SimpleNamespace(
        max_attempts=3,
        retry_seconds=7,
        blocked_retry_seconds=19,
        success_sleep_seconds=1800,
        attempt_timeout_seconds=0,
        retry_blocked=False,
        repeat_after_complete=False,
    )

    assert auto_collect._run_until_complete(args, {"email": "system@aads.local", "is_admin": True}) == 0
    assert len(calls) == 2
    assert sleeps == [7]


def test_until_complete_force_recreates_after_wrong_portal_session(monkeypatch):
    calls = []
    sleeps = []
    results = [
        {
            "summary": [
                {
                    "service": "ddangyo",
                    "status": "action_required",
                    "error_code": "PC_AGENT_WRONG_PORTAL_SESSION",
                    "counts": {"sales": 0, "settlements": 0, "reviews": 0},
                }
            ]
        },
        {
            "summary": [
                {
                    "service": "ddangyo",
                    "status": "succeeded",
                    "error_code": "",
                    "counts": {"sales": 1, "settlements": 0, "reviews": 0},
                }
            ]
        },
    ]

    def fake_run_sync(payload, user, *, queue_only=False):
        calls.append(dict(payload))
        return results.pop(0)

    monkeypatch.setattr(
        auto_collect,
        "_payload",
        lambda args: {
            "business_id": "all",
            "force_recreate_portal_sessions": False,
        },
    )
    monkeypatch.setattr(auto_collect, "_run_sync", fake_run_sync)
    monkeypatch.setattr(auto_collect, "_sleep", lambda seconds: sleeps.append(seconds))
    monkeypatch.setattr(auto_collect, "_initial_force_recreate_portal_sessions", lambda payload, user: False)

    args = SimpleNamespace(
        max_attempts=3,
        retry_seconds=7,
        blocked_retry_seconds=19,
        success_sleep_seconds=1800,
        attempt_timeout_seconds=0,
        repeat_after_complete=False,
    )

    assert auto_collect._run_until_complete(args, {"email": "system@aads.local", "is_admin": True}) == 0
    assert calls[0]["force_recreate_portal_sessions"] is False
    assert calls[1]["force_recreate_portal_sessions"] is True
    assert sleeps == [7]


def test_until_complete_force_recreates_after_pc_agent_session_required(monkeypatch):
    calls = []
    sleeps = []
    results = [
        {
            "summary": [
                {
                    "service": "coupangeats",
                    "status": "action_required",
                    "error_code": "PC_AGENT_SESSION_REQUIRED",
                    "counts": {"sales": 0, "settlements": 0, "reviews": 0},
                }
            ]
        },
        {
            "summary": [
                {
                    "service": "coupangeats",
                    "status": "succeeded",
                    "error_code": "",
                    "counts": {"sales": 1, "settlements": 0, "reviews": 0},
                }
            ]
        },
    ]

    def fake_run_sync(payload, user, *, queue_only=False):
        calls.append(dict(payload))
        return results.pop(0)

    monkeypatch.setattr(
        auto_collect,
        "_payload",
        lambda args: {
            "business_id": "all",
            "force_recreate_portal_sessions": False,
        },
    )
    monkeypatch.setattr(auto_collect, "_run_sync", fake_run_sync)
    monkeypatch.setattr(auto_collect, "_sleep", lambda seconds: sleeps.append(seconds))
    monkeypatch.setattr(auto_collect, "_initial_force_recreate_portal_sessions", lambda payload, user: False)

    args = SimpleNamespace(
        max_attempts=3,
        retry_seconds=7,
        blocked_retry_seconds=19,
        success_sleep_seconds=1800,
        attempt_timeout_seconds=0,
        repeat_after_complete=False,
    )

    assert auto_collect._run_until_complete(args, {"email": "system@aads.local", "is_admin": True}) == 0
    assert calls[0]["force_recreate_portal_sessions"] is False
    assert calls[1]["force_recreate_portal_sessions"] is True
    assert sleeps == [19]


def test_until_complete_force_recreates_on_first_attempt_from_existing_status(monkeypatch):
    calls = []

    monkeypatch.setattr(
        auto_collect,
        "_payload",
        lambda args: {
            "services": ["ddangyo"],
            "business_id": "all",
            "branch": "전체",
            "all_businesses": True,
            "force_recreate_portal_sessions": False,
        },
    )
    monkeypatch.setattr(
        auto_collect,
        "list_collection_status",
        lambda user, business_id=None: [
            {
                "service": "ddangyo",
                "business_id": "biz-junghwa",
                "branch": "중화점",
                "status": "action_required",
                "error_code": "PC_AGENT_WRONG_PORTAL_SESSION",
                "counts": {"sales": 0, "settlements": 0, "reviews": 0},
            }
        ],
    )
    monkeypatch.setattr(
        auto_collect,
        "_run_sync",
        lambda payload, user, *, queue_only=False: (
            calls.append(dict(payload))
            or {
                "summary": [
                    {
                        "service": "ddangyo",
                        "status": "succeeded",
                        "error_code": "",
                        "counts": {"sales": 1, "settlements": 0, "reviews": 0},
                    }
                ]
            }
        ),
    )
    monkeypatch.setattr(auto_collect, "_sleep", lambda seconds: None)

    args = SimpleNamespace(
        max_attempts=1,
        retry_seconds=7,
        blocked_retry_seconds=19,
        success_sleep_seconds=1800,
        attempt_timeout_seconds=0,
        retry_blocked=False,
        repeat_after_complete=False,
    )

    assert auto_collect._run_until_complete(args, {"email": "system@aads.local", "is_admin": True}) == 0
    assert calls[0]["force_recreate_portal_sessions"] is True


def test_until_complete_can_retry_blocked_when_requested(monkeypatch):
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
    monkeypatch.setattr(auto_collect, "_initial_force_recreate_portal_sessions", lambda payload, user: False)

    args = SimpleNamespace(
        max_attempts=2,
        retry_seconds=7,
        blocked_retry_seconds=19,
        success_sleep_seconds=1800,
        attempt_timeout_seconds=0,
        retry_blocked=True,
        repeat_after_complete=False,
    )

    assert auto_collect._run_until_complete(args, {"email": "system@aads.local", "is_admin": True}) == 2
    assert sleeps == [19]


def test_until_complete_stops_on_terminal_blocked_by_default(monkeypatch):
    sleeps = []

    monkeypatch.setattr(auto_collect, "_payload", lambda args: {"business_id": "all"})
    monkeypatch.setattr(
        auto_collect,
        "_run_sync",
        lambda payload, user, *, queue_only=False: {
            "summary": [
                {
                    "service": "coupangeats",
                    "status": "action_required",
                    "error_code": "PORTAL_BLOCKED",
                    "counts": {"sales": 0, "settlements": 0, "reviews": 0},
                }
            ]
        },
    )
    monkeypatch.setattr(auto_collect, "_sleep", lambda seconds: sleeps.append(seconds))
    monkeypatch.setattr(auto_collect, "_initial_force_recreate_portal_sessions", lambda payload, user: False)

    args = SimpleNamespace(
        max_attempts=0,
        retry_seconds=7,
        blocked_retry_seconds=19,
        success_sleep_seconds=1800,
        attempt_timeout_seconds=0,
        retry_blocked=False,
        repeat_after_complete=False,
    )

    assert auto_collect._run_until_complete(args, {"email": "system@aads.local", "is_admin": True}) == 2
    assert sleeps == []


def test_timeout_result_is_retryable():
    summary = auto_collect._summary(
        auto_collect._timeout_result(
            {
                "services": ["baemin"],
                "business_id": "all",
                "branch": "전체",
                "date_from": "2026-08-01",
                "date_to": "2026-08-20",
            },
            1200,
        )
    )

    state = auto_collect._completion_state(summary)

    assert state["complete"] is False
    assert state["blocked"] is False
    assert state["retryable_codes"] == ["ATTEMPT_TIMEOUT"]


def test_run_sync_with_timeout_runs_child_process(monkeypatch):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return auto_collect.subprocess.CompletedProcess(
            argv,
            0,
            stdout='{"summary":[{"service":"baemin","status":"succeeded","error_code":"","counts":{"sales":1}}]}',
            stderr="",
        )

    monkeypatch.setattr(auto_collect.subprocess, "run", fake_run)

    summary = auto_collect._run_sync_with_timeout(
        {
            "services": ["baemin"],
            "business_id": "biz-mia",
            "branch": "열정국밥_미아점",
            "date_from": "2026-08-01",
            "date_to": "2026-08-21",
            "close_portal_browser_on_complete": True,
        },
        {"email": "system@aads.local", "is_admin": True},
        3,
    )

    argv, kwargs = calls[0]
    assert "--until-complete" not in argv
    assert "--services" in argv
    assert kwargs["timeout"] == 3
    assert summary["summary"][0]["status"] == "succeeded"


def test_run_sync_with_timeout_splits_multi_service_attempts(monkeypatch):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        service = argv[argv.index("--services") + 1]
        return auto_collect.subprocess.CompletedProcess(
            argv,
            0,
            stdout=(
                '{"summary":[{"service":"'
                + service
                + '","status":"succeeded","error_code":"","counts":{"sales":1}}]}'
            ),
            stderr="",
        )

    monkeypatch.setattr(auto_collect.subprocess, "run", fake_run)

    summary = auto_collect._run_sync_with_timeout(
        {
            "services": ["baemin", "coupangeats"],
            "business_id": "biz-mia",
            "branch": "열정국밥_미아점",
            "date_from": "2026-08-01",
            "date_to": "2026-08-21",
        },
        {"email": "system@aads.local", "is_admin": True},
        3,
    )

    assert [argv[argv.index("--services") + 1] for argv in calls] == ["baemin", "coupangeats"]
    assert "--skip-financial-accounts" in calls[0]
    assert "--skip-financial-accounts" in calls[1]
    assert [item["service"] for item in summary["summary"]] == ["baemin", "coupangeats"]


def test_run_sync_with_timeout_marks_attempt_timeout(monkeypatch):
    marked = []

    def fake_run(argv, **kwargs):
        raise auto_collect.subprocess.TimeoutExpired(cmd=argv, timeout=kwargs["timeout"])

    monkeypatch.setattr(auto_collect.subprocess, "run", fake_run)
    monkeypatch.setattr(
        auto_collect,
        "_mark_timeout_statuses",
        lambda payload, timeout_seconds, attempt_started_at: marked.append(
            (payload, timeout_seconds, attempt_started_at)
        ),
    )
    monkeypatch.setattr(auto_collect, "_latest_status_summary", lambda payload: {"summary": []})

    summary = auto_collect._run_sync_with_timeout(
        {
            "services": ["coupangeats"],
            "business_id": "biz-mia",
            "branch": "열정국밥_미아점",
            "date_from": "2026-08-01",
            "date_to": "2026-08-21",
        },
        {"email": "system@aads.local", "is_admin": True},
        3,
    )

    assert marked[0][1] == 3
    assert summary["summary"][0]["service"] == "coupangeats"
    assert summary["summary"][0]["status"] == "failed"
    assert summary["summary"][0]["error_code"] == "ATTEMPT_TIMEOUT"


def test_run_sync_with_timeout_uses_latest_status_after_child_timeout(monkeypatch):
    def fake_run(argv, **kwargs):
        raise auto_collect.subprocess.TimeoutExpired(cmd=argv, timeout=kwargs["timeout"])

    monkeypatch.setattr(auto_collect.subprocess, "run", fake_run)
    monkeypatch.setattr(auto_collect, "_mark_timeout_statuses", lambda payload, timeout_seconds, attempt_started_at: None)
    monkeypatch.setattr(
        auto_collect,
        "_latest_status_summary",
        lambda payload: {
            "summary": [
                {
                    "service": "baemin",
                    "status": "succeeded",
                    "error_code": "",
                    "counts": {"sales": 1, "settlements": 1, "reviews": 266, "ads": 0},
                }
            ],
            "totals": {"sales": 1, "settlements": 1, "reviews": 266, "ads": 0},
        },
    )

    summary = auto_collect._run_sync_with_timeout(
        {
            "services": ["baemin"],
            "business_id": "biz-mia",
            "branch": "열정국밥_미아점",
            "date_from": "2026-08-01",
            "date_to": "2026-08-21",
        },
        {"email": "system@aads.local", "is_admin": True},
        3,
    )

    assert summary["summary"][0]["status"] == "succeeded"
