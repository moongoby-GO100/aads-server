from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _read(name: str) -> str:
    return (ROOT / "scripts" / name).read_text(encoding="utf-8")


def test_runner_templates_accept_all_four_central_leases() -> None:
    primary = _read("pipeline-runner.sh")
    local = _read("pipeline-runner.sh.local")

    assert primary == local
    assert 'CLAUDE_LEASE_SLOT_HOME_ROOT="${CLAUDE_LEASE_SLOT_HOME_ROOT:-/root/.claude-lease}"' in primary
    assert 'leased_slot_token "$slot" >/dev/null 2>&1' in primary
    assert 'via central_lease (access-only)' in primary
    assert 'leased_slot_token 4' in primary
    assert 'TOKEN_SWITCH_SKIP' in primary


def test_refresh_and_distribution_cover_all_four_slots() -> None:
    keeper = _read("claude_token_keeper.sh")
    lease_push = _read("claude-lease-push.sh")
    service = _read("aads-claude-lease-push.service")
    binding = _read("check_account_binding.py")

    assert "for slot in 1 2 3 4; do" in keeper
    assert 'SLOTS="${CLAUDE_LEASE_SLOTS:-1 2 3 4}"' in lease_push
    assert 'Environment="CLAUDE_LEASE_SLOTS=1 2 3 4"' in service
    assert '"ANTHROPIC_AUTH_TOKEN_4": "4"' in binding


def test_remote_lease_never_copies_refresh_tokens() -> None:
    lease_push = _read("claude-lease-push.sh")

    emitted = lease_push.split("json.dump(", 1)[1].split("sys.stdout", 1)[0]
    assert '"accessToken": token' in emitted
    assert '"refreshToken"' not in emitted
