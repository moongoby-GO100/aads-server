"""도구 출력 비밀값 가림 — 가짜 값만 사용한다."""
import asyncio
import json
import re
from unittest.mock import patch

import pytest

from app.core.secret_redact import (
    redact_text,
    redact_text_counted,
    redact_value,
    redact_value_counted,
)
from app.services import tool_executor as te
from app.services.tool_executor import ToolExecutor

FAKE_PW = "pw0123456789fake"


def _run_execute(result, monkeypatch=None):
    async def fake(self, name, inp):
        return result

    with patch.object(ToolExecutor, "_dispatch", fake):
        return asyncio.run(ToolExecutor().execute("run_remote_command", {"command": "docker inspect x"}))


# ── 기본 동작 ──
def test_url_password_masked_user_host_kept():
    out = redact_text(f"postgresql://acct_x:{FAKE_PW}@127.0.0.1:5432/acct")
    assert out == "postgresql://acct_x:***@127.0.0.1:5432/acct"
    assert redact_text("redis://:fakepass@h:6379/0") == "redis://:***@h:6379/0"


def test_docker_inspect_env_lines():
    text = f"[ACCT_DATABASE_URL=postgresql://u:{FAKE_PW}@db:5432/x OPENAI_API_KEY=fakevalue123 APP_PORT=8115]"
    out, n = redact_text_counted(text)
    assert FAKE_PW not in out and "fakevalue123" not in out
    assert "APP_PORT=8115" in out and "@db:5432/x" in out and n == 2


def test_known_prefixes_and_mysql_arg():
    for tok in ("ghp_" + "A" * 24, "AKIA" + "B" * 16, "xoxb-" + "1" * 12, "github_pat_" + "C" * 24,
                "sk-ant-" + "D" * 12, "sk-" + "E" * 24, "AIza" + "F" * 24):
        assert tok not in redact_text(f"x {tok} y")
    assert "hunter2" not in redact_text("mysql -u root -phunter2 db")
    assert "hunter2" not in redact_text("PGPASSWORD=hunter2 psql -h x")


def test_no_overmasking():
    samples = [
        "commit " + "a" * 40,
        "id 123e4567-e89b-12d3-a456-426614174000",
        "image sha256:" + "b" * 64,
        "https://example.com/path?x=1",
        "https://example.com:8443/x",
        "password=********",
        "DB_PASSWORD=***",
        "PASSWORD=[REDACTED]",
        "postgresql://u:***@h/db",
        "max_tokens=100 APP_PORT=8115",
        "APP_PORT=8115",
        "task-AADS-TOOL-OUTPUT-SECRET-REDACT-runner-e82c7a44",
    ]
    for s in samples:
        assert redact_text_counted(s) == (s, 0), s
    obj = {"max_tokens": 100, "usage": {"input_tokens": 5}, "port": 8115, "has_token": True}
    assert redact_value_counted(obj) == (obj, 0)


# ── 이전 리뷰 지적 1차 (1~4) ──
def test_review1_1_keyword_contained_env_name():  # DB_PASSWORD_FILE_CONTENT=fake
    out, n = redact_text_counted("DB_PASSWORD_FILE_CONTENT=fakepass")
    assert "fakepass" not in out and n == 1


def test_review1_2_keyword_contained_json_key():  # {"TOKEN_VALUE":"fake"}
    s = '{"TOKEN_VALUE":"fakepass"}'
    out, n = redact_text_counted(s)
    assert "fakepass" not in out and n == 1 and json.loads(out)["TOKEN_VALUE"] == "***"
    out2, n2 = redact_value_counted(json.loads(s))
    assert out2 == {"TOKEN_VALUE": "***"} and n2 == 1


def test_review1_3_double_quoted_value():  # PASSWORD="fake"
    out, n = redact_text_counted('PASSWORD="fakepass"')
    assert out == 'PASSWORD="***"' and n == 1


def test_review1_4_single_quoted_value():  # PASSWORD='fake'
    out, n = redact_text_counted("PASSWORD='fakepass'")
    assert out == "PASSWORD='***'" and n == 1


# ── 2차 (5~7) ──
def test_review2_5_unquoted_value_with_comma_tail():  # PASSWORD=fake,tail
    out, n = redact_text_counted("PASSWORD=fakepass,tail")
    assert "fakepass" not in out and "tail" not in out and n == 1


def test_review2_6_json_escaped_quote_in_value():
    for s in ('{"TOKEN_VALUE": "fake\\"pass"}',):
        out, n = redact_text_counted(s)
        assert "fake" not in out and "pass" not in out and n == 1
        assert json.loads(out)["TOKEN_VALUE"] == "***"
    nested = json.dumps(json.dumps({"DB_PASSWORD_X": 'fake"pass'}))
    out = redact_text(nested)
    assert "fake" not in out and "pass" not in out
    assert json.loads(json.loads(out))["DB_PASSWORD_X"] == "***"


def test_review2_7_partially_masked_value():  # fake***plain
    for s in ("PASSWORD=fake***plain", "postgresql://u:fake***plain@db/x", '{"PASSWORD": "fake***plain"}'):
        out, n = redact_text_counted(s)
        assert "fake" not in out and "plain" not in out and n == 1, s
    assert redact_value({"PASSWORD": "fake***plain"}) == {"PASSWORD": "***"}


# ── 직렬화 후 파싱 ──
def test_json_dumps_stdout_still_parses_and_hides():
    raw = json.dumps({"stdout": 'PASSWORD="fake"\nAPP_PORT=8115'})
    out = _run_execute(raw)
    assert "fake" not in out
    body = out.split("\n[보안]")[0]
    assert json.loads(body)["stdout"].endswith("APP_PORT=8115")
    assert out.rstrip().endswith("[보안] 비밀값 1건을 가렸습니다")


def test_json_dumps_dict_result_still_parses_and_hides():
    res = {"stdout": 'x PASSWORD="fake"', "env": {"MY_API_KEY": "fake", "nested": [{"db_dsn": "fake"}]}, "rc": 0}
    out = _run_execute(res)
    assert "fake" not in out
    d = json.loads(out.split("\n[보안]")[0])
    assert d["env"]["MY_API_KEY"] == "***" and d["env"]["nested"][0]["db_dsn"] == "***" and d["rc"] == 0
    assert "[보안] 비밀값 3건" in out


def test_redact_value_does_not_mutate_original():
    orig = {"PASSWORD": "fake", "a": [{"token": "fake"}]}
    redact_value(orig)
    assert orig == {"PASSWORD": "fake", "a": [{"token": "fake"}]}


# ── 자르기 경계 ──
@pytest.mark.parametrize("secret_chunk", ['{"PASSWORD": "fakeXYZ', "postgresql://u:fakeXYZ"])
def test_truncation_boundary_never_leaks(monkeypatch, secret_chunk):
    full_tail = {
        '{"PASSWORD": "fakeXYZ': '{"PASSWORD": "fakeXYZ-rest"}',
        "postgresql://u:fakeXYZ": "postgresql://u:fakeXYZ-rest@h/db",
    }[secret_chunk]
    for cut in range(len(secret_chunk) - 3, len(secret_chunk) + 1):
        pad = "x" * 40
        text = pad + " " + full_tail
        monkeypatch.setattr(te, "_MAX_RESULT_CHARS", len(pad) + 1 + cut)
        out = _run_execute(text)
        assert "fake" not in out and "XYZ" not in out, (cut, out)


def test_tool_truncated_tail_unclosed_values_covered():
    # 도구 자체가 값 중간에서 자른 출력 — 닫히지 않은 값을 끝까지 덮는다
    for s in ('{"PASSWORD": "fakeabc', "postgresql://u:fakeabc", "PASSWORD=fakeabc", 'x "TOKEN": "fake\\'):
        out = redact_text(s)
        assert "fake" not in out, s


# ── 기존 형식 보존(parity) ──
_OLD_SECRET_PATTERNS = (
    (re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"), "sk-ant-***"),
    (re.compile(r"sk-[A-Za-z0-9]{20,}"), "sk-***"),
    (re.compile(r"AIza[A-Za-z0-9_\-]{20,}"), "AIza***"),
    (re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"), "gh_***"),
    (re.compile(r"xox[abprs]-[A-Za-z0-9\-]{10,}"), "xox-***"),
    (re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{5,}"), "jwt.***"),
    (re.compile(r"postgres(?:ql)?://[^\s:/]+:[^\s@]+@"), "postgres://***:***@"),
    (re.compile(r"(?i)\b(api[_-]?key|auth[_-]?token|secret|password|passwd)\b\s*[:=]\s*\S+"), r"\1=***"),
)

_PARITY_SAMPLES = [
    ("password: fakepass", "fakepass"),
    ("api-key=fakepass", "fakepass"),
    ("api_key = fakepass", "fakepass"),
    ("auth-token: fakepass", "fakepass"),
    ("secret=fakepass", "fakepass"),
    ("passwd: fakepass extra", "fakepass"),
    ("PASSWORD=fakepass", "fakepass"),
    ("postgres://user:fakepass@host:5432/db", "fakepass"),
    ("postgresql://user:fakepass@host/db", "fakepass"),
    ("key sk-ant-" + "A" * 20, "A" * 20),
    ("key sk-" + "B" * 24, "B" * 24),
    ("key AIza" + "C" * 24, "C" * 24),
    ("tok ghp_" + "D" * 24, "D" * 24),
    ("tok xoxb-" + "1" * 12, "1" * 12),
    ("jwt eyJ" + "a" * 12 + "." + "b" * 12 + "." + "c" * 8, "b" * 12),
]


@pytest.mark.parametrize("sample,secret", _PARITY_SAMPLES)
def test_parity_new_mask_secrets_covers_old_patterns(sample, secret):
    from app.services.llmops_store import mask_secrets

    old = sample
    for pat, rep in _OLD_SECRET_PATTERNS:
        old = pat.sub(rep, old)
    assert secret not in old  # 옛 패턴이 가리던 입력임을 확인
    assert secret not in mask_secrets(sample), sample


def test_llmops_mask_secrets_is_thin_wrapper():
    from app.services import llmops_store

    s = f"postgresql://acct_x:{FAKE_PW}@127.0.0.1:5432/acct"
    assert llmops_store.mask_secrets(s) == redact_text(s)
    assert not hasattr(llmops_store, "_SECRET_PATTERNS")
    assert llmops_store.clip("password: fakepass") == "password: ***"


# ── ToolExecutor.execute 경유 ──
def test_execute_redacts_result_and_adds_notice():
    raw = f"ACCT_DATABASE_URL=postgresql://u:{FAKE_PW}@h:5432/d\nAPP_PORT=1"
    out = _run_execute(raw)
    assert FAKE_PW not in out and "APP_PORT=1" in out
    assert out.rstrip().endswith("[보안] 비밀값 1건을 가렸습니다")


def test_execute_clean_result_unchanged():
    assert _run_execute("ok") == "ok"


def test_execute_redaction_failure_blocks_output():
    with patch("app.core.secret_redact.redact_text_counted", side_effect=RuntimeError("x")):
        out = _run_execute(f"PASSWORD={FAKE_PW}")
    assert FAKE_PW not in out and out.startswith("[ERROR]")


# ── 이전 리뷰 지적 3차 (runner-e82c7a44 반려 → CTO 직접 수정) ──
def test_review3_1_keyword_name_with_file_or_path_suffix_masked():
    # 이름만 보고 통과시키지 않는다: SECRET_FILE·API_KEY_PATH·TOKEN_TYPE 의 문자열 값도 가린다
    for s in ("SECRET_FILE=fake", "API_KEY_PATH=fake", '{"SECRET_FILE": "fake"}', "TOKEN_TYPE=fake",
              "TOKEN_COUNT=fake"):
        out, n = redact_text_counted(s)
        assert "fake" not in out and n == 1, s
    out, n = redact_value_counted({
        "SECRET_FILE": "fake", "api_key_path": "fake", "password": 1234,
        "token_count": 5, "tokens_used": 9, "token_usage": {"input": 1}, "max_tokens": "100",
    })
    assert out == {
        "SECRET_FILE": "***", "api_key_path": "***", "password": "***",
        "token_count": 5, "tokens_used": 9, "token_usage": {"input": 1}, "max_tokens": "100",
    }
    assert n == 3
    # 수량·통계 이름 + 숫자 값만 남긴다
    for s in ("TOKEN_COUNT=5", "max_tokens=100, temperature=0", '{"input_tokens": 5}'):
        assert redact_text_counted(s) == (s, 0), s


def test_review3_2_env_value_escaped_quote_inside_serialized_json():
    # 리뷰 재현: PASSWORD="fake\"plain" 을 stdout 에 넣어 json.dumps → plain 이 남던 문제
    raw = json.dumps({"stdout": 'PASSWORD="fake\\"plain" APP_PORT=8115'})
    out, n = redact_text_counted(raw)
    assert "fake" not in out and "plain" not in out and n == 1
    assert json.loads(out)["stdout"] == 'PASSWORD="***" APP_PORT=8115'
    # 값이 백슬래시로 끝나도 닫는 따옴표를 넘어 뒤 내용을 먹지 않는다
    raw = json.dumps({"stdout": 'PASSWORD="fake\\\\" APP_PORT=8115'})
    out = redact_text(raw)
    assert "fake" not in out and json.loads(out)["stdout"] == 'PASSWORD="***" APP_PORT=8115'
    # 도구가 값 중간에서 잘라 닫히지 않은 경우
    out = redact_text(json.dumps({"stdout": 'PASSWORD="fakeabc'}))
    assert "fake" not in out and json.loads(out)["stdout"] == 'PASSWORD="***"'
    # 두 번 직렬화된 JSON — 값이 백슬래시로 끝나도 구조가 깨지지 않는다
    nested = json.dumps(json.dumps({"PASSWORD": "fake\\", "user": "bob"}))
    out = redact_text(nested)
    assert "fake" not in out
    assert json.loads(json.loads(out)) == {"PASSWORD": "***", "user": "bob"}
    # 끝이 백슬래시 여러 개로 잘린 경우
    for tail in ('\\"PASSWORD\\": \\"fake\\\\\\', 'PASSWORD=\\"fake\\\\\\'):
        assert "fake" not in redact_text("x " + tail), tail
    # ToolExecutor.execute 경유
    out = _run_execute(json.dumps({"stdout": 'PASSWORD="fake\\"plain"'}))
    assert "fake" not in out and "plain" not in out
    assert json.loads(out.split("\n[보안]")[0])["stdout"] == 'PASSWORD="***"'


def test_review3_3_url_without_password_unchanged():
    for s in (
        "https://example.com:8443?x=1",
        "https://example.com:8443#frag",
        "https://[::1]:8443",
        "https://[::1]:8443/x?y=1",
        "http://localhost:8000",
        "see https://example.com:8443?x=1",
        "redis://h:6379",
    ):
        assert redact_text_counted(s) == (s, 0), s
    # 잘린 꼬리의 비밀번호는 계속 가린다
    for s, secret in (("postgresql://u:fakeabc", "fakeabc"), ("redis://:fakeabc", "fakeabc"),
                      ("postgresql://u:fa?ke", "fa?ke")):
        assert secret not in redact_text(s), s


def test_review3_no_regex_blowup_on_large_input():
    import time

    chunks = [
        '\\"PASSWORD\\": \\"' + "a\\\\" * 40000,
        'PASSWORD=\\"' + "b\\\\\\\"" * 30000,
        "postgresql://u:" + "c" * 100000,
        "TOKEN_X=" + "d" * 100000 + "]",
    ]
    started = time.monotonic()
    for c in chunks:
        out = redact_text(c)
        assert len(out) < len(c)
    assert time.monotonic() - started < 5.0


# -- 병렬 재작업 runner-f31f9b00 반려 지적 3건 (d6c92a56 기준 재현, URL 작은따옴표 1건 수정) --
Q4, DQ4, BS4 = chr(39), chr(34), chr(92)


def test_review4_f31f9b00_findings():
    # 1) 수량형 이름이라도 숫자가 아니면 가린다
    for s in ("TOKEN_COUNT=fake", "SECRET_TYPE=fake"):
        assert redact_text_counted(s) == (s.split("=")[0] + "=***", 1), s
    assert redact_value({"TOKEN_COUNT": "fake", "SECRET_TYPE": "fake"}) == {"TOKEN_COUNT": "***", "SECRET_TYPE": "***"}
    # 2) 직렬화된 값이 백슬래시 두 개로 끝남 (닫는 따옴표 앞 백슬래시 다섯 개)
    raw = json.dumps({"stdout": "PASSWORD=" + DQ4 + "fake" + BS4 * 2 + DQ4 + " APP_PORT=8115"})
    out = redact_text(raw)
    assert "fake" not in out and json.loads(out)["stdout"] == "PASSWORD=" + DQ4 + "***" + DQ4 + " APP_PORT=8115"
    # 3) URL 비밀번호 안의 작은따옴표
    assert redact_text("postgresql://u:fake" + Q4 + "plain@host/db") == "postgresql://u:***@host/db"
    assert "plain" not in redact_text("x postgresql://u:fake" + Q4 + "plain")
    # 파이썬 repr 안의 URL 은 따옴표 밖을 먹지 않는다
    rep = repr({"a": "postgresql://u:pw0@h/db", "b": "x"})
    assert redact_text(rep) == rep.replace("pw0", "***")
