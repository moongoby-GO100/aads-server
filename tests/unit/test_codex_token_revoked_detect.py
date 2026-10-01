"""AADS-CODEX-TOKEN-REVOKED-DETECT-20261001: 서버측 token_revoked(401) 탐지.

2026-10-01 CODEX_OAUTH_JINAH 는 로컬 토큰이 멀쩡한데 서버가 401 token_revoked 로 거부했다.
스냅샷은 auth_usable=true 였고 러너는 그 계정을 1순위로 골라 매 시도를 401 로 태웠다.

고정하는 계약
  - codex_usage.auth_usable: 로컬 만료(기존) + 서버 응답의 401 폐기 신호
  - 폐기 통보는 새로 감지했을 때만, 같은 계정은 6시간 쿨다운
  - codex_pick_account_home(셸): auth_usable=false 와 계정별 폐기 마커를 건너뛰고 다음 priority
  - is_active 를 코드가 바꾸지 않는다
"""

import base64
import importlib.util
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
RUNNER = (SCRIPTS / "pipeline-runner.sh").read_text(encoding="utf-8")

REVOKED_BODY = (
    'ERROR: workspace routing discovery unauthorized (401)\n'
    '{"error":{"message":"Encountered invalidated oauth token for user",'
    '"code":"token_revoked"},"status":401}'
)


@pytest.fixture(scope="module")
def cu():
    spec = importlib.util.spec_from_file_location("codex_usage_under_test", SCRIPTS / "codex_usage.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _jwt(exp: float) -> str:
    def seg(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")

    return f"{seg({'alg': 'none'})}.{seg({'exp': int(exp)})}.sig"


def _auth_file(tmp_path: Path, exp: float, name: str = "auth.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps({"tokens": {"access_token": _jwt(exp)}}))
    return path


# ── (a)(b)(c) auth_usable ────────────────────────────────────────────

def test_a_revoked_401_response_makes_auth_unusable(cu, tmp_path):
    path = _auth_file(tmp_path, time.time() + 3600)
    assert cu.auth_usable(path, REVOKED_BODY) is False


@pytest.mark.parametrize("text", [
    'status 401 {"code":"token_revoked"}',
    "401 Unauthorized: Encountered invalidated oauth token for user",
    "ERROR: workspace routing discovery unauthorized (401)",
])
def test_a_each_signal_is_detected(cu, text):
    assert cu.revoked_reason(text)


@pytest.mark.parametrize("text", [
    "",
    "401 Unauthorized",  # 폐기 문구 없음 — 일시적 인증 오류와 구분이 안 된다
    '{"code":"token_revoked"}',  # 401 없음
    "HTTP 4010 token_revoked",  # 401 이 아닌 숫자
    "rate limit reached, try again later",
])
def test_a_requires_401_together_with_a_signal(cu, text):
    assert cu.revoked_reason(text) == ""


def test_b_normal_response_stays_usable(cu, tmp_path):
    path = _auth_file(tmp_path, time.time() + 3600)
    assert cu.auth_usable(path) is True
    assert cu.auth_usable(path, '{"primary":{"usedPercent":96}}') is True


def test_c_local_expiry_still_unusable(cu, tmp_path):
    expired = _auth_file(tmp_path, time.time() - 10)
    assert cu.auth_usable(expired) is False
    assert cu.auth_usable(expired, "") is False
    assert cu.auth_usable(tmp_path / "missing.json") is False


def test_reason_is_a_fixed_tag_never_the_raw_text(cu):
    leaked = REVOKED_BODY + "\nBearer sk-ant-oat01-SECRETSECRETSECRET refresh_token=rt_ABCDEF"
    reason = cu.revoked_reason(leaked)
    assert reason.startswith("401 ")
    assert "SECRET" not in reason and "rt_" not in reason and "Bearer" not in reason


# ── 판정 결합 ────────────────────────────────────────────────────────

def test_resolve_revoked_transitions(cu):
    now = 10_000.0
    # 새로 감지
    assert cu.resolve_revoked("", None, "401 token_revoked", False, None, now) == ("401 token_revoked", now)
    # 이미 폐기 중이면 감지 시각 유지
    assert cu.resolve_revoked("401 token_revoked", 5_000.0, "401 token_revoked", False, None, now) == (
        "401 token_revoked", 5_000.0)
    # 실시간 조회 성공 → 해제
    assert cu.resolve_revoked("401 token_revoked", 5_000.0, "", True, None, now) == ("", None)
    # 결론이 안 나는 조회 → 이전 판정 유지(깜박임 방지)
    assert cu.resolve_revoked("401 token_revoked", 5_000.0, "", False, 4_000.0, now) == (
        "401 token_revoked", 5_000.0)
    # 감지 뒤 auth.json 재작성(재로그인) → 낡은 판정 폐기
    assert cu.resolve_revoked("401 token_revoked", 5_000.0, "", False, 6_000.0, now) == ("", None)


def test_effective_auth_usable(cu):
    assert cu.effective_auth_usable(True, True, "", None, None) is True
    # 스냅샷이 false 면 false (스냅샷 없음은 호출부가 True 를 준다)
    assert cu.effective_auth_usable(True, False, "", None, None) is False
    # 로컬 만료는 폐기 여부와 무관하게 false
    assert cu.effective_auth_usable(False, True, "", None, None) is False
    # 폐기 중
    assert cu.effective_auth_usable(True, False, "401 token_revoked", 5_000.0, 4_000.0) is False
    # 재로그인(auth.json 이 감지 뒤에 다시 쓰임) → 다음 수집을 기다리지 않고 복귀
    assert cu.effective_auth_usable(True, False, "401 token_revoked", 5_000.0, 6_000.0) is True


# ── (d) 통보 쿨다운 ──────────────────────────────────────────────────

@pytest.fixture
def notify_env(cu, tmp_path, monkeypatch):
    monkeypatch.setattr(cu, "ACCOUNTS_ROOT", tmp_path)
    monkeypatch.setattr(cu, "NOTIFY_STATE_FILE", tmp_path / "revoked_notified.json")
    sent = []

    def sender(msg):
        sent.append(msg)
        return True

    return sent, sender


def _acct(reason="401 token_revoked", at=1_790_000_000.0):
    return {"key_name": "CODEX_OAUTH_JINAH", "label": "jinah", "auth_revoked_reason": reason,
            "auth_revoked_at_epoch": at}


def test_d_repeated_detection_notifies_once(cu, notify_env):
    sent, sender = notify_env
    now = 1_790_000_100.0
    assert cu.notify_revoked([_acct()], now, sender) == ["CODEX_OAUTH_JINAH"]
    assert cu.notify_revoked([_acct()], now + 600, sender) == []
    assert cu.notify_revoked([_acct()], now + 7 * 3600, sender) == []  # 같은 폐기 상태가 이어지는 동안
    assert len(sent) == 1


def test_d_message_has_required_fields_and_no_secrets(cu, notify_env):
    sent, sender = notify_env
    cu.notify_revoked([_acct()], 1_790_000_100.0, sender)
    msg = sent[0]
    assert "CODEX_OAUTH_JINAH" in msg and "jinah" in msg
    assert "KST" in msg and "codex login" in msg and "401 token_revoked" in msg
    assert "access_token" not in msg and "refresh_token" not in msg


def test_d_flap_within_cooldown_is_suppressed_then_allowed(cu, notify_env):
    sent, sender = notify_env
    t0 = 1_790_000_000.0
    assert cu.notify_revoked([_acct()], t0, sender)
    assert cu.notify_revoked([_acct(reason="")], t0 + 600, sender) == []  # 복구
    assert cu.notify_revoked([_acct()], t0 + 1200, sender) == []  # 6시간 안 재폐기 → 억제
    assert cu.notify_revoked([_acct(reason="")], t0 + 1800, sender) == []
    assert cu.notify_revoked([_acct()], t0 + 6 * 3600 + 10, sender) == ["CODEX_OAUTH_JINAH"]
    assert len(sent) == 2


def test_d_failed_send_is_retried_next_cycle(cu, notify_env):
    _, _ = notify_env
    calls = []

    def failing(msg):
        calls.append(msg)
        return False

    assert cu.notify_revoked([_acct()], 1_790_000_000.0, failing) == []
    ok = []
    assert cu.notify_revoked([_acct()], 1_790_000_600.0, lambda m: ok.append(m) or True)
    assert len(calls) == 1 and len(ok) == 1


def test_d_healthy_account_never_notifies(cu, notify_env):
    sent, sender = notify_env
    assert cu.notify_revoked([_acct(reason="")], 1_790_000_000.0, sender) == []
    assert sent == []


# ── DB 방어: 컬럼 없는 DB 에서도 죽지 않는다 ─────────────────────────

def test_db_accounts_falls_back_when_revoke_columns_missing(cu, tmp_path, monkeypatch):
    monkeypatch.setattr(cu, "ACCOUNTS_ROOT", tmp_path)
    home = tmp_path / "CODEX_OAUTH_JINAH"
    home.mkdir()
    _auth_file(home, time.time() + 3600)
    seen = []

    def fake_psql(sql):
        seen.append(sql)
        if "auth_revoked" in sql:
            raise RuntimeError('psql 실패: column "auth_revoked_reason" does not exist')
        return [["CODEX_OAUTH_JINAH", "jinah", "9", "f", "", "f", "", ""]]

    monkeypatch.setattr(cu, "psql", fake_psql)
    (acct,) = cu.db_accounts()
    assert len(seen) == 2
    assert acct["is_active"] is False
    assert acct["has_auth"] is True
    assert acct["auth_usable"] is False  # 스냅샷 auth_usable=false 를 따른다


def test_db_accounts_without_snapshot_defaults_to_usable(cu, tmp_path, monkeypatch):
    monkeypatch.setattr(cu, "ACCOUNTS_ROOT", tmp_path)
    home = tmp_path / "CODEX_OAUTH_MAIN"
    home.mkdir()
    _auth_file(home, time.time() + 3600)
    monkeypatch.setattr(cu, "psql", lambda sql: [["CODEX_OAUTH_MAIN", "m", "2", "t", "", "", "", ""]])
    (acct,) = cu.db_accounts()
    assert acct["auth_usable"] is True
    assert acct["is_active"] is True


def test_push_snapshots_falls_back_to_old_upsert(cu, monkeypatch):
    monkeypatch.setattr(cu, "_revoke_columns_ok", True)
    seen = []

    def fake_psql(sql):
        seen.append(sql)
        if "auth_revoked" in sql:
            raise RuntimeError('psql 실패: column "auth_revoked_reason" of relation does not exist')
        return []

    monkeypatch.setattr(cu, "psql", fake_psql)
    acct = {"key_name": "K", "auth_usable": False, "auth_revoked_reason": "401 token_revoked",
            "auth_revoked_at_epoch": 1_790_000_000}
    cu.push_snapshots([acct], {})
    cu.push_snapshots([acct], {})
    assert "auth_revoked_reason" in seen[0]
    assert all("auth_revoked" not in q for q in seen[1:])
    assert "FALSE" in seen[1]
    assert len(seen) == 3  # 첫 시도(실패) + 후퇴 + 이후엔 바로 옛 UPSERT


def test_push_snapshots_writes_reason_when_columns_exist(cu, monkeypatch):
    monkeypatch.setattr(cu, "_revoke_columns_ok", True)
    seen = []
    monkeypatch.setattr(cu, "psql", lambda sql: seen.append(sql) or [])
    cu.push_snapshots([{"key_name": "K", "auth_usable": False, "auth_revoked_reason": "401 token_revoked",
                        "auth_revoked_at_epoch": 1_790_000_000}], {})
    assert "'401 token_revoked'" in seen[0] and "FALSE" in seen[0]


def test_code_never_flips_is_active():
    src = (SCRIPTS / "codex_usage.py").read_text(encoding="utf-8")
    assert "SET is_active" not in src and "is_active=" not in src.replace("is_active = ", "")


# ── (e) 러너: codex_pick_account_home ────────────────────────────────

def _function(name: str) -> str:
    start = RUNNER.index(f"{name}() {{")
    return RUNNER[start: RUNNER.index("\n}\n", start) + 3]


@pytest.fixture(scope="module")
def shell_fns(tmp_path_factory):
    if shutil.which("bash") is None or shutil.which("python3") is None:
        pytest.skip("bash/python3 미설치 환경")
    path = tmp_path_factory.mktemp("fn") / "fn.sh"
    path.write_text(
        "log() { echo \"LOG $*\" >&2; }\n"
        + "\n".join(_function(n) for n in (
            "codex_pick_account_home", "codex_revoked_marker_path",
            "codex_failure_is_token_revoked", "mark_codex_account_revoked")),
        encoding="utf-8")
    return path


def _state(tmp_path: Path, accounts: list[dict]) -> Path:
    for a in accounts:
        (tmp_path / a["key_name"]).mkdir(exist_ok=True)
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"accounts": accounts}))
    return state


def _acct_row(name, priority, **kw):
    row = {"key_name": name, "priority": priority, "is_active": True, "has_auth": True,
           "rate_limited_until_epoch": None}
    row.update(kw)
    return row


def _sh(shell_fns, body, tmp_path, state=None, extra_env=None):
    env = {"PATH": "/usr/bin:/bin:/usr/local/bin",
           "AADS_CODEX_REVOKED_MARKER_PREFIX": str(tmp_path / "revoked-")}
    if state:
        env["AADS_CODEX_ACCOUNTS_STATE"] = str(state)
    env.update(extra_env or {})
    return subprocess.run(["bash", "-c", f"source {shell_fns}; {body}"],
                          capture_output=True, text=True, env=env, timeout=30)


def test_e_pick_skips_auth_unusable_and_returns_next_priority(shell_fns, tmp_path):
    state = _state(tmp_path, [
        _acct_row("CODEX_OAUTH_JINAH", 1, auth_usable=False),
        _acct_row("CODEX_OAUTH_MAIN", 2, auth_usable=True),
    ])
    out = _sh(shell_fns, "codex_pick_account_home", tmp_path, state)
    assert out.returncode == 0
    assert out.stdout.strip() == str(tmp_path / "CODEX_OAUTH_MAIN")


def test_e_pick_without_field_keeps_old_behavior(shell_fns, tmp_path):
    state = _state(tmp_path, [
        _acct_row("CODEX_OAUTH_JINAH", 1),
        _acct_row("CODEX_OAUTH_MAIN", 2),
    ])
    out = _sh(shell_fns, "codex_pick_account_home", tmp_path, state)
    assert out.stdout.strip() == str(tmp_path / "CODEX_OAUTH_JINAH")


def test_e_pick_fails_when_every_account_is_unusable(shell_fns, tmp_path):
    state = _state(tmp_path, [_acct_row("CODEX_OAUTH_MAIN", 1, auth_usable=False)])
    assert _sh(shell_fns, "codex_pick_account_home", tmp_path, state).returncode == 1


def test_e_pick_keeps_rate_limit_cooldown(shell_fns, tmp_path):
    state = _state(tmp_path, [
        _acct_row("CODEX_OAUTH_JINAH", 1, rate_limited_until_epoch=int(time.time()) + 3600),
        _acct_row("CODEX_OAUTH_MAIN", 2),
    ])
    out = _sh(shell_fns, "codex_pick_account_home", tmp_path, state)
    assert out.stdout.strip() == str(tmp_path / "CODEX_OAUTH_MAIN")


def test_e_per_account_revoked_marker_excludes_only_that_account(shell_fns, tmp_path):
    state = _state(tmp_path, [
        _acct_row("CODEX_OAUTH_JINAH", 1),
        _acct_row("CODEX_OAUTH_MAIN", 2),
    ])
    out = _sh(shell_fns, "mark_codex_account_revoked CODEX_OAUTH_JINAH x; codex_pick_account_home",
              tmp_path, state)
    assert out.stdout.strip() == str(tmp_path / "CODEX_OAUTH_MAIN")
    marker = tmp_path / "revoked-CODEX_OAUTH_JINAH"
    assert marker.exists() and int(marker.read_text()) > time.time()
    assert not (tmp_path / "revoked-CODEX_OAUTH_MAIN").exists()  # 전역 마커 아님


def test_e_expired_revoked_marker_is_ignored_and_removed(shell_fns, tmp_path):
    state = _state(tmp_path, [_acct_row("CODEX_OAUTH_JINAH", 1), _acct_row("CODEX_OAUTH_MAIN", 2)])
    marker = tmp_path / "revoked-CODEX_OAUTH_JINAH"
    marker.write_text(str(int(time.time()) - 5))
    out = _sh(shell_fns, "codex_pick_account_home", tmp_path, state)
    assert out.stdout.strip() == str(tmp_path / "CODEX_OAUTH_JINAH")
    assert not marker.exists()


def test_revoked_marker_ttl_follows_cooldown_convention(shell_fns, tmp_path):
    before = int(time.time())
    _sh(shell_fns, "mark_codex_account_revoked K x", tmp_path)
    assert 7000 <= int((tmp_path / "revoked-K").read_text()) - before <= 7210
    _sh(shell_fns, "mark_codex_account_revoked K2 x", tmp_path, extra_env={"AADS_CODEX_AUTH_DISABLED_TTL": "60"})
    assert 55 <= int((tmp_path / "revoked-K2").read_text()) - before <= 70


def test_marker_name_is_sanitized(shell_fns, tmp_path):
    out = _sh(shell_fns, "codex_revoked_marker_path 'A/../B C'", tmp_path)
    assert out.stdout == str(tmp_path / "revoked-A_.._B_C")


def test_failure_detector_needs_401_and_signal(shell_fns, tmp_path):
    def run(err, out=""):
        e, o = tmp_path / "e.err", tmp_path / "o.out"
        e.write_text(err)
        o.write_text(out)
        return _sh(shell_fns, f"codex_failure_is_token_revoked {e} {o}", tmp_path).returncode

    assert run(REVOKED_BODY) == 0
    assert run("", REVOKED_BODY) == 0  # 짧은 stdout 에 에러 본문이 온 경우
    assert run("ERROR: unauthorized (401)") == 1
    assert run('{"code":"token_revoked"}') == 1
    assert run("", "x" * 5000 + REVOKED_BODY) == 1  # 긴 stdout 은 모델 본문으로 보고 무시


def test_runner_wires_revoked_detection_into_codex_failure_path():
    assert "codex_failure_is_token_revoked \"$err_file\" \"$output_file\"" in RUNNER
    assert "mark_codex_account_revoked \"$_revoked_key\"" in RUNNER
    assert 'acct.get("auth_usable") is False' in RUNNER


def test_runner_and_local_copy_stay_identical():
    assert (SCRIPTS / "pipeline-runner.sh").read_text() == (SCRIPTS / "pipeline-runner.sh.local").read_text()
