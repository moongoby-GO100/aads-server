"""R-001 개정(2026-10-08): 핸드오버는 DB 에만 기록하고 러너는 HANDOVER.md 를 커밋하지 않는다.

① 워커가 HANDOVER.md 를 고쳐도 승인 커밋에 들어가지 않고 워크트리 파일은 남는다
② RUNNER_ALLOW_HANDOVER_MD=1 이면 예전처럼 들어간다
③ DB upsert 는 entry_key=runner:<job_id> 로 멱등이다
④ DB 쓰기가 실패하면 이벤트를 남기고 작업은 계속된다
⑤ 프롬프트·규칙 문구 회귀
"""

import asyncio
import importlib.util
import os
import shutil
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
HANDOVER = "HANDOVER.md"


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _fn(script: str, name: str) -> str:
    start = script.index(f"{name}() {{")
    return script[start:script.index("\n}\n", start) + 3]


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture()
def need_tools():
    if shutil.which("bash") is None or shutil.which("git") is None:
        pytest.skip("bash/git 미설치 환경")


def _repo(tmp_path: Path) -> tuple[Path, str]:
    wt = tmp_path / "wt"
    subprocess.run(["git", "init", "-q", str(wt)], check=True)
    _git(wt, "config", "user.email", "t@example.com")
    _git(wt, "config", "user.name", "Test")
    (wt / "app.py").write_text("a = 1\n", encoding="utf-8")
    (wt / HANDOVER).write_text("# H\n", encoding="utf-8")
    (wt / "docs").mkdir()
    (wt / "docs" / HANDOVER).write_text("# D\n", encoding="utf-8")
    _git(wt, "add", "-A")
    _git(wt, "commit", "-q", "-m", "seed")
    base = _git(wt, "rev-parse", "HEAD")
    _git(wt, "checkout", "-q", "--detach")
    _git(wt, "branch", "-f", "main", base)
    return wt, base


def _commit(tmp_path: Path, wt: Path, base: str, allow: bool = False) -> Path:
    script = _read("scripts/pipeline-runner.sh")
    events = tmp_path / "events.txt"
    body = f"""
set -o pipefail
log() {{ echo "$*"; }}
sql_escape() {{ printf "%s" "'$1'"; }}
db_update() {{ :; }}
record_runner_event() {{ printf '%s|%s|%s\\n' "$2" "$3" "$9" >> "$EVENTS_FILE"; }}
verify_isolated_job_worktree() {{ return 0; }}
requeue_scope_violations() {{ return 0; }}
is_deploy_only_instruction() {{ return 1; }}
record_git_diagnostics() {{ echo diag; }}
_fail_job() {{ printf '%s\\n' "$3|$4" >> "$FAIL_FILE"; return 1; }}
{_fn(script, "mask_git_diagnostics")}
{_fn(script, "verify_worker_commit_provenance")}
{_fn(script, "commit_job_worktree_for_approval")}
"""
    f = tmp_path / "fn.sh"
    f.write_text(body, encoding="utf-8")
    fail = tmp_path / "fail.txt"
    env = {"PATH": os.environ["PATH"], "FAIL_FILE": str(fail), "EVENTS_FILE": str(events), "HOME": str(tmp_path)}
    if allow:
        env["RUNNER_ALLOW_HANDOVER_MD"] = "1"
    proc = subprocess.run(
        ["bash", "-c", f'source "{f}"; commit_job_worktree_for_approval runner-abc12345 s "{wt}" /m "inst" "{base}"'],
        capture_output=True, text=True, env=env,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr + (fail.read_text() if fail.exists() else "")
    assert proc.stdout.strip().splitlines()[-1] == _git(wt, "rev-parse", "HEAD")
    return events


def _changed(wt: Path, base: str) -> list[str]:
    return sorted(_git(wt, "diff", "--name-only", f"{base}..HEAD").splitlines())


def test_handover_md_changes_are_not_committed_and_files_are_preserved(tmp_path, need_tools):
    wt, base = _repo(tmp_path)
    (wt / "app.py").write_text("a = 2\n", encoding="utf-8")
    (wt / HANDOVER).write_text("# H\n" + "x\n" * 250, encoding="utf-8")
    (wt / "docs" / HANDOVER).write_text("# D2\n", encoding="utf-8")
    (wt / "sub").mkdir()
    (wt / "sub" / HANDOVER).write_text("new untracked\n", encoding="utf-8")
    events = _commit(tmp_path, wt, base)
    assert _changed(wt, base) == ["app.py"]
    assert (wt / HANDOVER).read_text(encoding="utf-8").count("x\n") == 250
    assert (wt / "docs" / HANDOVER).read_text(encoding="utf-8") == "# D2\n"
    assert (wt / "sub" / HANDOVER).exists()
    line = events.read_text(encoding="utf-8")
    assert line.startswith("handover_md_excluded|info|")
    assert '"HANDOVER.md":250' in line and '"lines":' in line


def test_deleted_handover_md_is_not_committed(tmp_path, need_tools):
    wt, base = _repo(tmp_path)
    (wt / "app.py").write_text("a = 2\n", encoding="utf-8")
    (wt / HANDOVER).unlink()
    _commit(tmp_path, wt, base)
    assert _changed(wt, base) == ["app.py"]
    assert not (wt / HANDOVER).exists()


def test_no_event_when_handover_md_untouched(tmp_path, need_tools):
    wt, base = _repo(tmp_path)
    (wt / "app.py").write_text("a = 2\n", encoding="utf-8")
    events = _commit(tmp_path, wt, base)
    assert _changed(wt, base) == ["app.py"]
    assert not events.exists()


def test_allow_env_restores_old_behavior(tmp_path, need_tools):
    wt, base = _repo(tmp_path)
    (wt / "app.py").write_text("a = 2\n", encoding="utf-8")
    (wt / HANDOVER).write_text("# H\nmore\n", encoding="utf-8")
    events = _commit(tmp_path, wt, base, allow=True)
    assert _changed(wt, base) == sorted(["app.py", HANDOVER])
    assert not events.exists()


# ── DB 기록 ───────────────────────────────────────────────────────────────


def _helper():
    spec = importlib.util.spec_from_file_location("runner_handover_write", ROOT / "scripts" / "runner_handover_write.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeStore:
    def __init__(self, fail=False):
        self.rows = {}
        self.calls = 0
        self.fail = fail

    @staticmethod
    def normalize_project_key(v):
        return v.upper()

    async def upsert_handover_entry(self, **kw):
        self.calls += 1
        if self.fail:
            raise RuntimeError("db down")
        k = (kw["tenant_id"], kw["project_key"], kw["entry_key"])
        cur = self.rows.get(k)
        cmp = {x: kw[x] for x in ("title", "body", "entry_type", "source_kind", "source_task_id", "metadata")}
        if cur and cur["cmp"] == cmp:
            return {"id": cur["id"]}, False
        rev = (cur["revision"] + 1) if cur else 1
        self.rows[k] = {"cmp": cmp, "revision": rev, "id": "id-1", "kw": kw}
        return {"id": "id-1"}, True

    @asynccontextmanager
    async def _connection(self):
        rows = self.rows

        class Conn:
            async def fetchrow(self, _sql, tenant, project, key):
                r = rows.get((tenant, project, key))
                return {"revision": r["revision"]} if r else None

        yield Conn()


PAYLOAD = {
    "tenant_id": "11111111-1111-1111-1111-111111111111",
    "project": "aads",
    "job_id": "runner-abc12345",
    "stage": "awaiting_approval",
    "job": {
        "instruction": "TASK_ID: X\nTITLE: " + "가" * 300 + "\nbody",
        "commit_hash": "a" * 40,
        "status": "awaiting_approval",
        "review_verdict": "APPROVE",
        "review_score": 0.9,
        "result_output": "변경 요약\n12 passed in 3.2s\nruff OK\n",
        "changed_files": ["app/a.py", "scripts/b.sh"],
    },
}


def test_db_upsert_is_idempotent_by_entry_key():
    mod = _helper()
    store = _FakeStore()
    r1 = asyncio.run(mod.write_and_verify(PAYLOAD, store=store))
    r2 = asyncio.run(mod.write_and_verify(PAYLOAD, store=store))
    assert r1["ok"] and r1["entry_key"] == "runner:runner-abc12345" and r1["revision"] == 1 and r1["changed"] is True
    assert r2["ok"] and r2["revision"] == 1 and r2["changed"] is False
    assert len(store.rows) == 1
    row = next(iter(store.rows.values()))["kw"]
    assert row["entry_type"] == "verification" and row["source_kind"] == "runner"
    assert row["source_task_id"] == "runner-abc12345" and row["project_key"] == "AADS"
    assert len(row["title"]) == 200
    for needle in ("a" * 40, "app/a.py", "12 passed", "변경 요약"):
        assert needle in row["body"]
    done = dict(PAYLOAD, stage="done")
    r3 = asyncio.run(mod.write_and_verify(done, store=store))
    assert r3["revision"] == 2 and len(store.rows) == 1


def test_db_write_failure_is_reported_not_raised(monkeypatch, capsys):
    mod = _helper()
    store = _FakeStore(fail=True)
    with pytest.raises(RuntimeError):
        asyncio.run(mod.write_and_verify(PAYLOAD, store=store))
    monkeypatch.setattr(mod, "write_and_verify", lambda payload: (_ for _ in ()).throw(RuntimeError("db down")))
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO("{}"))
    assert mod.main() == 1
    assert '"ok": false' in capsys.readouterr().out


def _record_harness(tmp_path: Path, helper_src: str, project_root_helper: bool = True) -> tuple[Path, Path, Path]:
    script = _read("scripts/pipeline-runner.sh")
    scripts_dir = tmp_path / "repo" / "scripts"
    scripts_dir.mkdir(parents=True)
    (scripts_dir / "runner_handover_write.py").write_text(helper_src, encoding="utf-8")
    events, sqls = tmp_path / "events.txt", tmp_path / "sqls.txt"
    body = f"""
log() {{ echo "$*"; }}
sql_escape() {{ printf "%s" "'$1'"; }}
db_exec() {{ printf '%s' '{{"tenant_id":"t","project":"AADS","job_id":"runner-abc12345","stage":"done","job":{{}}}}'; }}
record_runner_event() {{ printf '%s|%s\\n' "$2" "$9" >> "$EVENTS_FILE"; }}
db_update() {{ printf '%s\\n' "$1" >> "$SQLS_FILE"; }}
DB_MODE=psql; PGHOST=h; PGUSER=u; PGPASSWORD=p; PGDATABASE=d
{_fn(script, "runner_handover_record")}
"""
    f = scripts_dir / "fn.sh"
    f.write_text(body, encoding="utf-8")
    return f, events, sqls


def _run_record(tmp_path, helper_src, extra_env=None):
    f, events, sqls = _record_harness(tmp_path, helper_src)
    env = {"PATH": os.environ["PATH"], "EVENTS_FILE": str(events), "SQLS_FILE": str(sqls), "HOME": str(tmp_path)}
    env.update(extra_env or {})
    proc = subprocess.run(
        ["bash", "-c", f'source "{f}"; runner_handover_record runner-abc12345 done; echo rc=$?'],
        capture_output=True, text=True, env=env,
    )
    return proc, events, sqls


def test_record_failure_logs_event_marks_job_and_continues(tmp_path, need_tools):
    helper = 'import sys\nprint(\'{"ok": false, "error": "RuntimeError: db down"}\')\nsys.exit(1)\n'
    proc, events, sqls = _run_record(tmp_path, helper)
    assert "rc=0" in proc.stdout
    assert "HANDOVER_DB_WRITE_FAILED" in proc.stdout
    assert events.read_text(encoding="utf-8").startswith("handover_db_write_failed|")
    assert "핸드오버 DB 미기록" in sqls.read_text(encoding="utf-8")


def test_record_success_logs_revision(tmp_path, need_tools):
    helper = 'print(\'{"ok": true, "entry_key": "runner:runner-abc12345", "revision": 3, "changed": true}\')\n'
    proc, events, sqls = _run_record(tmp_path, helper)
    assert "rc=0" in proc.stdout and "revision=3" in proc.stdout
    assert '"revision":3' in events.read_text(encoding="utf-8")
    assert not sqls.exists()


def test_record_can_be_disabled(tmp_path, need_tools):
    proc, events, _ = _run_record(tmp_path, "raise SystemExit(1)\n", {"RUNNER_HANDOVER_DB_WRITE": "0"})
    assert "rc=0" in proc.stdout and not events.exists()


# ── 문구 회귀 ──────────────────────────────────────────────────────────────


def test_worker_prompt_forbids_handover_md():
    script = _read("scripts/pipeline-runner.sh")
    assert "HANDOVER.md 수정 금지" in script
    assert "handover_md_rule" in script
    assert 'RUNNER_ALLOW_HANDOVER_MD' in script


def test_runner_and_local_copy_are_identical():
    assert _read("scripts/pipeline-runner.sh") == _read("scripts/pipeline-runner.sh.local")


def test_runner_records_handover_at_approval_and_done():
    script = _read("scripts/pipeline-runner.sh")
    assert script.count('runner_handover_record "$job_id" "awaiting_approval"') >= 3
    assert script.count('runner_handover_record "$job_id" "done"') >= 5


@pytest.mark.parametrize("rel,old", [
    ("app/core/prompts/system_prompt_v2.py", "완료 전에 HANDOVER.md를 갱신하라"),
    ("app/api/channels.py", "반드시 HANDOVER.md 업데이트"),
    ("app/services/cto_mode.py", "  - HANDOVER.md 업데이트\n"),
    ("app/services/chat_tools.py", "HANDOVER.md 업데이트 포함"),
    ("CLAUDE.md", "HANDOVER 업데이트 없이 완료 선언 금지"),
])
def test_file_write_instruction_removed(rel, old):
    text = _read(rel)
    assert old not in text
    assert "handover_write" in text


def test_rule_text_lives_in_agents_md_and_claude_md_points_to_it():
    claude, agents = _read("CLAUDE.md"), _read("AGENTS.md")
    assert "`HANDOVER.md` 파일 수정 금지" in claude and "R-001, 2026-10-08 개정" in claude
    assert "## 핸드오버 기록 (R-001" in agents
    assert "RUNNER_ALLOW_HANDOVER_MD=1" in agents and "runner:<job_id>" in agents
