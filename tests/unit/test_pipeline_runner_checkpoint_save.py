"""M2 러너 체크포인트 저장 — 러너 종료로 재대기할 때 작업 폴더 진행분을 DB에 저장.

scripts/pipeline-runner.sh 의 CKPT 블록과 _shutdown_finalize_job 을 추출해, DB 함수만 대역으로
바꾼 bash 하네스에서 실제 임시 git 저장소로 돌린다. db_exec 대역은 실제 psql -t -A 출력 형식
(boolean 은 't'/'f')을 그대로 흉내 낸다.
"""
import base64
import hashlib
import os
import re
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

from tests.unit import ckpt_crypto_helper as cc

ROOT = Path(__file__).resolve().parents[2]
SH = ROOT / "scripts" / "pipeline-runner.sh"
LOCAL = ROOT / "scripts" / "pipeline-runner.sh.local"
FAKE_ID = "11111111-2222-3333-4444-555555555555"


def _extract(text: str) -> str:
    parts = []
    m = re.search(r"^sql_escape\(\) \{.*?^\}\n", text, re.S | re.M)
    parts.append(m.group(0))
    m = re.search(r"^resolve_job_base_sha\(\) \{.*?^\}\n", text, re.S | re.M)
    parts.append(m.group(0))
    m = re.search(r"^JOB_DIFF_EXCLUDE_SPECS=.*$", text, re.M)
    parts.append(m.group(0))
    m = re.search(r"^# ── M2 체크포인트 저장 \(CKPT-BEGIN\).*?\(CKPT-END\)[^\n]*\n", text, re.S | re.M)
    parts.append(m.group(0))
    m = re.search(r"^SHUTDOWN_REQUEUE_MARK=.*$", text, re.M)
    parts.append(m.group(0))
    m = re.search(r"^SHUTDOWN_REQUEUE_MAX=.*$", text, re.M)
    parts.append(m.group(0))
    m = re.search(r"^_shutdown_finalize_job\(\) \{.*?^\}\n", text, re.S | re.M)
    parts.append(m.group(0))
    return "\n".join(parts)


HARNESS_HEAD = r"""
set -u
log() { :; }
post_to_chat() { :; }
_notify_ai() { :; }
_reap_job_test_containers() { :; }
RUNNER_HOSTNAME=testhost
DEPLOY_LOCK_WAIT_PHASE=deploy_lock_wait
# 대역: SQL 은 첫 인자. 실제 psql -t -A 출력 형식을 흉내 낸다.
db_exec() {
    local sql="$1"
    printf '%s' "$sql" >> "$H/sql.log"; printf '\n--\n' >> "$H/sql.log"
    case "$sql" in
      *"to_regclass('public.pipeline_job_checkpoints')"*)
          [[ "${MOCK_TABLE:-yes}" == "yes" ]] && echo "${MOCK_BOOL_TRUE:-t}" || echo f ;;
      *"FROM pipeline_jobs WHERE job_id"*"instruction_hash"*|*"instruction_hash,''"*)
          printf 'ihash123\x1emodel-x\n' ;;
      *"INSERT INTO pipeline_job_checkpoints"*)
          [[ "${MOCK_INSERT:-ok}" == "ok" ]] || return 1
          printf '%s' "$sql" > "$H/insert.sql"; echo "$FAKE_ID" ;;
      *"review_feedback"*) echo "${MOCK_MARKS:-0}" ;;
      *) echo "" ;;
    esac
}
db_update() { printf '%s\n--\n' "$1" >> "$H/update.log"; }
record_runner_event() {
    [[ -n "${MOCK_EVENT_SLEEP:-}" ]] && sleep "$MOCK_EVENT_SLEEP"
    echo "$2|${9-}" >> "$H/events.log"
}
"""


def _run(tmp_path, body, env=None, preamble=""):
    text = SH.read_text(encoding="utf-8")
    h = tmp_path / "h"
    h.mkdir(exist_ok=True)
    script = f"H={h}\nFAKE_ID={FAKE_ID}\n{HARNESS_HEAD}\n{_extract(text)}\n{preamble}\n{body}\n"
    (h / "run.sh").write_text(script, encoding="utf-8")
    e = {**os.environ, **cc.key_env(tmp_path), **(env or {})}
    r = subprocess.run(["bash", str(h / "run.sh")], capture_output=True, text=True, env=e, timeout=120)
    return r, h


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8") if p.exists() else ""


@pytest.fixture
def job(tmp_path):
    """/tmp/aads-wt-<jid> 에 base 커밋이 있는 작업 폴더를 만든다."""
    jid = "runner-ckpt" + uuid.uuid4().hex[:8]
    wt = Path(f"/tmp/aads-wt-{jid}")
    g = lambda *a: subprocess.run(["git", "-C", str(wt), *a], check=True, capture_output=True, text=True)  # noqa: E731
    wt.mkdir()
    g("init", "-q")
    g("config", "user.email", "t@t")
    g("config", "user.name", "t")
    (wt / "a.txt").write_text("one\n")
    g("add", "-A")
    g("commit", "-q", "-m", "base")
    base = g("rev-parse", "HEAD").stdout.strip()
    g("update-ref", "refs/remotes/origin/main", base)
    yield jid, wt, base, g
    shutil.rmtree(wt, ignore_errors=True)


def _dirty(wt):
    (wt / "a.txt").write_text("one\ntwo\n")
    (wt / "new.txt").write_text("brand new\n")
    (wt / ".runner_full_diff.patch").write_text("artifact\n")
    (wt / "RESULT-x.md").write_text("result\n")


def _insert_patch(h, tmp_path, jid, base):
    """INSERT 의 patch 열은 암호문(v1)이다. 시험용 독립 구현으로 풀어 평문 patch 를 돌려준다."""
    sql = _read(h / "insert.sql")
    m = re.search(r"decode\('([A-Za-z0-9+/=]+)','base64'\)", sql)
    blob = base64.b64decode(m.group(1))
    assert blob.startswith(b"v1:")
    return cc.decrypt(cc.make_key_file(tmp_path), jid, base, blob), sql


def test_save_apply_sha_and_exclusions(tmp_path, job):
    jid, wt, base, g = job
    _dirty(wt)
    index_before = g("diff", "--cached", "--name-only").stdout
    r, h = _run(tmp_path, f'_shutdown_save_checkpoint {jid} 2; echo "ID=$_CKPT_LAST_ID"')
    assert r.returncode == 0, r.stderr
    assert f"ID={FAKE_ID}" in r.stdout
    patch, sql = _insert_patch(h, tmp_path, jid, base)
    assert f"'{base}'" in sql and hashlib.sha256(patch).hexdigest() in sql
    assert "$esc$ihash123$esc$" in sql and "$esc$testhost$esc$" in sql and "model-x" in sql
    assert re.search(r",\s*2,\s*\$esc\$testhost", sql)  # attempt=2
    text = patch.decode()
    assert "new.txt" in text and "a.txt" in text
    assert ".runner_full_diff.patch" not in text and "RESULT-x.md" not in text
    # 실제 작업 폴더의 index 는 바뀌지 않는다
    assert g("diff", "--cached", "--name-only").stdout == index_before
    # 다른 폴더에서 base_sha 체크아웃 후 적용
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", str(wt), str(other)], check=True)
    subprocess.run(["git", "-C", str(other), "checkout", "-q", base], check=True)
    pf = tmp_path / "ck.patch"
    pf.write_bytes(patch)
    ap = subprocess.run(["git", "-C", str(other), "apply", "--index", str(pf)], capture_output=True, text=True)
    assert ap.returncode == 0, ap.stderr
    assert (other / "new.txt").read_text() == "brand new\n"
    ev = _read(h / "events.log")
    assert "checkpoint_saved" in ev and FAKE_ID in ev and '"files":2' in ev


@pytest.mark.parametrize("boolean_text", ["t", "true"])
def test_boolean_t_is_read_as_table_exists(tmp_path, job, boolean_text):
    """P1-1 회귀: psql -t -A 의 't' 와 ::text 캐스트의 'true' 둘 다 참. 'true|running' 형태에 속지 않는다."""
    r, _ = _run(tmp_path, '_ckpt_is_true t && _ckpt_is_true true && ! _ckpt_is_true f && ! _ckpt_is_true "true|running" && echo OK')
    assert "OK" in r.stdout
    jid, wt, base, g = job
    _dirty(wt)
    r, h = _run(tmp_path, f'_shutdown_save_checkpoint {jid} 1; echo "ID=$_CKPT_LAST_ID"', env={"MOCK_BOOL_TRUE": boolean_text})
    assert f"ID={FAKE_ID}" in r.stdout
    assert "table_missing" not in _read(h / "events.log")


def test_no_changes_not_saved(tmp_path, job):
    jid, *_ = job
    r, h = _run(tmp_path, f'_shutdown_save_checkpoint {jid} 1; echo "ID=[$_CKPT_LAST_ID]"')
    assert "ID=[]" in r.stdout
    assert not (h / "insert.sql").exists()
    assert "checkpoint_skipped" in _read(h / "events.log") and "no_changes" in _read(h / "events.log")


def test_too_large_not_saved(tmp_path, job):
    jid, wt, *_ = job
    _dirty(wt)
    r, h = _run(tmp_path, f'_shutdown_save_checkpoint {jid} 1; echo "ID=[$_CKPT_LAST_ID]"', env={"CHECKPOINT_MAX_BYTES": "50"})
    assert "ID=[]" in r.stdout and not (h / "insert.sql").exists()
    assert "too_large" in _read(h / "events.log")


def test_table_missing_skips_but_requeues(tmp_path, job):
    jid, wt, *_ = job
    _dirty(wt)
    r, h = _run(tmp_path, f'_shutdown_finalize_job {jid} sid quiet', env={"MOCK_TABLE": "no"})
    assert r.returncode == 0, r.stderr
    assert not (h / "insert.sql").exists()
    up = _read(h / "update.log")
    assert "status='queued'" in up and "[RUNNER_SHUTDOWN_REQUEUE] 러너" in up and "checkpoint=" not in up
    assert "table_missing" in _read(h / "events.log")


def test_db_failure_still_requeues(tmp_path, job):
    jid, wt, *_ = job
    _dirty(wt)
    r, h = _run(tmp_path, f'_shutdown_finalize_job {jid} sid quiet', env={"MOCK_INSERT": "fail"})
    assert r.returncode == 0, r.stderr
    up = _read(h / "update.log")
    assert "status='queued'" in up and "checkpoint=" not in up
    assert "db_error" in _read(h / "events.log")


def test_timeout_still_requeues(tmp_path, job):
    jid, wt, *_ = job
    _dirty(wt)
    pre = '_ckpt_build_and_store() { sleep 2; }'  # 저장 단계만 지연
    r, h = _run(tmp_path, f'_shutdown_finalize_job {jid} sid quiet', env={"CHECKPOINT_SAVE_TIMEOUT": "1"}, preamble=pre)
    assert r.returncode == 0, r.stderr
    assert "status='queued'" in _read(h / "update.log")
    assert "checkpoint=" not in _read(h / "update.log")


def test_success_marks_requeue_with_checkpoint_id(tmp_path, job):
    jid, wt, *_ = job
    _dirty(wt)
    r, h = _run(tmp_path, f'_shutdown_finalize_job {jid} sid quiet', env={"MOCK_MARKS": "1"})
    up = _read(h / "update.log")
    assert f"[RUNNER_SHUTDOWN_REQUEUE] checkpoint={FAKE_ID} 러너 종료로" in up
    assert re.search(r",\s*2,\s*\$esc\$testhost", _read(h / "insert.sql"))  # attempt = 마커 1 + 1


def test_limit_exceeded_does_not_save(tmp_path, job):
    jid, wt, *_ = job
    _dirty(wt)
    r, h = _run(tmp_path, f'_shutdown_finalize_job {jid} sid quiet', env={"MOCK_MARKS": "2"})
    assert not (h / "insert.sql").exists()
    up = _read(h / "update.log")
    assert "SET status='error'" in up and "SET status='queued'" not in up
    assert "job_terminal" in _read(h / "events.log")


def test_total_budget_exhausted_skips_later_jobs(tmp_path, job):
    jid, wt, *_ = job
    _dirty(wt)
    body = f"_CKPT_SPENT_MS=20000\n_shutdown_finalize_job {jid} sid quiet"
    r, h = _run(tmp_path, body)
    assert not (h / "insert.sql").exists()
    assert "status='queued'" in _read(h / "update.log")
    assert "sql.log" and "to_regclass" not in _read(h / "sql.log")


def test_event_write_is_bounded_by_budget(tmp_path, job):
    """P1-2 회귀: 이벤트 기록이 2초 지연돼도 예산(0초 남음)을 넘겨 기다리지 않고, 쓴 시간이 집계된다."""
    jid, wt, *_ = job
    _dirty(wt)
    body = (
        "_CKPT_SPENT_MS=19900\n"
        f"t0=$(date +%s%N); _shutdown_save_checkpoint {jid} 1; t1=$(date +%s%N)\n"
        'echo "ELAPSED_MS=$(( (t1 - t0) / 1000000 ))"; echo "SPENT=$_CKPT_SPENT_MS"'
    )
    r, h = _run(tmp_path, body, env={"MOCK_EVENT_SLEEP": "5", "CHECKPOINT_EVENT_TIMEOUT": "5"})
    elapsed = int(re.search(r"ELAPSED_MS=(\d+)", r.stdout).group(1))
    spent = int(re.search(r"SPENT=(\d+)", r.stdout).group(1))
    assert elapsed < 600, r.stdout
    assert spent >= 20000 - 100 or spent >= 19900 + elapsed - 200, r.stdout
    assert _read(h / "events.log") == ""  # 5초 걸리는 기록은 끊겼다


def test_event_bound_counts_toward_total(tmp_path, job):
    jid, wt, *_ = job
    _dirty(wt)
    body = (
        f"_shutdown_save_checkpoint {jid} 1; echo \"SPENT=$_CKPT_SPENT_MS\""
    )
    r, h = _run(tmp_path, body, env={"MOCK_EVENT_SLEEP": "5", "CHECKPOINT_EVENT_TIMEOUT": "1"})
    spent = int(re.search(r"SPENT=(\d+)", r.stdout).group(1))
    assert 1000 <= spent < 4000, r.stdout  # 이벤트가 끊긴 시간(~1초)까지 포함


def test_marker_with_checkpoint_keeps_count(tmp_path):
    """재대기 한도 계산(마커 개수 세기)이 ' checkpoint=<id>' 추가에도 같다."""
    text = SH.read_text(encoding="utf-8")
    mark = "[RUNNER_SHUTDOWN_REQUEUE]"
    plain = f"x\n{mark} 러너 종료로 중단되어 자동 재큐잉"
    withid = f"x\n{mark} checkpoint={FAKE_ID} 러너 종료로 중단되어 자동 재큐잉"
    for s in (plain, withid):
        assert (len(s) - len(s.replace(mark, ""))) // len(mark) == 1
    # SQL 이 마커 문자열만 센다(뒤따르는 텍스트에 의존하지 않음)
    assert "replace(COALESCE(review_feedback,'')" in text and "${#SHUTDOWN_REQUEUE_MARK}" in text


def test_real_pg_boolean_text_format():
    """실 PG 가 있으면 psql -t -A 의 boolean 출력이 t 인지 확인한다."""
    dsn = os.environ.get("PG_TEST_DSN")
    if not dsn or not shutil.which("psql"):
        pytest.skip("PG_TEST_DSN/psql 없음 — 실 PG 형식 검사 생략(대역은 psql 형식 't' 사용)")
    out = subprocess.run(["psql", dsn, "-q", "-t", "-A", "-c", "SELECT to_regclass('pg_class') IS NOT NULL;"],
                         capture_output=True, text=True).stdout.strip()
    assert out == "t"


def test_sh_and_local_identical_and_syntax():
    assert SH.read_bytes() == LOCAL.read_bytes()
    for p in (SH, LOCAL):
        assert subprocess.run(["bash", "-n", str(p)]).returncode == 0


def test_zero_budget_does_not_call_slow_checkpoint_event(tmp_path):
    r, h = _run(tmp_path, '\n'.join([
        't0=$(date +%s%N)',
        '_shutdown_save_checkpoint test 1',
        "_ckpt_record_event test checkpoint_skipped '{}'",
        'echo "ELAPSED_MS=$(( ($(date +%s%N) - t0) / 1000000 ))"',
    ]), env={"CHECKPOINT_SAVE_TOTAL_BUDGET": "0", "MOCK_EVENT_SLEEP": "2"})
    assert r.returncode == 0, r.stderr
    assert int(re.search(r"ELAPSED_MS=(\d+)", r.stdout).group(1)) < 500
    assert not (h / "events.log").exists()
