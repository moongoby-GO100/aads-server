import json
import time
import asyncio

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


def test_claude_login_requires_fresh_access_and_hides_token(tmp_path):
    credential = tmp_path / ".credentials.json"
    plan = {"kind": "claude", "credential": credential}
    credential.write_text(json.dumps({"claudeAiOauth": {
        "accessToken": "access-secret", "refreshToken": "refresh-secret",
        "expiresAt": int((time.time() + 3600) * 1000),
    }}))
    assert account_login._fresh_claude_credential(plan) is True
    sess = {"login_id": "id", "target": "claude:4", "kind": "claude",
            "account": "slot4", "needs_code": True, "plan": plan,
            "state": "success", "message": "", "url": None, "user_code": None,
            "started_at": time.time()}
    result = account_login.public(sess)
    assert len(result["credential_fingerprint"]) == 64
    assert "secret" not in json.dumps(result)
    credential.write_text(json.dumps({"claudeAiOauth": {
        "accessToken": "access-secret", "refreshToken": "refresh-secret",
        "expiresAt": int((time.time() - 60) * 1000),
    }}))
    assert account_login._fresh_claude_credential(plan) is False


def test_claude_cli_probe_and_slot4_keeper_are_invoked_without_output(monkeypatch):
    calls = []

    class Proc:
        returncode = 0

        async def communicate(self):
            return b'{"result":"OK"}', None

        async def wait(self):
            return 0

    async def create(*args, **kwargs):
        calls.append((args, kwargs))
        return Proc()

    monkeypatch.setattr(account_login.asyncio, "create_subprocess_exec", create)
    plan = {"env": {"HOME": "/isolated/slot4"}}
    assert asyncio.run(account_login._verify_claude_call(plan)) is True
    monkeypatch.setattr(account_login.Path, "is_file", lambda _path: True)
    assert asyncio.run(account_login._sync_slot4_token_to_db()) is True
    assert calls[0][0][0:3] == (account_login.CLAUDE_BIN, "-p", "Reply OK")
    assert calls[0][1]["stderr"] == asyncio.subprocess.DEVNULL
    assert calls[1][0][0:2] == (
        "bash", str(account_login.Path(account_login.__file__).with_name("claude_token_keeper.sh"))
    )
    assert calls[1][1]["env"]["CLAUDE_TOKEN_KEEPER_SLOT"] == "4"
