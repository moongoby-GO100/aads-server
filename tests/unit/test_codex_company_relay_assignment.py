from pathlib import Path

from scripts import claude_relay_server as relay


def test_company_account_order_rebinds_to_first_allowed(monkeypatch, tmp_path: Path):
    main = tmp_path / "accounts" / "CODEX_OAUTH_MAIN" / "auth.json"
    jinah = tmp_path / "accounts" / "CODEX_OAUTH_JINAH" / "auth.json"
    main.parent.mkdir(parents=True)
    jinah.parent.mkdir(parents=True)
    main.write_text("{}")
    jinah.write_text("{}")

    monkeypatch.setattr(relay, "_codex_accounts", lambda: [
        (1, "CODEX_OAUTH_MAIN", main),
        (2, "CODEX_OAUTH_JINAH", jinah),
    ])
    codex_dir = tmp_path / "session" / ".codex"
    codex_dir.mkdir(parents=True)
    (codex_dir / "account.json").write_text('{"key_name":"CODEX_OAUTH_MAIN"}')

    selected, how = relay._pick_codex_account(
        "session-1", codex_dir,
        account_order=["CODEX_OAUTH_JINAH", "CODEX_OAUTH_MAIN"],
    )

    assert selected == jinah
    assert how == "rebound"
    assert "CODEX_OAUTH_JINAH" in (codex_dir / "account.json").read_text()


def test_company_account_order_excludes_other_company_accounts(monkeypatch, tmp_path: Path):
    main = tmp_path / "accounts" / "CODEX_OAUTH_MAIN" / "auth.json"
    jinah = tmp_path / "accounts" / "CODEX_OAUTH_JINAH" / "auth.json"
    main.parent.mkdir(parents=True)
    jinah.parent.mkdir(parents=True)
    main.write_text("{}")
    jinah.write_text("{}")
    monkeypatch.setattr(relay, "_codex_accounts", lambda: [
        (1, "CODEX_OAUTH_MAIN", main),
        (2, "CODEX_OAUTH_JINAH", jinah),
    ])
    codex_dir = tmp_path / "session" / ".codex"
    codex_dir.mkdir(parents=True)

    selected, _ = relay._pick_codex_account(
        "session-2", codex_dir, account_order=["CODEX_OAUTH_MAIN"],
    )

    assert selected == main
