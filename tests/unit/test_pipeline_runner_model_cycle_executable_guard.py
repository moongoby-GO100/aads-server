"""get_db_model_cycle 의 codex is_executable 가드 회귀시험.

Python 경로(app/services/pipeline_runner_service.py 의 codex_executable)와
bash 경로(scripts/pipeline-runner.sh get_db_model_cycle)가 같은 가드를 유지해야
llm_models.is_executable=false 인 codex 모델이 폴백 체인에 들어가 codex CLI 400 으로
실패하는 일을 막는다.
"""
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ("pipeline-runner.sh", "pipeline-runner.sh.local")


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _cycle_body(script_name: str) -> str:
    script = _read(f"scripts/{script_name}")
    start = script.index("get_db_model_cycle() {")
    return script[start:script.index("\n}\n", start)]


def _group2_subquery(body: str) -> str:
    start = body.index("SELECT 2 AS group_order")
    return body[start:body.index("), ranked AS (", start)]


def _config_subqueries(body: str) -> str:
    start = body.index("WITH candidates AS (")
    return body[start:body.index("SELECT 2 AS group_order", start)]


def test_get_db_model_cycle_has_is_executable_guard():
    for name in SCRIPTS:
        body = _cycle_body(name)
        assert "is_executable = TRUE" in body
        assert re.search(r"FROM llm_models\b.*?lm\.provider = 'codex'", body, re.S)


def test_guard_scoped_to_routing_preferences_not_runner_model_config():
    for name in SCRIPTS:
        body = _cycle_body(name)
        group2 = _group2_subquery(body)
        assert "is_executable" in group2
        assert "model_routing_preferences" in group2
        assert "llm_models" in group2
        config = _config_subqueries(body)
        assert "runner_model_config" in config
        assert "is_executable" not in config
        assert "llm_models" not in config


def test_guard_covers_all_rows_resolved_to_codex():
    for name in SCRIPTS:
        group2 = _group2_subquery(_cycle_body(name))
        assert "mrp.provider IN ('codex','openai') AND mrp.model_id LIKE 'gpt-%'" in group2
        assert "EXISTS" in group2
        assert "lm.model_id = mrp.model_id" in group2


def test_guard_does_not_change_query_failure_fallback():
    for name in SCRIPTS:
        body = _cycle_body(name)
        assert "2>/dev/null) || return 1" in body
        assert body.count("db_exec ") == 1


def test_pipeline_runner_sh_and_local_cycle_bodies_identical():
    assert _cycle_body("pipeline-runner.sh") == _cycle_body("pipeline-runner.sh.local")


def test_python_path_still_has_codex_executable_guard():
    src = _read("app/services/pipeline_runner_service.py")
    assert "provider = 'codex' AND is_executable = TRUE" in src
    assert "codex_executable" in src
    assert "pipeline_c_model_chain_excluded" in src


def test_group2_sql_excludes_non_executable_codex_model_on_live_db():
    """DB 접속 가능할 때만: 실행 불가 codex 모델이 결과에 없어야 한다."""
    try:
        probe = subprocess.run(
            ["docker", "exec", "-i", "aads-postgres", "psql", "-U", "aads", "-d", "aads",
             "-q", "-t", "-A", "-c", "SELECT 1"],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        pytest.skip("docker/DB 접속 불가")
    if probe.returncode != 0 or probe.stdout.strip() != "1":
        pytest.skip("aads-postgres 접속 불가")

    body = _cycle_body("pipeline-runner.sh")
    start = body.index('db_exec "') + len('db_exec "')
    sql = body[start:body.index('" 2>/dev/null) || return 1')].replace("${size}", "S")
    res = subprocess.run(
        ["docker", "exec", "-i", "aads-postgres", "psql", "-U", "aads", "-d", "aads",
         "-q", "-t", "-A"],
        input=sql, capture_output=True, text=True, timeout=30,
    )
    assert res.returncode == 0, res.stderr
    chain = set(res.stdout.split())
    non_exec = subprocess.run(
        ["docker", "exec", "-i", "aads-postgres", "psql", "-U", "aads", "-d", "aads",
         "-q", "-t", "-A", "-c",
         "SELECT model_id FROM llm_models WHERE provider='codex' AND is_executable=FALSE"],
        capture_output=True, text=True, timeout=15,
    )
    for model_id in non_exec.stdout.split():
        assert f"codex:{model_id}" not in chain
