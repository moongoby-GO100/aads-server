import asyncio
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).parents[2] / "scripts/coalesce_deploy_queue.py"
SPEC = importlib.util.spec_from_file_location("deploy_queue_batcher", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)
QueueItem = MODULE.QueueItem
classify_batch = MODULE.classify_batch
apply_decision = MODULE._apply_decision

OBSERVABILITY_PATH = Path(__file__).parents[2] / "app/services/deploy_observability.py"
OBSERVABILITY_SPEC = importlib.util.spec_from_file_location("deploy_observability_batch_test", OBSERVABILITY_PATH)
assert OBSERVABILITY_SPEC and OBSERVABILITY_SPEC.loader
OBSERVABILITY = importlib.util.module_from_spec(OBSERVABILITY_SPEC)
sys.modules[OBSERVABILITY_SPEC.name] = OBSERVABILITY
OBSERVABILITY_SPEC.loader.exec_module(OBSERVABILITY)


def item(run_id: int, sha: str, *, files=("app/main.py",), flags=(), policy="auto_if_green"):
    return QueueItem(run_id, sha, "queued_for_deploy", "api_bluegreen", policy, files, flags)


def test_batches_only_proven_low_risk_ancestors(monkeypatch, tmp_path):
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0))
    representative, included, reason = classify_batch([item(10, "a" * 40), item(11, "b" * 40)], tmp_path)
    assert representative.run_id == 11
    assert [row.run_id for row in included] == [10]
    assert reason == "all_older_requests_are_low_risk_ancestors"


def test_unknown_manifest_stays_fifo_and_is_not_superseded(tmp_path):
    representative, included, reason = classify_batch([item(10, "a" * 40, files=()), item(11, "b" * 40)], tmp_path)
    assert representative.run_id == 10
    assert included == []
    assert reason == "candidate_manifest_unknown_or_risky"


def test_migration_or_dependency_change_stays_fifo(tmp_path):
    representative, included, reason = classify_batch([
        item(10, "a" * 40, files=("migrations/20260919_x.sql",)),
        item(11, "b" * 40),
    ], tmp_path)
    assert representative.run_id == 10
    assert included == []
    assert reason == "candidate_manifest_unknown_or_risky"


def test_non_ancestor_stays_fifo(monkeypatch, tmp_path):
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 1))
    representative, included, reason = classify_batch([item(10, "a" * 40), item(11, "b" * 40)], tmp_path)
    assert representative.run_id == 10
    assert included == []
    assert reason == "git_ancestry_not_proven"


def test_real_git_history_proves_ancestor_batch(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "batch-test"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "batch@test.invalid"], check=True)
    tracked = tmp_path / "tracked.txt"
    tracked.write_text("one\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "tracked.txt"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "one"], check=True)
    older = subprocess.check_output(["git", "-C", str(tmp_path), "rev-parse", "HEAD"], text=True).strip()
    tracked.write_text("two\n")
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qam", "two"], check=True)
    newer = subprocess.check_output(["git", "-C", str(tmp_path), "rev-parse", "HEAD"], text=True).strip()

    representative, included, reason = classify_batch([item(10, older), item(11, newer)], tmp_path)

    assert representative.release_sha == newer
    assert [row.release_sha for row in included] == [older]
    assert reason == "all_older_requests_are_low_risk_ancestors"


def test_drain_runs_batcher_before_selecting_oldest_ready_request():
    text = (Path(__file__).parents[2] / "scripts/aads_deploy_drain.sh").read_text()
    assert text.index('python3 "$BATCHER"') < text.index('rows="$(')
    assert "target_env, created_at ASC, id ASC" in text
    assert "refusing legacy newest-wins drain" in text
    assert "deploy batch classification failed; queue left unchanged" in text


def test_direct_worker_runs_batcher_before_selecting_fifo_ready_head():
    text = (Path(__file__).parents[2] / "scripts/start_aads_deploy_queue_worker.sh").read_text()
    assert text.index('python3 "$BATCHER"') < text.index('latest_sha="$(')
    assert "ORDER BY created_at ASC, id ASC" in text
    assert "deploy batch prerequisites unavailable; queue left unchanged" in text


def test_batch_decision_persists_inclusion_before_superseding(monkeypatch, tmp_path):
    statements = []

    def capture(sql, *, capture=True):
        statements.append(sql)
        return ""

    monkeypatch.setattr(MODULE, "_psql", capture)
    representative = item(11, "b" * 40)
    included = [item(10, "a" * 40)]
    apply_decision(
        representative,
        included,
        "all_older_requests_are_low_risk_ancestors",
        ("AADS", "api", "production"),
    )

    sql = statements[-1]
    assert sql.index("INSERT INTO deploy_batch_inclusions") < sql.index("SET status='superseded'")
    assert "representative_run_id" in sql
    assert "included_run_id" in sql
    assert "WHERE id IN (10) AND status='queued'" in sql


def test_api_intake_preserves_requests_for_host_classification():
    text = (Path(__file__).parents[2] / "app/services/deploy_observability.py").read_text()
    assert "waiting_batch_predecessor" in text
    assert "pg_advisory_xact_lock" in text
    assert "superseded_by_newer_deploy_request" not in text


def test_direct_deploy_intake_preserves_requests_for_host_classification():
    text = (Path(__file__).parents[2] / "deploy.sh").read_text()
    block = text[text.index("queue_pending_deploy_request()"):text.index("wait_for_active_deploy_lock()")]
    assert "pg_advisory_xact_lock" in block
    assert "waiting_batch_predecessor" in block
    assert "INSERT INTO deploy_release_manifests" in block
    assert "superseded_by_newer_deploy" not in block
    assert "supersede_older_queued_deploy_requests" not in text


class _Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class _EnqueueConnection:
    def __init__(self, *, ready_exists: bool):
        self.ready_exists = ready_exists
        self.executions = []

    def transaction(self):
        return _Transaction()

    async def execute(self, query, *args):
        self.executions.append((query, args))
        return "OK"

    async def fetchval(self, query, *args):
        if "SELECT EXISTS" in query:
            return self.ready_exists
        if "MAX(queue_position)" in query:
            return 2
        raise AssertionError(query)

    async def fetchrow(self, query, *args):
        if "SELECT *" in query:
            return None
        if "INSERT INTO deploy_runs" in query:
            return {"id": 23, "release_sha": args[4], "phase": args[6]}
        raise AssertionError(query)


def test_enqueue_preserves_second_request_as_waiting_and_infers_risk_flags():
    conn = _EnqueueConnection(ready_exists=True)
    result = asyncio.run(OBSERVABILITY.enqueue_deploy_request(
        conn,
        project="AADS",
        release_sha="a" * 40,
        metadata={"changed_files": ["migrations/20260919_x.sql", "app/main.py"]},
    ))

    assert result["phase"] == "waiting_batch_predecessor"
    manifest_call = next(args for query, args in conn.executions if "INSERT INTO deploy_release_manifests" in query)
    assert json.loads(manifest_call[-1]) == ["migration"]
