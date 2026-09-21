import json

from scripts import account_login


def test_claude_binding_exposes_identity_without_oauth_secrets(tmp_path, monkeypatch):
    root = tmp_path / "slots"
    home = root / "slot2"
    claude_dir = home / ".claude"
    claude_dir.mkdir(parents=True)
    (claude_dir / ".credentials.json").write_text(json.dumps({
        "claudeAiOauth": {
            "accessToken": "access-secret",
            "refreshToken": "refresh-secret",
            "subscriptionType": "max",
        }
    }))
    (home / ".claude.json").write_text(json.dumps({
        "oauthAccount": {
            "emailAddress": "wrong@example.com",
            "organizationUuid": "org-123",
        }
    }))
    monkeypatch.setattr(account_login, "CLAUDE_SLOT_ROOT", root)
    monkeypatch.setattr(account_login, "CODEX_ACCOUNTS_ROOT", tmp_path / "codex")

    result = account_login.bindings()

    assert result == [{
        "target": "claude:2",
        "kind": "claude",
        "account": "slot2",
        "bound": True,
        "needs_login": False,
        "subscription": "max",
        "actual_account": "wrong@example.com",
        "login_in_progress": False,
        "login_id": None,
    }]
    assert "secret" not in json.dumps(result)


def test_existing_credential_is_not_success_until_replaced(tmp_path):
    credential = tmp_path / ".credentials.json"
    credential.write_text(json.dumps({
        "claudeAiOauth": {"accessToken": "old", "refreshToken": "old-refresh"}
    }))
    plan = {"kind": "claude", "credential": credential}
    sess = {
        "plan": plan,
        "credential_before": account_login._credential_signature(plan),
    }

    assert account_login._credential_replaced(sess) is False

    credential.write_text(json.dumps({
        "claudeAiOauth": {"accessToken": "new", "refreshToken": "new-refresh"}
    }))
    assert account_login._credential_replaced(sess) is True
