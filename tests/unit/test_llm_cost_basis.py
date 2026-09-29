"""AADS-LLM-M9-COST-BASIS-20260930 회귀시험.

- surface 판정 5경로
- cost_source 분리: 실보고(relay_reported)와 정가 추산(cost_usd_catalog)이 섞이지 않음
- 성공 작업당 비용: 분모 0 / 분모 미정의 / 표본 부족 (+ 정상)
- job_id 귀속: 앱 기록 경로·러너 기록 경로 모두 job_id 를 남기고, 집계가 그것으로 분모를 센다
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

from app.services import llm_cost_basis as cb
from app.services import oauth_usage_tracker as tracker

ROOT = Path(__file__).resolve().parents[2]


def _load_runner_usage():
    path = ROOT / "scripts" / "runner_cli_usage.py"
    spec = importlib.util.spec_from_file_location("runner_cli_usage", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


runner_usage = _load_runner_usage()


# ── surface 판정 5경로 ────────────────────────────────────────────────

def test_surface_chat_when_session_is_chat_session():
    assert cb.classify_surface("cli_relay", None, session_is_chat=True) == "chat"
    assert cb.classify_surface("model_selector_sdk", "", session_is_chat=True) == "chat"


def test_surface_runner_from_job_id_or_runner_call_source():
    # 실행 컨텍스트가 채팅 세션보다 우선한다.
    assert cb.classify_surface("cli_relay", "runner-abc123", session_is_chat=True) == "runner"
    assert cb.classify_surface("runner_claude_cli", None) == "runner"
    assert cb.classify_surface("runner_codex_cli", "") == "runner"


def test_surface_terminal_cli_for_non_chat_relay():
    assert cb.classify_surface("cli_relay", None, session_is_chat=False) == "terminal_cli"
    assert cb.classify_surface("codex_relay", "", session_is_chat=False) == "terminal_cli"


def test_surface_service_for_in_app_clients():
    for src in ("anthropic_client", "anthropic_client_msg", "model_selector_sdk", "ceo_chat"):
        assert cb.classify_surface(src, None, session_is_chat=False) == "service"


def test_surface_unknown_is_not_guessed():
    assert cb.classify_surface("", None) == "unknown"
    assert cb.classify_surface("something_new", None) == "unknown"


# ── cost_source 분리 ─────────────────────────────────────────────────

def test_resolve_cost_fields_keeps_reported_and_marks_unmeasured_null():
    assert cb.resolve_cost_fields(12.5, "relay_reported") == (12.5, "relay_reported")
    assert cb.resolve_cost_fields(0.3, "catalog_estimated") == (0.3, "catalog_estimated")
    # 출처 없는 0 은 미측정 → NULL
    assert cb.resolve_cost_fields(0.0, None) == (None, "unknown")
    assert cb.resolve_cost_fields(None, None) == (None, "unknown")
    # 보고라고 했는데 값이 없으면 unknown
    assert cb.resolve_cost_fields(None, "relay_reported") == (None, "unknown")
    # 모르는 출처 문자열은 받아들이지 않는다
    assert cb.resolve_cost_fields(1.0, "guess") == (1.0, "unknown")


def _capture_log_usage(monkeypatch, **kwargs):
    captured = []

    async def fake_enqueue(entry):
        captured.append(entry)

    monkeypatch.setattr(tracker, "_enqueue_usage", fake_enqueue)
    monkeypatch.setattr(tracker, "_ensure_flush_task", lambda: None)

    async def run():
        tracker.log_usage(token="", account_slot="1", **kwargs)
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    asyncio.run(run())
    assert len(captured) == 1
    return captured[0]


def test_log_usage_records_cost_source_and_never_fills_catalog(monkeypatch):
    entry = _capture_log_usage(
        monkeypatch,
        model="claude-opus-5",
        input_tokens=100,
        output_tokens=200,
        cost_usd=9.99,
        cost_source="relay_reported",
        call_source="cli_relay",
    )
    assert entry["cost_usd"] == 9.99
    assert entry["cost_source"] == "relay_reported"
    # 정가 재계산값은 앱이 만들지 않는다 — entry 에 칸 자체가 없다.
    assert "cost_usd_catalog" not in entry


def test_log_usage_default_is_unmeasured_not_zero(monkeypatch):
    entry = _capture_log_usage(
        monkeypatch, model="claude-haiku-4-5-20251001", call_source="anthropic_client"
    )
    assert entry["cost_usd"] is None
    assert entry["cost_source"] == "unknown"


def test_insert_catalog_column_comes_from_db_function_not_reported_value():
    entry = {
        "account_slot": "1", "token_prefix": "", "model": "claude-opus-5",
        "input_tokens": 10, "output_tokens": 20,
        "cache_creation_tokens": 0, "cache_read_tokens": 0,
        "cost_usd": 7.77, "cost_source": "relay_reported", "rl": {},
        "call_source": "cli_relay", "session_id": "s", "error_code": None,
        "duration_ms": 0, "tenant_id": None, "job_id": None,
    }
    sql, args = tracker._build_usage_insert([entry], tracker._USAGE_LOG_COLUMNS, with_catalog=True)
    cols = [c.strip() for c in sql.split("(", 1)[1].split(")", 1)[0].split(",")]
    assert cols[-1] == "cost_usd_catalog"
    assert "llm_catalog_cost_usd(" in sql
    # 인자에는 실보고 값이 cost_usd 자리에 한 번만 들어가고, catalog 자리는 인자가 아니다.
    assert args.count(7.77) == 1
    assert args[tracker._USAGE_LOG_COLUMNS.index("cost_usd")] == 7.77
    assert args[tracker._USAGE_LOG_COLUMNS.index("cost_source")] == "relay_reported"
    assert len(args) == len(tracker._USAGE_LOG_COLUMNS)


def test_insert_legacy_schema_fallback_drops_new_columns():
    entry = {
        "account_slot": "1", "token_prefix": "", "model": "m",
        "input_tokens": 1, "output_tokens": 1,
        "cache_creation_tokens": 0, "cache_read_tokens": 0,
        "cost_usd": None, "cost_source": "unknown", "rl": {},
        "call_source": "x", "session_id": "", "error_code": None,
        "duration_ms": 0, "tenant_id": None, "job_id": "runner-1",
    }
    sql, args = tracker._build_usage_insert(
        [entry], tracker._USAGE_LOG_LEGACY_COLUMNS, with_catalog=False
    )
    assert "cost_source" not in sql and "job_id" not in sql and "cost_usd_catalog" not in sql
    assert len(args) == len(tracker._USAGE_LOG_LEGACY_COLUMNS)


def test_summary_never_adds_reported_and_estimated_into_one_total():
    rows = [
        {"call_source": "cli_relay", "model": "claude-opus-5", "cost_source": "relay_reported",
         "calls": 3, "cost_usd": 30.0, "cost_usd_catalog": 5.0, "catalog_calls": 3,
         "both_cost_usd": 30.0, "both_catalog_usd": 5.0},
        {"call_source": "cli_relay", "model": "claude-opus-5", "cost_source": "catalog_estimated",
         "calls": 1, "cost_usd": 2.0, "cost_usd_catalog": None, "catalog_calls": 0},
    ]
    items = cb.summarize_usage_rows(rows)
    by_src = {i["cost_source"]: i for i in items}
    assert set(by_src) == {"relay_reported", "catalog_estimated"}
    assert by_src["relay_reported"]["total_cost_usd"] == 30.0
    assert by_src["relay_reported"]["catalog_cost_usd"] == 5.0
    assert by_src["relay_reported"]["reported_to_catalog_ratio"] == 6.0
    assert by_src["catalog_estimated"]["total_cost_usd"] == 2.0
    assert by_src["catalog_estimated"]["catalog_cost_usd"] is None
    surf = cb.summarize_by_surface(items)
    assert len(surf) == 2  # 모델은 접어도 비용출처는 접지 않는다


# ── 성공 작업당 비용 ─────────────────────────────────────────────────

def test_cost_per_success_denominator_zero():
    r = cb.cost_per_success(10.0, 0, denominator_defined=True)
    assert r == {"value": None, "status": "denominator_zero"}


def test_cost_per_success_denominator_undefined():
    r = cb.cost_per_success(10.0, None, denominator_defined=False)
    assert r == {"value": None, "status": "denominator_undefined"}


def test_cost_per_success_insufficient_sample():
    r = cb.cost_per_success(10.0, 4, denominator_defined=True)
    assert r == {"value": None, "status": "insufficient_sample"}


def test_cost_per_success_ok():
    assert cb.cost_per_success(10.0, 5, denominator_defined=True) == {"value": 2.0, "status": "ok"}


def test_chat_surface_has_no_invented_denominator():
    rows = [{"call_source": "cli_relay", "model": "claude-opus-5", "cost_source": "relay_reported",
             "session_is_chat": True, "calls": 50, "cost_usd": 100.0}]
    item = cb.summarize_usage_rows(rows)[0]
    assert item["surface"] == "chat"
    assert item["success_jobs"] is None
    assert item["cost_per_success_usd"] is None
    assert item["cost_per_success_status"] == "denominator_undefined"
    assert item["denominator"] == "undefined"


# ── job_id 귀속 ──────────────────────────────────────────────────────

def test_log_usage_carries_job_id(monkeypatch):
    entry = _capture_log_usage(
        monkeypatch, model="claude-opus-5", cost_usd=1.0, cost_source="relay_reported",
        call_source="cli_relay", session_id="chat-uuid", job_id="runner-deadbeef",
    )
    assert entry["job_id"] == "runner-deadbeef"
    assert entry["session_id"] == "chat-uuid"  # 기존 session_id 는 그대로
    values = tracker._usage_log_values(entry)
    assert values[tracker._USAGE_LOG_COLUMNS.index("job_id")] == "runner-deadbeef"


def test_runner_success_denominator_counts_done_jobs_only():
    rows = []
    for i in range(6):
        rows.append({"call_source": "runner_claude_cli", "model": "claude-opus-5",
                     "cost_source": "relay_reported", "job_id": f"runner-{i}",
                     "job_status": "done", "calls": 1, "cost_usd": 3.0})
    # 같은 작업이 두 번 시도돼도 성공 작업은 하나다.
    rows.append({"call_source": "runner_claude_cli", "model": "claude-opus-5",
                 "cost_source": "relay_reported", "job_id": "runner-0",
                 "job_status": "done", "calls": 1, "cost_usd": 3.0})
    rows.append({"call_source": "runner_claude_cli", "model": "claude-opus-5",
                 "cost_source": "relay_reported", "job_id": "runner-rej",
                 "job_status": "rejected_done", "calls": 1, "cost_usd": 6.0})
    rows.append({"call_source": "runner_claude_cli", "model": "claude-opus-5",
                 "cost_source": "relay_reported", "job_id": "runner-err",
                 "job_status": "error", "calls": 1, "cost_usd": 6.0})
    item = cb.summarize_usage_rows(rows)[0]
    assert item["surface"] == "runner"
    assert item["jobs"] == 8
    assert item["success_jobs"] == 6
    assert item["total_cost_usd"] == 33.0
    # 실패·반려 작업 비용도 성공 작업이 떠안는다.
    assert item["cost_per_success_usd"] == 5.5
    assert item["cost_per_success_status"] == "ok"


def test_runner_sql_joins_pipeline_jobs_by_job_id():
    sql = cb.COST_PER_SUCCESS_SQL
    assert "LEFT JOIN pipeline_jobs pj ON pj.job_id = o.job_id" in sql
    assert "LEFT JOIN chat_sessions cs ON cs.id::text = o.session_id" in sql


def _claude_payload():
    return {
        "type": "result", "subtype": "success", "is_error": False,
        "result": "RESULT: 변경 파일 2개", "session_id": "cli-sess-1",
        "total_cost_usd": 4.5,
        "usage": {"input_tokens": 1, "output_tokens": 2},
        "modelUsage": {
            "claude-opus-5[1m]": {"inputTokens": 10, "outputTokens": 1000,
                                  "cacheReadInputTokens": 50000,
                                  "cacheCreationInputTokens": 700, "costUSD": 4.0},
            "claude-haiku-4-5-20251001": {"inputTokens": 5, "outputTokens": 50, "costUSD": 0.5},
        },
    }


def test_runner_claude_json_restores_text_and_attributes_job(tmp_path):
    out = tmp_path / "runner-abc.out"
    out.write_text(json.dumps(_claude_payload()), encoding="utf-8")
    sql = runner_usage.process(
        "claude_cli", str(out), job_id="runner-abc", account_slot="2",
        model="claude-opus-5", exit_code=0, duration_ms=1234,
    )
    # 하류 판정은 예전 text 모드와 같은 결과 텍스트를 본다.
    assert out.read_text(encoding="utf-8") == "RESULT: 변경 파일 2개\n"
    assert json.loads((tmp_path / "runner-abc.out.usage.json").read_text())["total_cost_usd"] == 4.5
    assert sql.startswith("INSERT INTO oauth_usage_log")
    assert sql.count("'runner-abc'") >= 2  # job_id 칸 + tenant 조회
    assert "'runner_claude_cli'" in sql
    assert "'claude-opus-5'" in sql and "[1m]" not in sql
    assert "4.0, 'relay_reported'" in sql
    assert "llm_catalog_cost_usd('claude-opus-5', 10, 1000)" in sql
    assert "'cli-sess-1'" in sql
    assert "FROM pipeline_jobs WHERE job_id = 'runner-abc'" in sql


def test_runner_non_json_output_is_untouched_and_unmeasured(tmp_path):
    out = tmp_path / "runner-x.out"
    out.write_text("You've hit your limit\n", encoding="utf-8")
    sql = runner_usage.process(
        "claude_cli", str(out), job_id="runner-x", account_slot="1",
        model="claude-opus-5", exit_code=1, duration_ms=0,
    )
    assert out.read_text(encoding="utf-8") == "You've hit your limit\n"
    assert "NULL, 'unknown'" in sql
    assert "'exit_1'" in sql


def test_runner_codex_row_is_attributed_without_guessing_cost(tmp_path):
    out = tmp_path / "runner-c.out"
    out.write_text("done", encoding="utf-8")
    sql = runner_usage.process(
        "codex_cli", str(out), job_id="runner-c", account_slot="codex",
        model="gpt-6-astra", exit_code=0, duration_ms=10,
    )
    assert out.read_text(encoding="utf-8") == "done"
    assert "'runner_codex_cli'" in sql and "'runner-c'" in sql
    assert "NULL, 'unknown'" in sql


def test_runner_sql_escapes_quotes():
    sql = runner_usage.build_insert_sql(
        [runner_usage.unmeasured_row("m'x")], job_id="j'1", call_source="runner_claude_cli",
        account_slot="1",
    )
    assert "'m''x'" in sql and "'j''1'" in sql


# ── 마이그레이션·배선 ────────────────────────────────────────────────

def test_migration_up_down_pair_exists():
    up = (ROOT / "migrations" / "20260930_oauth_usage_cost_basis.sql").read_text(encoding="utf-8")
    down = (ROOT / "migrations" / "rollback" / "20260930_oauth_usage_cost_basis.down.sql").read_text(
        encoding="utf-8"
    )
    for col in ("cost_source", "cost_usd_catalog", "job_id"):
        assert f"ADD COLUMN IF NOT EXISTS {col}" in up
        assert f"DROP COLUMN IF EXISTS {col}" in down
    assert "'relay_reported', 'catalog_estimated', 'unknown'" in up
    assert "FUNCTION public.llm_catalog_cost_usd" in up
    # 과거 행에 정가 추산을 채우지 않는다.
    assert "SET cost_usd_catalog" not in up
    # 자동 적용 게이트(파괴적 문장 차단)에 걸리지 않아야 한다.
    for bad in ("DROP TABLE", "DROP COLUMN", "TRUNCATE"):
        assert bad not in up.upper()


def test_runner_script_restores_and_records_usage_before_output_is_read():
    script = (ROOT / "scripts" / "pipeline-runner.sh").read_text(encoding="utf-8")
    restore = script.index('if ! restore_runner_claude_output "$job_id" "$output_file"; then')
    call = script.index('claude_cli) record_runner_cli_usage "$job_id"')
    first_read = script.index('output=$(head -c 50000 "$output_file")')
    assert restore < call < first_read
    # 되돌리기는 codex 재시도·출력 검사보다 먼저, CLI 가 끝난 직후다.
    wait_call = script.index(
        'wait_runner_cli_process "$job_id" "$claude_pid" "$output_file" "$err_file" '
        '"$current_model" "$effective_model" "$job_size" "$((attempt+1))" "$total_attempts" '
        '"$cycle_num" "$runner_kind" "$cli_started_ms" "0" || exit_code=$?'
    )
    assert wait_call < restore < script.index("AADS-241: Codex 연결 재시도")
    assert 'RUNNER_CLI_USAGE_BIN="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/runner_cli_usage.py"' in script


def test_runner_local_template_is_byte_identical():
    """pipeline-runner.sh.local 은 HEAD 에서 .sh 와 바이트 동일한 사본이었다 — 같이 고친다."""
    sh = (ROOT / "scripts" / "pipeline-runner.sh").read_bytes()
    local = (ROOT / "scripts" / "pipeline-runner.sh.local").read_bytes()
    assert sh == local


@pytest.mark.parametrize("call_source,cost_source", [
    ('call_source="cli_relay"', 'cost_source="relay_reported"'),
    ('call_source="codex_relay"', 'cost_source="catalog_estimated"'),
])
def test_model_selector_labels_cost_source(call_source, cost_source):
    src = (ROOT / "app" / "services" / "model_selector.py").read_text(encoding="utf-8")
    idx = src.index(call_source)
    window = src[idx - 400: idx + 50]
    assert cost_source in window


# ── rework 1: 러너 출력 되돌리기와 사용량 기록 분리 (리뷰 지적 1·2) ─────────

RUNNER_SCRIPT = (ROOT / "scripts" / "pipeline-runner.sh").read_text(encoding="utf-8")


def _runner_function(name: str) -> str:
    start = RUNNER_SCRIPT.index(f"{name}() {{")
    return RUNNER_SCRIPT[start:RUNNER_SCRIPT.index("\n}\n", start) + 3]


def _write_exec(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _run_runner_harness(tmp_path: Path, body: str, *, helper: str, jq: str = "") -> subprocess.CompletedProcess:
    """러너 함수 네 개만 떼어 set -eo pipefail 스텁 환경에서 돌린다."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    if jq:
        _write_exec(bindir / "jq", jq)
    harness = tmp_path / "harness.sh"
    harness.write_text(
        "set -eo pipefail\n"
        'log() { echo "LOG $*" >&2; }\n'
        'db_update() { echo "DB $1" >> "$DB_LOG"; }\n'
        f'RUNNER_CLI_USAGE_BIN="{helper}"\n'
        + ("" if jq else "jq() { return 127; }\n")
        + _runner_function("runner_cli_usage_ready")
        + _runner_function("runner_output_is_cli_result_json")
        + _runner_function("restore_runner_claude_output")
        + _runner_function("record_runner_cli_usage")
        + body,
        encoding="utf-8",
    )
    env = dict(os.environ)
    env["PATH"] = f"{bindir}:{env.get('PATH', '')}"
    env["DB_LOG"] = str(tmp_path / "db.log")
    return subprocess.run(["bash", str(harness)], capture_output=True, text=True, timeout=60, env=env)


REAL_HELPER = str(ROOT / "scripts" / "runner_cli_usage.py")


def test_helper_restore_and_usage_are_separate_steps(tmp_path):
    out = tmp_path / "runner-s.out"
    out.write_text(json.dumps(_claude_payload()), encoding="utf-8")
    assert runner_usage.restore_output(str(out)) == "restored"
    assert out.read_text(encoding="utf-8") == "RESULT: 변경 파일 2개\n"
    before = out.read_text(encoding="utf-8")
    sql = runner_usage.usage_sql(
        "claude_cli", str(out), job_id="runner-s", account_slot="1",
        model="claude-opus-5", exit_code=0, duration_ms=1,
    )
    # usage 단계는 출력 파일을 쓰지 않는다 — 실패해도 러너 결과가 오염되지 않는다.
    assert out.read_text(encoding="utf-8") == before
    assert "4.0, 'relay_reported'" in sql and "'runner-s'" in sql


def test_helper_restore_removes_stale_usage_from_previous_attempt(tmp_path):
    out = tmp_path / "runner-r.out"
    (tmp_path / "runner-r.out.usage.json").write_text(json.dumps(_claude_payload()), encoding="utf-8")
    out.write_text("You've hit your limit\n", encoding="utf-8")
    assert runner_usage.restore_output(str(out)) == "not_json"
    assert not (tmp_path / "runner-r.out.usage.json").exists()
    sql = runner_usage.usage_sql(
        "claude_cli", str(out), job_id="runner-r", account_slot="1",
        model="claude-opus-5", exit_code=1, duration_ms=0,
    )
    assert "4.0" not in sql and "'no_usage_json'" not in sql and "NULL, 'unknown'" in sql


def test_helper_parses_result_after_leading_warning_line(tmp_path):
    out = tmp_path / "runner-w.out"
    out.write_text("warning: something\n" + json.dumps(_claude_payload()) + "\n", encoding="utf-8")
    assert runner_usage.restore_output(str(out)) == "restored"
    assert out.read_text(encoding="utf-8") == "RESULT: 변경 파일 2개\n"


def test_runner_restore_with_real_helper(tmp_path):
    out = tmp_path / "o.out"
    out.write_text(json.dumps(_claude_payload()), encoding="utf-8")
    r = _run_runner_harness(
        tmp_path,
        f'restore_runner_claude_output runner-h "{out}" && echo RESTORE_OK\n'
        f'record_runner_cli_usage runner-h claude_cli "{out}" 1 claude-opus-5 0 10\n',
        helper=REAL_HELPER,
    )
    assert r.returncode == 0, r.stderr
    assert "RESTORE_OK" in r.stdout
    assert out.read_text(encoding="utf-8") == "RESULT: 변경 파일 2개\n"
    db = (tmp_path / "db.log").read_text(encoding="utf-8")
    assert "INSERT INTO oauth_usage_log" in db and "'runner-h'" in db


def test_runner_restore_falls_back_to_jq_when_helper_crashes(tmp_path):
    helper = _write_exec(tmp_path / "broken.py", "import sys\nraise SystemExit(3)\n")
    # jq 스텁: 헬퍼와 무관한 경로가 결과만 꺼내는지 본다.
    jq = (
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "p = json.load(open(sys.argv[-1]))\n"
        "sys.stdout.write(p.get('result') or '')\n"
    )
    out = tmp_path / "o.out"
    out.write_text(json.dumps(_claude_payload()), encoding="utf-8")
    r = _run_runner_harness(
        tmp_path, f'restore_runner_claude_output runner-j "{out}" && echo RESTORE_OK\n',
        helper=str(helper), jq=jq,
    )
    assert r.returncode == 0, r.stderr
    assert "RESTORE_OK" in r.stdout
    assert "RUNNER_CLI_JSON_RESTORE_WARN" in r.stderr
    assert out.read_text(encoding="utf-8").startswith("RESULT: 변경 파일 2개")


def test_runner_restore_failure_is_reported_not_swallowed(tmp_path):
    """헬퍼도 jq 도 실패하면 1 을 돌려 시도를 실패로 만든다 — JSON 이 결과로 새지 않는다."""
    helper = _write_exec(tmp_path / "slow.py", "raise RuntimeError('boom')\n")
    out = tmp_path / "o.out"
    raw = json.dumps(_claude_payload())
    out.write_text(raw, encoding="utf-8")
    r = _run_runner_harness(
        tmp_path,
        f'if restore_runner_claude_output runner-f "{out}"; then echo RESTORE_OK; else echo RESTORE_FAILED; fi\n',
        helper=str(helper),
    )
    assert r.returncode == 0, r.stderr
    assert "RESTORE_FAILED" in r.stdout
    assert "RUNNER_CLI_JSON_RESTORE_FAILED job=runner-f" in r.stderr
    assert "boom" in r.stderr


def test_runner_restore_leaves_text_output_alone(tmp_path):
    out = tmp_path / "o.out"
    out.write_text("plain text result\n", encoding="utf-8")
    r = _run_runner_harness(
        tmp_path, f'restore_runner_claude_output runner-t "{out}" && echo RESTORE_OK\n',
        helper=REAL_HELPER,
    )
    assert r.returncode == 0, r.stderr
    assert "RESTORE_OK" in r.stdout
    assert out.read_text(encoding="utf-8") == "plain text result\n"


def test_runner_restored_text_mentioning_result_json_is_not_a_failure(tmp_path):
    """되돌린 결과 본문이 CLI JSON 모양을 인용해도 되돌리기 실패로 보지 않는다."""
    out = tmp_path / "o.out"
    payload = _claude_payload()
    payload["result"] = 'RESULT: 판정은 {"type":"result"} 로 시작하는 파일만 본다'
    out.write_text(json.dumps(payload), encoding="utf-8")
    r = _run_runner_harness(
        tmp_path, f'restore_runner_claude_output runner-q "{out}" && echo RESTORE_OK\n', helper=REAL_HELPER,
    )
    assert r.returncode == 0, r.stderr
    assert "RESTORE_OK" in r.stdout
    assert out.read_text(encoding="utf-8").startswith("RESULT: 판정은")


def test_runner_json_detection_survives_pipefail_on_large_output(tmp_path):
    out = tmp_path / "big.out"
    payload = _claude_payload()
    payload["result"] = "x" * 300_000
    out.write_text(json.dumps(payload), encoding="utf-8")
    r = _run_runner_harness(
        tmp_path, f'runner_output_is_cli_result_json "{out}" && echo IS_JSON\n', helper=REAL_HELPER,
    )
    assert r.returncode == 0, r.stderr
    assert "IS_JSON" in r.stdout


def test_record_usage_logs_missing_helper_and_failure(tmp_path):
    out = tmp_path / "o.out"
    out.write_text("text\n", encoding="utf-8")
    missing = _run_runner_harness(
        tmp_path, f'record_runner_cli_usage runner-m claude_cli "{out}" 1 m 0 1; echo DONE\n',
        helper=str(tmp_path / "nope.py"),
    )
    assert missing.returncode == 0, missing.stderr
    assert "RUNNER_CLI_USAGE_SKIP job=runner-m" in missing.stderr
    helper = _write_exec(tmp_path / "bad.py", "import sys\nsys.stderr.write('parse exploded')\nraise SystemExit(1)\n")
    failed = _run_runner_harness(
        tmp_path, f'record_runner_cli_usage runner-b claude_cli "{out}" 1 m 0 1; echo DONE\n',
        helper=str(helper),
    )
    assert failed.returncode == 0, failed.stderr
    assert "DONE" in failed.stdout
    assert "RUNNER_CLI_USAGE_FAILED job=runner-b" in failed.stderr and "parse exploded" in failed.stderr
    assert not (tmp_path / "db.log").exists()
    assert out.read_text(encoding="utf-8") == "text\n"


# ── rework 1: 스키마 폴백 (리뷰 지적 4·5) ───────────────────────────────

class _PgError(Exception):
    """asyncpg.PostgresError 와 같은 계약(sqlstate·column_name 속성)만 흉내 낸다."""

    def __init__(self, message, sqlstate, column_name=None):
        super().__init__(message)
        self.sqlstate = sqlstate
        self.column_name = column_name


class _FakeConn:
    def __init__(self, failures):
        self.failures = list(failures)
        self.sqls = []

    async def execute(self, sql, *args):
        self.sqls.append(sql)
        if self.failures:
            exc = self.failures.pop(0)
            if exc is not None:
                raise exc


class _FakeAcquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *a):
        return False


class _FakePool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return _FakeAcquire(self.conn)


def _entry(**over):
    e = {
        "account_slot": "1", "token_prefix": "", "model": "claude-opus-5",
        "input_tokens": 1, "output_tokens": 1,
        "cache_creation_tokens": 0, "cache_read_tokens": 0,
        "cost_usd": None, "cost_source": "unknown", "rl": {},
        "call_source": "cli_relay", "session_id": "", "error_code": None,
        "duration_ms": 0, "tenant_id": None, "job_id": "runner-1",
    }
    e.update(over)
    return e


@pytest.fixture
def fresh_schema_state(monkeypatch):
    monkeypatch.setattr(tracker, "_COST_BASIS_COLUMNS_MISSING_UNTIL", 0.0)
    monkeypatch.setattr(tracker, "_COST_BASIS_CATALOG_MISSING_UNTIL", 0.0)
    clock = {"now": 1000.0}
    monkeypatch.setattr(tracker._time, "monotonic", lambda: clock["now"])
    return clock


def test_catalog_function_missing_keeps_cost_source_and_job_id(monkeypatch, fresh_schema_state):
    conn = _FakeConn([_PgError('function llm_catalog_cost_usd(text, bigint, bigint) does not exist', "42883"), None])
    monkeypatch.setattr(tracker, "get_pool", lambda: _FakePool(conn))
    asyncio.run(tracker._insert_usage_batch([_entry()]))
    assert "llm_catalog_cost_usd(" in conn.sqls[0]
    # 두 번째 시도는 새 열은 유지하고 정가 함수만 뺀다.
    assert "cost_source" in conn.sqls[1] and "job_id" in conn.sqls[1]
    assert "llm_catalog_cost_usd(" not in conn.sqls[1]


def test_schema_fallback_is_rechecked_after_migration(monkeypatch, fresh_schema_state):
    clock = fresh_schema_state
    missing_col = _PgError('column "cost_source" of relation "oauth_usage_log" does not exist', "42703")
    # (새 열+정가) 실패 → (새 열) 실패 → (옛 열) 성공.
    conn = _FakeConn([missing_col, missing_col, None])
    monkeypatch.setattr(tracker, "get_pool", lambda: _FakePool(conn))
    asyncio.run(tracker._insert_usage_batch([_entry()]))
    assert len(conn.sqls) == 3
    assert "cost_source" not in conn.sqls[-1]
    # 재확인 전에는 옛 열로 바로 간다.
    asyncio.run(tracker._insert_usage_batch([_entry()]))
    assert "cost_source" not in conn.sqls[-1]
    # 마이그레이션이 운영 중에 적용되고 재확인 시간이 지나면 재시작 없이 새 열로 돌아온다.
    clock["now"] += tracker._COST_BASIS_RECHECK_SEC + 1
    asyncio.run(tracker._insert_usage_batch([_entry()]))
    assert "cost_source" in conn.sqls[-1] and "llm_catalog_cost_usd(" in conn.sqls[-1]


def test_cost_usd_not_null_violation_is_not_requeued(monkeypatch, fresh_schema_state):
    err = _PgError('null value in column "cost_usd" of relation "oauth_usage_log" violates not-null constraint',
                   "23502", column_name="cost_usd")
    conn = _FakeConn([err])
    monkeypatch.setattr(tracker, "get_pool", lambda: _FakePool(conn))
    monkeypatch.setattr(tracker, "_USAGE_LOG_BUFFER", [_entry()])

    async def run():
        await tracker._flush_usage_buffer()

    asyncio.run(run())
    assert tracker._USAGE_LOG_BUFFER == []  # 무한 재시도 버퍼로 되돌리지 않는다
    assert len(conn.sqls) == 1


def test_other_insert_errors_are_still_requeued(monkeypatch, fresh_schema_state):
    conn = _FakeConn([Exception("connection reset")])
    monkeypatch.setattr(tracker, "get_pool", lambda: _FakePool(conn))
    monkeypatch.setattr(tracker, "_USAGE_LOG_BUFFER", [_entry()])
    asyncio.run(tracker._flush_usage_buffer())
    assert len(tracker._USAGE_LOG_BUFFER) == 1


def test_migration_guarantees_cost_usd_nullable_and_passes_destructive_gate():
    up = (ROOT / "migrations" / "20260930_oauth_usage_cost_basis.sql").read_text(encoding="utf-8")
    drop_nn = up.index("ALTER COLUMN cost_usd DROP NOT NULL")
    assert drop_nn < up.index("SET cost_usd = NULL")
    gate = (ROOT / "scripts" / "apply_release_migrations.sh").read_text(encoding="utf-8")
    start = gate.index("destructive_reason() {")
    fn = gate[start:gate.index("\n}\n", start) + 3]
    r = subprocess.run(
        ["bash", "-c", fn + 'destructive_reason "$1"', "_",
         str(ROOT / "migrations" / "20260930_oauth_usage_cost_basis.sql")],
        capture_output=True, text=True, timeout=30,
    )
    assert r.returncode == 0 and r.stdout.strip() == "", r.stdout + r.stderr


# ── rework 1: 모델 SDK 경로 비용출처 판정 (리뷰 지적 6) ─────────────────

def test_model_selector_sdk_cost_source_uses_initialized_same_criterion():
    src = (ROOT / "app" / "services" / "model_selector.py").read_text(encoding="utf-8")
    start = src.index("async def _run_agent_sdk_with_key(")
    body = src[start:src.index("\nasync def ", start + 10)]
    init = body.index("total_cost = 0.0")
    decide = body.index("_sdk_cost_reported = bool(total_cost)")
    assert init < decide
    assert "cost = total_cost if _sdk_cost_reported else" in body
    assert 'cost_source="relay_reported" if _sdk_cost_reported else "catalog_estimated"' in body


# ── rework 1: 성공 작업당 비용 분모 중복 제거 (리뷰 지적 8) ──────────────

def _runner_row(job, model, cost, status="done"):
    return {"call_source": "runner_claude_cli", "model": model, "cost_source": "relay_reported",
            "job_id": job, "job_status": status, "calls": 1, "cost_usd": cost}


def test_multi_model_job_counted_once_in_denominator():
    rows = []
    for i in range(5):
        rows.append(_runner_row(f"runner-{i}", "claude-opus-5", 4.0))
        rows.append(_runner_row(f"runner-{i}", "claude-haiku-4-5", 1.0))  # 같은 작업의 보조 모델
    items = cb.summarize_usage_rows(rows)
    by_model = {i["model"]: i for i in items}
    assert by_model["claude-opus-5"]["success_jobs"] == 5
    assert by_model["claude-haiku-4-5"]["success_jobs"] == 5
    assert by_model["claude-opus-5"]["cost_per_success_usd"] == 4.0
    assert by_model["claude-haiku-4-5"]["cost_per_success_usd"] == 1.0
    surf = cb.summarize_by_surface(items)
    assert len(surf) == 1
    s0 = surf[0]
    # 작업 5개, 작업당 5.0 — 모델마다 분모에 다시 들어가지 않는다.
    assert s0["success_jobs"] == 5 and s0["jobs"] == 5
    assert s0["total_cost_usd"] == 25.0
    assert s0["cost_per_success_usd"] == 5.0
    assert s0["cost_per_success_status"] == "ok"
    # 모델 몫을 더하면 표면 작업당 비용이다.
    assert sum(i["cost_per_success_usd"] for i in items) == s0["cost_per_success_usd"]


def test_model_share_uses_surface_denominator_when_model_used_by_few_jobs():
    rows = [_runner_row(f"runner-{i}", "claude-opus-5", 2.0) for i in range(6)]
    rows.append(_runner_row("runner-0", "claude-haiku-4-5", 0.6))
    items = {i["model"]: i for i in cb.summarize_usage_rows(rows)}
    haiku = items["claude-haiku-4-5"]
    assert haiku["success_jobs"] == 6
    assert haiku["success_jobs_using_model"] == 1
    assert haiku["cost_per_success_usd"] == 0.1
    surf = cb.summarize_by_surface(list(items.values()))[0]
    assert surf["cost_per_success_usd"] == 2.1


def test_by_surface_keeps_undefined_and_insufficient_status():
    chat = [{"call_source": "cli_relay", "model": "m", "cost_source": "relay_reported",
             "session_is_chat": True, "calls": 3, "cost_usd": 9.0}]
    runner = [_runner_row(f"runner-{i}", "m", 1.0) for i in range(3)]
    surf = {s["surface"]: s for s in cb.summarize_by_surface(cb.summarize_usage_rows(chat + runner))}
    assert surf["chat"]["cost_per_success_usd"] is None
    assert surf["chat"]["cost_per_success_status"] == "denominator_undefined"
    assert surf["runner"]["cost_per_success_usd"] is None
    assert surf["runner"]["cost_per_success_status"] == "insufficient_sample"


# ── rework 2: 스키마 부재 판정은 SQLSTATE 로 (리뷰 지적 3·4) ────────────

def test_schema_gap_uses_sqlstate_not_message_text():
    # 문구가 다른 로케일이어도 SQLSTATE 만으로 판정한다.
    localized = _PgError("열 \"cost_source\" 없음", "42703")
    assert tracker._cost_basis_schema_gap(localized, with_catalog=False) == "columns"
    # 영문 "does not exist" 문구라도 SQLSTATE 가 없으면 스키마 부재로 보지 않는다.
    plain = Exception('column "cost_source" of relation "oauth_usage_log" does not exist')
    assert tracker._cost_basis_schema_gap(plain, with_catalog=False) is None
    assert tracker._cost_basis_schema_gap(plain, with_catalog=True) is None
    # 연결 오류 등 다른 SQLSTATE 는 None — 버퍼 재시도로 간다.
    assert tracker._cost_basis_schema_gap(_PgError("x", "08006"), with_catalog=True) is None
    # 정가 함수를 뺀 시도에서 42883 은 이 마이그레이션과 무관하다.
    assert tracker._cost_basis_schema_gap(_PgError("x", "42883"), with_catalog=False) is None


def test_catalog_function_body_column_missing_falls_back_not_requeued(monkeypatch, fresh_schema_state):
    # 정가 함수 본문이 읽는 llm_models 열이 런타임에 없을 때(리뷰 지적 3).
    # 문구에 llm_catalog_cost_usd/cost_usd_catalog 가 없어도 catalog 부재로 보고
    # 정가 함수만 빼서 적는다 — 버퍼로 되돌려 무한 재시도하지 않는다.
    err = _PgError("column m.provider does not exist", "42703")
    conn = _FakeConn([err, None])
    monkeypatch.setattr(tracker, "get_pool", lambda: _FakePool(conn))
    monkeypatch.setattr(tracker, "_USAGE_LOG_BUFFER", [_entry()])
    asyncio.run(tracker._flush_usage_buffer())
    assert tracker._USAGE_LOG_BUFFER == []
    assert len(conn.sqls) == 2
    assert "llm_catalog_cost_usd(" not in conn.sqls[1]
    assert "cost_source" in conn.sqls[1] and "job_id" in conn.sqls[1]
    assert tracker._COST_BASIS_CATALOG_MISSING_UNTIL > 0
    assert tracker._COST_BASIS_COLUMNS_MISSING_UNTIL == 0.0


def test_schema_gap_matches_real_asyncpg_exceptions():
    exc = pytest.importorskip("asyncpg.exceptions")
    assert tracker._cost_basis_schema_gap(
        exc.UndefinedFunctionError("function llm_catalog_cost_usd does not exist"), with_catalog=True
    ) == "catalog"
    assert tracker._cost_basis_schema_gap(
        exc.UndefinedTableError('relation "llm_models" does not exist'), with_catalog=True
    ) == "catalog"
    assert tracker._cost_basis_schema_gap(
        exc.UndefinedColumnError('column "job_id" does not exist'), with_catalog=False
    ) == "columns"
    nn = exc.NotNullViolationError("null value")
    nn.column_name = "cost_usd"
    assert tracker._is_cost_usd_not_null_violation(nn)
    other = exc.NotNullViolationError("null value")
    other.column_name = "tenant_id"
    assert not tracker._is_cost_usd_not_null_violation(other)


def test_catalog_function_columns_exist_in_llm_models_schema():
    # 정가 함수가 읽는 llm_models 열이 053 스키마에 모두 있다(리뷰 지적 3).
    # 2026-09-30 운영 DB 에서도 check_function_bodies=on 으로 컴파일 확인.
    schema = (ROOT / "migrations" / "053_llm_model_registry.sql").read_text(encoding="utf-8")
    table = schema[schema.index("CREATE TABLE"):schema.index(");", schema.index("CREATE TABLE"))]
    for col in ("id SERIAL", "provider ", "model_id ", "input_cost ", "output_cost ", "is_active "):
        assert col in table, col
    up = (ROOT / "migrations" / "20260930_oauth_usage_cost_basis.sql").read_text(encoding="utf-8")
    fn = up[up.index("FUNCTION public.llm_catalog_cost_usd("):]
    for ref in ("m.provider", "m.is_active", "m.id", "m.model_id", "m.input_cost", "m.output_cost"):
        assert ref in fn, ref


def test_dead_schema_helper_removed():
    assert not hasattr(tracker, "_is_missing_cost_basis_schema")


# ── rework 2: 상태값과 비용출처 이름공간 분리 (리뷰 지적 6) ─────────────

def test_cost_per_success_cost_unknown_has_own_status():
    r = cb.cost_per_success(None, 10, denominator_defined=True)
    assert r == {"value": None, "status": cb.PER_SUCCESS_COST_UNKNOWN}
    assert cb.PER_SUCCESS_COST_UNKNOWN not in cb.COST_SOURCES
    assert set(cb.PER_SUCCESS_STATUSES).isdisjoint(cb.COST_SOURCES)


# ── rework 2: cli_relay 의 미보고 0.00 을 보고값으로 백필하지 않음 (리뷰 지적 5) ──

def test_backfill_does_not_label_cli_relay_zero_as_relay_reported():
    up = (ROOT / "migrations" / "20260930_oauth_usage_cost_basis.sql").read_text(encoding="utf-8")
    start = up.index("SET cost_source = 'relay_reported'")
    stmt = up[start:up.index(";", start)]
    assert "call_source = 'cli_relay'" in stmt
    assert "cost_usd <> 0" in stmt
    null_start = up.index("SET cost_usd = NULL\n WHERE call_source = 'cli_relay'")
    assert up.index("ALTER COLUMN cost_usd DROP NOT NULL") < null_start
    down = (ROOT / "migrations" / "rollback" / "20260930_oauth_usage_cost_basis.down.sql").read_text(
        encoding="utf-8"
    )
    assert "WHERE call_source = 'cli_relay'\n   AND cost_usd IS NULL" in down


# ── rework 2: 내부 DB 오류 문구를 응답에 싣지 않음 (리뷰 지적 8) ─────────

def test_per_success_endpoint_does_not_leak_db_error(monkeypatch):
    from fastapi import HTTPException

    from app.api import ops

    async def boom():
        raise RuntimeError('relation "oauth_usage_log" column "cost_source" does not exist')

    monkeypatch.setattr(ops, "_get_conn", boom)
    with pytest.raises(HTTPException) as ei:
        asyncio.run(ops.ops_llm_cost_per_success(days=7))
    assert ei.value.status_code == 500
    assert "oauth_usage_log" not in str(ei.value.detail)
    assert "cost_source" not in str(ei.value.detail)
