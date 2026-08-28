from types import SimpleNamespace

import pytest

import scripts.yeoljeong_auto_collect as auto_collect
from app.services.bank_collection_lock import release_bank_lock, try_acquire_bank_lock


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
    assert payload["force_recreate_bank_browser"] is False
    assert payload["close_portal_browser_on_complete"] is True


def test_payload_maps_browser_agent_to_delivery_pc_agent():
    args = auto_collect.build_parser().parse_args(["--browser-agent-id", "agent-food"])

    payload = auto_collect._payload(args)
    argv = auto_collect._child_collect_argv(payload)

    assert payload["browser_agent_id"] == "agent-food"
    assert payload["pc_agent_id"] == "agent-food"
    assert payload["prefer_pc_agent"] is True
    assert payload["require_pc_agent"] is True
    assert argv[argv.index("--browser-agent-id") + 1] == "agent-food"


def test_payload_and_child_argv_preserve_baemin_full_backfill_options():
    args = auto_collect.build_parser().parse_args(
        [
            "--services",
            "baemin",
            "--business-id",
            "all",
            "--branch",
            "전체",
            "--mode",
            "full_backfill",
            "--date-from",
            "2026-01-01",
            "--date-to",
            "2026-08-25",
            "--max-orders",
            "200",
            "--max-reviews",
            "150",
        ]
    )

    payload = auto_collect._payload(args)
    argv = auto_collect._child_collect_argv(payload)

    assert payload["all_businesses"] is True
    assert payload["mode"] == "full_backfill"
    assert payload["max_orders"] == 200
    assert payload["max_reviews"] == 150
    assert argv[argv.index("--mode") + 1] == "full_backfill"
    assert argv[argv.index("--max-orders") + 1] == "200"
    assert argv[argv.index("--max-reviews") + 1] == "150"


def test_payload_can_keep_browser_open_for_manual_debugging():
    args = auto_collect.build_parser().parse_args(["--keep-browser-open"])

    payload = auto_collect._payload(args)

    assert payload["close_portal_browser_on_complete"] is False


def test_payload_can_force_recreate_only_bank_browser():
    args = auto_collect.build_parser().parse_args(["--force-recreate-bank-browser"])

    payload = auto_collect._payload(args)
    argv = auto_collect._child_collect_argv(payload)

    assert payload["force_recreate_portal_sessions"] is False
    assert payload["force_recreate_bank_browser"] is True
    assert "--force-recreate-bank-browser" in argv
    assert "--force-recreate-sessions" not in argv


def test_until_complete_payload_collects_financial_accounts_by_default():
    args = auto_collect.build_parser().parse_args(["--until-complete"])

    payload = auto_collect._payload(args)

    assert payload["skip_financial_accounts"] is False


def test_bank_only_skips_delivery_and_collects_bank_accounts(monkeypatch):
    calls = []

    def fail_delivery_sync(payload, user, *, queue_only=False):
        raise AssertionError("bank-only collection must not call delivery sync")

    monkeypatch.setattr(auto_collect, "_run_sync", fail_delivery_sync)
    monkeypatch.setattr(
        auto_collect,
        "list_bank_accounts",
        lambda user, business_id=None, *, branch_id=None, status=None: [
            {
                "id": "bank-browser-1",
                "business_id": business_id,
                "branch_id": branch_id,
                "connection_type": "browser",
                "auto_sync": True,
            }
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
                "connection_type": "browser",
                "collected_rows": 1,
                "imported_rows": 1,
                "duplicate_rows": 0,
                "message": "은행 거래 수집이 완료되었습니다.",
            },
            "transactions": [],
        }

    monkeypatch.setattr(auto_collect, "collect_bank_account_transactions", fake_collect)

    args = auto_collect.build_parser().parse_args(
        [
            "--bank-only",
            "--services",
            "baemin",
            "--business-id",
            "biz-mia",
            "--branch",
            "열정국밥_미아점",
            "--date-from",
            "2026-08-26",
            "--date-to",
            "2026-08-26",
            "--bank-browser-work-key",
            "yeoljeong-bank-shinhan-individual-recovery",
        ]
    )
    payload = auto_collect._payload(args)
    argv = auto_collect._child_collect_argv(payload)
    result = auto_collect._run_collectors(payload, {"email": "system@aads.local", "is_admin": True})

    assert payload["bank_only"] is True
    assert "--bank-only" in argv
    assert "--bank-browser-work-key" in argv
    assert argv[argv.index("--bank-browser-work-key") + 1] == "yeoljeong-bank-shinhan-individual-recovery"
    assert [call[0] for call in calls] == ["bank-browser-1"]
    assert calls[0][1]["browser_work_key"] == "yeoljeong-bank-shinhan-individual-recovery"
    assert result["summary"] == []
    assert result["bank_totals"]["imported_rows"] == 1


def test_bank_scope_accepts_mia_branch_alias(monkeypatch):
    calls = []

    def fake_list_bank_accounts(user, business_id=None, *, branch_id=None, status=None):
        calls.append({"business_id": business_id, "branch_id": branch_id, "status": status})
        return [
            {
                "id": "bank-mia",
                "business_id": business_id,
                "branch_id": branch_id,
                "connection_type": "browser",
                "auto_sync": True,
            }
        ]

    monkeypatch.setattr(auto_collect, "list_bank_accounts", fake_list_bank_accounts)

    accounts = auto_collect._bank_accounts_for_payload(
        {"business_id": "biz-mia", "branch": "미아점"},
        {"email": "system@aads.local", "is_admin": True},
    )

    assert [account["id"] for account in accounts] == ["bank-mia"]
    assert calls == [{"business_id": "biz-mia", "branch_id": "branch-gangbuk-mia", "status": "active"}]


def test_bank_only_promotes_ibk_quick_platform_account_to_browser_bank_account(monkeypatch):
    created = []

    monkeypatch.setattr(
        auto_collect,
        "list_platform_accounts",
        lambda user, business_id=None: [
            {
                "id": "platform-ibk",
                "service": "ibk_business",
                "label": "중화점 기업은행 기업",
                "business_id": "biz-junghwa",
                "branch": "중화점",
                "collection_mode": "bank-quick-service",
                "status": "credential_registered",
                "auto_sync": True,
                "account_no_masked": "**********4014",
            }
        ],
    )

    def fake_list_bank_accounts(user, business_id=None, *, branch_id=None, status=None):
        return list(created) if status == "active" else []

    def fake_create_bank_account(payload, user):
        record = {
            "id": "bank-ibk",
            "business_id": payload["business_id"],
            "branch_id": payload["branch_id"],
            "bank_code": payload["bank_code"],
            "bank_name": payload["bank_name"],
            "institution_code": payload["institution_code"],
            "connection_type": payload["connection_type"],
            "auto_sync": payload["auto_sync"],
        }
        created.append(record)
        return record

    monkeypatch.setattr(auto_collect, "list_bank_accounts", fake_list_bank_accounts)
    monkeypatch.setattr(auto_collect, "create_bank_account", fake_create_bank_account)

    accounts = auto_collect._bank_accounts_for_payload(
        {"business_id": "biz-junghwa", "branch": "중화점"},
        {"email": "system@aads.local", "is_admin": True},
    )

    assert [account["id"] for account in accounts] == ["bank-ibk"]
    assert created[0]["bank_code"] == "003"
    assert created[0]["institution_code"] == "ibk_business"
    assert created[0]["connection_type"] == "browser"


def test_bank_accounts_for_payload_dedupes_same_scope_and_bank(monkeypatch):
    monkeypatch.setattr(
        auto_collect,
        "list_bank_accounts",
        lambda user, business_id=None, *, branch_id=None, status=None: [
            {
                "id": "bank-ibk-1",
                "business_id": "biz-junghwa",
                "branch_id": "branch-junghwa",
                "bank_code": "003",
                "bank_name": "IBK기업은행",
                "institution_code": "ibk_business",
                "connection_type": "browser",
                "auto_sync": True,
            },
            {
                "id": "bank-ibk-2",
                "business_id": "biz-junghwa",
                "branch_id": "branch-junghwa",
                "bank_code": "003",
                "bank_name": "IBK기업은행",
                "institution_code": "ibk_business",
                "connection_type": "browser",
                "auto_sync": True,
            },
        ],
    )

    accounts = auto_collect._bank_accounts_for_payload(
        {"business_id": "biz-junghwa", "branch": "중화점"},
        {"email": "system@aads.local", "is_admin": True},
    )

    assert [account["id"] for account in accounts] == ["bank-ibk-1"]


def test_skip_financial_accounts_still_disables_bank_collection():
    args = auto_collect.build_parser().parse_args(["--until-complete", "--skip-financial-accounts"])

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


def test_bank_account_collection_passes_browser_auto_open_controls(monkeypatch):
    calls = []

    monkeypatch.setattr(
        auto_collect,
        "list_bank_accounts",
        lambda user, business_id=None, *, branch_id=None, status=None: [
            {
                "id": "bank-browser-1",
                "business_id": business_id,
                "branch_id": branch_id,
                "connection_type": "browser",
                "auto_sync": True,
            }
        ],
    )

    def fake_collect(account_id, payload, user):
        calls.append((account_id, payload))
        return {
            "collection": {
                "bank_account_id": account_id,
                "business_id": payload["business_id"],
                "branch_id": payload["branch_id"],
                "status": "action_required",
                "connector_status": "ACTION_REQUIRED",
                "connection_type": "browser",
                "error_code": "BANK_BROWSER_OPERATOR_ACTION_REQUIRED",
                "collected_rows": 0,
                "imported_rows": 0,
                "duplicate_rows": 0,
                "message": "기업페이지 승인 필요",
            },
            "transactions": [],
        }

    monkeypatch.setattr(auto_collect, "collect_bank_account_transactions", fake_collect)

    result = auto_collect._collect_bank_accounts(
        {
            "business_id": "biz-mia",
            "branch": "열정국밥_미아점",
            "date_from": "2026-08-01",
            "date_to": "2026-08-21",
            "browser_session_id": "bank-session-1",
            "browser_agent_id": "oby-ceo",
            "browser_preferred_port": 9333,
            "force_recreate_portal_sessions": True,
        },
        {"email": "system@aads.local", "is_admin": True},
    )

    assert [call[0] for call in calls] == ["bank-browser-1"]
    collect_payload = calls[0][1]
    assert collect_payload["browser_session_id"] == "bank-session-1"
    assert collect_payload["auto_open_browser"] is True
    assert collect_payload["browser_agent_id"] == "oby-ceo"
    assert collect_payload["browser_preferred_port"] == 9333
    assert collect_payload["force_recreate_browser"] is False
    assert result[0]["error_code"] == "BANK_BROWSER_OPERATOR_ACTION_REQUIRED"


def test_bank_account_collection_passes_bank_specific_force_recreate(monkeypatch):
    calls = []

    monkeypatch.setattr(
        auto_collect,
        "list_bank_accounts",
        lambda user, business_id=None, *, branch_id=None, status=None: [
            {
                "id": "bank-browser-1",
                "business_id": business_id,
                "branch_id": branch_id,
                "connection_type": "browser",
                "auto_sync": True,
            }
        ],
    )

    def fake_collect(account_id, payload, user):
        calls.append((account_id, payload))
        return {
            "collection": {
                "bank_account_id": account_id,
                "business_id": payload["business_id"],
                "branch_id": payload["branch_id"],
                "status": "action_required",
                "connector_status": "ACTION_REQUIRED",
                "connection_type": "browser",
                "error_code": "BANK_BROWSER_OPERATOR_ACTION_REQUIRED",
                "collected_rows": 0,
                "imported_rows": 0,
                "duplicate_rows": 0,
            },
            "transactions": [],
        }

    monkeypatch.setattr(auto_collect, "collect_bank_account_transactions", fake_collect)

    auto_collect._collect_bank_accounts(
        {
            "business_id": "biz-mia",
            "branch": "열정국밥_미아점",
            "force_recreate_bank_browser": True,
        },
        {"email": "system@aads.local", "is_admin": True},
    )

    assert calls[0][1]["force_recreate_browser"] is True


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
                "id": "legacy-card-1",
                "service": "card_pg",
                "business_id": business_id,
                "branch": "중화점",
                "collection_mode": "api",
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
                    "service": "card_pg",
                    "status": "connector_not_configured",
                    "message": "커넥터가 아직 연결되지 않았습니다.",
                    "account_id": payload["account_id"],
                    "collection_mode": "api",
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
            "services": ["card_pg"],
            "account_id": "legacy-card-1",
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


def test_completion_state_blocks_on_bank_browser_operator_action_required():
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
                "error_code": "BANK_BROWSER_OPERATOR_ACTION_REQUIRED",
                "counts": {"transactions": 0},
            }
        ],
    }

    state = auto_collect._completion_state(summary)

    assert state["complete"] is False
    assert state["blocked"] is True
    assert state["pending"] == 1
    assert state["blocking_codes"] == ["BANK_BROWSER_OPERATOR_ACTION_REQUIRED"]


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

    def fake_run_sync_with_timeout(payload, user, timeout):
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
    monkeypatch.setattr(auto_collect, "_run_sync_with_timeout", fake_run_sync_with_timeout)
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

    def fake_run_sync_with_timeout(payload, user, timeout):
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
    monkeypatch.setattr(auto_collect, "_run_sync_with_timeout", fake_run_sync_with_timeout)
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


def test_until_complete_force_recreates_bank_browser_after_cdp_not_ready(monkeypatch):
    calls = []
    sleeps = []
    results = [
        {
            "bank_collections": [
                {
                    "service": "bank",
                    "status": "failed",
                    "error_code": "CDP_NOT_READY",
                    "counts": {"transactions": 0},
                }
            ]
        },
        {
            "bank_collections": [
                {
                    "service": "bank",
                    "status": "completed",
                    "error_code": "",
                    "counts": {"transactions": 1},
                }
            ]
        },
    ]

    def fake_run_sync_with_timeout(payload, user, timeout):
        calls.append(dict(payload))
        return results.pop(0)

    monkeypatch.setattr(
        auto_collect,
        "_payload",
        lambda args: {
            "business_id": "all",
            "force_recreate_portal_sessions": False,
            "force_recreate_bank_browser": False,
        },
    )
    monkeypatch.setattr(auto_collect, "_run_sync_with_timeout", fake_run_sync_with_timeout)
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
    assert calls[0]["force_recreate_bank_browser"] is False
    assert calls[1]["force_recreate_bank_browser"] is True
    assert calls[1]["force_recreate_portal_sessions"] is False
    assert sleeps == [7]


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


def test_until_complete_stops_on_raw_baemin_security_blocked(monkeypatch):
    sleeps = []

    monkeypatch.setattr(auto_collect, "_payload", lambda args: {"business_id": "all"})
    monkeypatch.setattr(
        auto_collect,
        "_run_sync",
        lambda payload, user, *, queue_only=False: {
            "summary": [
                {
                    "service": "baemin",
                    "status": "action_required",
                    "error_code": "BAEMIN_SECURITY_BLOCKED",
                    "counts": {"sales": 0, "settlements": 0, "reviews": 0, "ads": 0},
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


def test_run_sync_with_timeout_multi_service_collects_bank_once(monkeypatch):
    child_calls = []
    bank_calls = []

    def fake_run(argv, **kwargs):
        child_calls.append(argv)
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
            }
        ],
    )

    def fake_collect(account_id, payload, user):
        bank_calls.append((account_id, payload))
        return {
            "collection": {
                "bank_account_id": account_id,
                "business_id": payload["business_id"],
                "branch_id": payload["branch_id"],
                "status": "completed",
                "connector_status": "CONFIGURED",
                "connection_type": "mock",
                "collected_rows": 1,
                "imported_rows": 1,
                "duplicate_rows": 0,
            },
            "transactions": [],
        }

    monkeypatch.setattr(auto_collect, "collect_bank_account_transactions", fake_collect)

    summary = auto_collect._run_sync_with_timeout(
        {
            "services": ["baemin", "ddangyo"],
            "business_id": "biz-mia",
            "branch": "열정국밥_미아점",
            "date_from": "2026-08-01",
            "date_to": "2026-08-24",
        },
        {"email": "system@aads.local", "is_admin": True},
        120,
    )

    assert len(child_calls) == 2
    assert [call[0] for call in bank_calls] == ["bank-1"]
    assert summary["bank_totals"]["accounts"] == 1
    assert summary["bank_totals"]["imported_rows"] == 1


def test_run_sync_with_timeout_applies_total_deadline_to_multi_service(monkeypatch):
    calls = []
    times = iter([100.0, 100.0, 103.1])

    def fake_monotonic():
        return next(times)

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
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

    monkeypatch.setattr(auto_collect.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(auto_collect.subprocess, "run", fake_run)
    monkeypatch.setattr(auto_collect, "_mark_timeout_statuses", lambda *args, **kwargs: None)

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

    assert len(calls) == 1
    assert calls[0][1]["timeout"] == 3
    assert [item["service"] for item in summary["summary"]] == ["baemin", "coupangeats"]
    assert summary["summary"][1]["error_code"] == "ATTEMPT_TIMEOUT"


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


def test_main_applies_timeout_to_direct_cli_run(monkeypatch, capsys):
    calls = []

    def fake_run_sync_with_timeout(payload, user, timeout_seconds):
        calls.append((payload, user, timeout_seconds))
        return {
            "summary": [
                {
                    "service": "coupangeats",
                    "status": "failed",
                    "error_code": "ATTEMPT_TIMEOUT",
                    "counts": {"sales": 0, "settlements": 0, "reviews": 0, "ads": 0},
                }
            ]
        }

    monkeypatch.setattr(auto_collect, "_run_sync_with_timeout", fake_run_sync_with_timeout)
    monkeypatch.setattr(auto_collect, "_run_collectors", lambda *args, **kwargs: pytest.fail("unexpected direct run"))

    exit_code = auto_collect.main(
        [
            "--services",
            "coupangeats",
            "--business-id",
            "biz-mia",
            "--branch",
            "열정국밥_미아점",
            "--attempt-timeout-seconds",
            "7",
        ]
    )

    assert exit_code == 0
    assert calls[0][0]["services"] == ["coupangeats"]
    assert calls[0][2] == 7
    assert "ATTEMPT_TIMEOUT" in capsys.readouterr().out


def test_main_child_no_timeout_runs_collectors_without_recursing(monkeypatch):
    calls = []

    def fake_run_collectors(payload, user, *, queue_only=False):
        calls.append((payload, user, queue_only))
        return {
            "summary": [
                {
                    "service": "baemin",
                    "status": "succeeded",
                    "error_code": "",
                    "counts": {"sales": 1, "settlements": 0, "reviews": 0, "ads": 0},
                }
            ]
        }

    monkeypatch.setattr(auto_collect, "_run_collectors", fake_run_collectors)
    monkeypatch.setattr(auto_collect, "_run_sync_with_timeout", lambda *args, **kwargs: pytest.fail("unexpected recursion"))

    exit_code = auto_collect.main(["--services", "baemin", "--child-no-timeout"])

    assert exit_code == 0
    assert calls[0][0]["services"] == ["baemin"]
    assert calls[0][2] is False


def test_global_queue_enqueues_delivery_scopes(monkeypatch):
    enqueued = []

    monkeypatch.setattr(
        auto_collect,
        "_delivery_requested_services",
        lambda payload: ["coupangeats", "ddangyo"],
    )
    monkeypatch.setattr(
        auto_collect,
        "_delivery_sync_window",
        lambda payload: (
            auto_collect.datetime(2026, 8, 1, tzinfo=auto_collect.KST).date(),
            auto_collect.datetime(2026, 8, 28, tzinfo=auto_collect.KST).date(),
        ),
    )
    monkeypatch.setattr(
        auto_collect,
        "_delivery_sync_scopes",
        lambda payload, services, accounts: [("biz-mia", "열정국밥_미아점")],
    )
    monkeypatch.setattr(
        auto_collect,
        "enqueue_collection_items",
        lambda items: {
            "count": len(items),
            "items": [dict(item, id=f"queue-{index}", status="queued") for index, item in enumerate(items)],
        },
    )
    monkeypatch.setattr(auto_collect, "queue_snapshot", lambda limit=20: [])

    result = auto_collect._enqueue_global_collection_queue(
        {
            "services": ["coupangeats", "ddangyo"],
            "business_id": "all",
            "branch": "전체",
            "all_businesses": True,
            "skip_financial_accounts": True,
        },
        {"email": "system@aads.local", "is_admin": True},
    )
    enqueued.extend(result["items"])

    assert result["global_queue"] is True
    assert result["count"] == 2
    assert [item["service"] for item in enqueued] == ["coupangeats", "ddangyo"]
    assert all(item["status"] == "queued" for item in enqueued)


def test_drain_global_queue_claims_and_completes(monkeypatch):
    completed = []

    monkeypatch.setattr(
        auto_collect,
        "claim_next_collection_item",
        lambda agent_id="": {
            "id": "queue-1",
            "service": "coupangeats",
            "business_id": "biz-mia",
            "branch": "미아점",
            "payload": {
                "services": ["coupangeats"],
                "business_id": "biz-mia",
                "branch": "미아점",
            },
        },
    )
    monkeypatch.setattr(
        auto_collect,
        "_run_collectors",
        lambda payload, user, *, queue_only=False: {
            "summary": [
                {
                    "service": "coupangeats",
                    "status": "succeeded",
                    "error_code": "",
                    "counts": {"sales": 1, "settlements": 0, "reviews": 0, "ads": 0},
                }
            ],
            "bank_collections": [],
        },
    )
    monkeypatch.setattr(
        auto_collect,
        "complete_collection_item",
        lambda item_id, **kwargs: completed.append((item_id, kwargs)) or {"id": item_id, **kwargs},
    )

    result = auto_collect._run_global_collection_queue_once({"is_admin": True}, agent_id="agent-1")

    assert result["claimed"] is True
    assert result["status"] == "succeeded"
    assert completed[0][0] == "queue-1"
    assert completed[0][1]["status"] == "succeeded"


def test_bank_only_defers_when_bank_pc_agent_lock_is_held(tmp_path, monkeypatch):
    lock_path = tmp_path / "bank.lock"
    monkeypatch.setenv("YEOLJEONG_BANK_AUTO_COLLECT_LOCK_PATH", str(lock_path))
    holder = try_acquire_bank_lock(lock_path)
    assert holder is not None
    try:
        result = auto_collect._run_collectors(
            {"bank_only": True, "business_id": "biz-mia", "branch": "branch-gangbuk-mia"},
            {"is_admin": True},
        )
    finally:
        release_bank_lock(holder)

    item = result["bank_collections"][0]
    assert item["status"] == "deferred"
    assert item["error_code"] == "BANK_COLLECTION_DEFERRED_DUE_TO_ACTIVE_BANK_LOCK"
