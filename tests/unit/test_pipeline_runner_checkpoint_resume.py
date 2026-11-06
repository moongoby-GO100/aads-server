"""M3 러너 체크포인트 이어받기 — 재대기 후 재시작할 때 DB 체크포인트로 작업 폴더를 복원하고, 실패하면 origin/main 으로 폴백.

scripts/pipeline-runner.sh 의 CKPT(M2 저장)·CKPT-RESUME(M3)·prepare_clean_job_worktree 를 추출해 DB 함수만 대역으로 바꾼
bash 하네스(set -eo pipefail, 러너와 동일)에서 실제 임시 git 저장소로 돌린다. 대역은 psql -t -A 출력 형식(boolean 't'/'f',
필드 구분 0x1e)을 그대로 따른다. 실 PG 가 있으면(PG_TEST_DSN) 격리 스키마로 SQL 자체도 검증한다.
"""
import base64
import hashlib
import os
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import pytest

from tests.unit import ckpt_crypto_helper as cc

ROOT = Path(__file__).resolve().parents[2]
SH = ROOT / "scripts" / "pipeline-runner.sh"
LOCAL = ROOT / "scripts" / "pipeline-runner.sh.local"
FAKE_ID = "11111111-2222-3333-4444-555555555555"
MARK = "[RUNNER_SHUTDOWN_REQUEUE]"
RS = "\x1e"


def _extract(text: str) -> str:
    parts = []
    for pat in (
        r"^sql_escape\(\) \{.*?^\}\n",
        r"^resolve_job_base_sha\(\) \{.*?^\}\n",
        r"^JOB_DIFF_EXCLUDE_SPECS=.*$",
        r"^_receipt_json_str\(\) \{.*?^\}\n",
        r"^SHUTDOWN_REQUEUE_MARK=.*$",
        r"^# ── M2 체크포인트 저장 \(CKPT-BEGIN\).*?\(CKPT-END\)[^\n]*\n",
        r"^# ── M3 체크포인트 이어받기 \(CKPT-RESUME-BEGIN\).*?\(CKPT-RESUME-END\)[^\n]*\n",
        r"^prepare_clean_job_worktree\(\) \{.*?^\}\n",
    ):
        m = re.search(pat, text, re.M if pat.endswith("$") else re.S | re.M)
        assert m, pat
        parts.append(m.group(0))
    return "\n".join(parts)


HARNESS_HEAD = r"""
set -eo pipefail
log() { echo "$*" >> "$H/log.txt"; }
is_git_workdir() { git -C "$1" rev-parse --is-inside-work-tree >/dev/null 2>&1; }
worktree_busy_pids() { :; }
git_fetch_origin_with_retry() { git -C "$1" fetch -q origin; }
_reap_job_temp_pg() { :; }
_reap_job_test_containers() { :; }
git_dirty_count() { git -C "$1" status --porcelain | wc -l | tr -d ' '; }
_fail_job() { echo "$3" >> "$H/fail.log"; }
RUNNER_HOSTNAME=hostB
db_exec() {
    local sql="$1"
    printf '%s\n--\n' "$sql" >> "$H/sql.log"
    [[ "${MOCK_DB_FAIL:-}" == "all" ]] && return 1
    case "$sql" in
      *"to_regclass('public.pipeline_job_checkpoints')"*)
          [[ "${MOCK_TABLE:-yes}" == "yes" ]] && echo t || echo f ;;
      *"unnest(string_to_array"*)
          [[ "${MOCK_DB_FAIL:-}" == "mark" ]] && return 1
          cat "$H/mark_line" 2>/dev/null || true ;;
      *"COALESCE(instruction_hash,'')"*)
          printf 'ihash123\x1emodel-x\n' ;;
      *"INSERT INTO pipeline_job_checkpoints"*)
          printf '%s' "$sql" > "$H/insert.sql"; echo "$FAKE_ID" ;;
      *"FROM pipeline_job_checkpoints c JOIN pipeline_jobs p"*)
          [[ "${MOCK_DB_FAIL:-}" == "row" ]] && return 1
          cat "$H/row" 2>/dev/null || true ;;
      *"encode(patch,'base64')"*)
          [[ -n "${MOCK_ENC_SLEEP:-}" ]] && sleep "$MOCK_ENC_SLEEP"
          base64 -w76 "$H/stored_patch" ;;
      *"jsonb_array_elements_text"*)
          cat "$H/files_out" 2>/dev/null || true ;;
      *) echo "" ;;
    esac
}
record_runner_event() { echo "$2|${9-}" >> "$H/events.log"; }
"""


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8") if p.exists() else ""


def _git(repo, *a, check=True):
    return subprocess.run(["git", "-C", str(repo), *a], check=check, capture_output=True, text=True)


def _bash(h: Path, body: str, env=None, preamble="", timeout=120):
    script = f"H={h}\nFAKE_ID={FAKE_ID}\n{HARNESS_HEAD}\n{_extract(SH.read_text(encoding='utf-8'))}\n{preamble}\n{body}\n"
    (h / "run.sh").write_text(script, encoding="utf-8")
    return subprocess.run(["bash", str(h / "run.sh")], capture_output=True, text=True,
                          env={**os.environ, **cc.key_env(h.parent), **(env or {})}, timeout=timeout)


class World:
    """origin → 호스트 A(작업 폴더, M2 저장) → origin 이 1커밋 앞섬 → 호스트 B(다른 저장소 사본)."""

    def __init__(self, tmp_path: Path):
        self.tmp = tmp_path
        self.jid = "runner-resume" + uuid.uuid4().hex[:8]
        self.origin = tmp_path / "origin.git"
        seed = tmp_path / "seed"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.origin)], check=True)
        subprocess.run(["git", "clone", "-q", str(self.origin), str(seed)], check=True, capture_output=True)
        for k, v in (("user.email", "t@t"), ("user.name", "t")):
            _git(seed, "config", k, v)
        (seed / "a.txt").write_text("one\n")
        (seed / "bin.dat").write_bytes(bytes(range(256)))
        _git(seed, "add", "-A")
        _git(seed, "commit", "-q", "-m", "base")
        _git(seed, "push", "-q", "origin", "HEAD:main")
        self.base = _git(seed, "rev-parse", "HEAD").stdout.strip()

        # 호스트 A: /tmp/aads-wt-<jid> (M2 가 이 경로를 본다) 에서 추적 수정 + 미추적 신규 + 바이너리
        wt_a = Path(f"/tmp/aads-wt-{self.jid}")
        subprocess.run(["git", "clone", "-q", str(self.origin), str(wt_a)], check=True, capture_output=True)
        for k, v in (("user.email", "t@t"), ("user.name", "t")):
            _git(wt_a, "config", k, v)
        (wt_a / "a.txt").write_text("one\ntwo\n")
        (wt_a / "new.txt").write_text("brand new\n")
        self.new_bin = bytes(reversed(range(256))) * 3
        (wt_a / "new.bin").write_bytes(self.new_bin)
        (wt_a / "bin.dat").write_bytes(bytes(range(255, -1, -1)))
        # 저장 함수는 바이너리 patch 를 거부한다(secret_unscannable). 이 테스트는 "이어받기(적용)" 쪽이 대상이므로
        # 저장기가 만들던 것과 같은 patch(임시 index + add -A + diff --cached --binary --no-renames)를 git 으로 직접 만든다.
        idx = tmp_path / "ckpt.index"
        env = {**os.environ, "GIT_INDEX_FILE": str(idx)}
        subprocess.run(["git", "-C", str(wt_a), "add", "--all", "--", "."], check=True, capture_output=True, env=env)
        self.patch = subprocess.run(["git", "-C", str(wt_a), "diff", "--cached", "--binary", "--no-renames", self.base],
                                    check=True, capture_output=True, env=env).stdout
        self.sha = hashlib.sha256(self.patch).hexdigest()
        self.files = ["a.txt", "bin.dat", "new.bin", "new.txt"]
        shutil.rmtree(wt_a, ignore_errors=True)

        # 호스트 B 는 origin 이 앞서기 전에 clone 한다(prepare 의 fetch 가 새 커밋을 가져온다).
        self.main_b = tmp_path / "mainB"
        subprocess.run(["git", "clone", "-q", str(self.origin), str(self.main_b)], check=True, capture_output=True)
        (seed / "other.txt").write_text("later\n")
        _git(seed, "add", "-A")
        _git(seed, "commit", "-q", "-m", "ahead")
        _git(seed, "push", "-q", "origin", "HEAD:main")
        self.head_new = _git(seed, "rev-parse", "HEAD").stdout.strip()
        assert self.head_new != self.base
        self.wt = tmp_path / "wtB"

    def stored(self, patch=None) -> bytes:
        """DB patch 열에 들어 있는 암호문(v1). patch 를 생략하면 World 의 정상 patch."""
        return cc.encrypt(cc.make_key_file(self.tmp), self.jid, self.base, self.patch if patch is None else patch)

    def run(self, *, mark=True, row=None, patch=None, raw_stored=None, files=None, env=None, body_extra=""):
        h = self.tmp / f"hB{uuid.uuid4().hex[:6]}"
        h.mkdir()
        if mark is True:
            (h / "mark_line").write_text(f"{MARK} checkpoint={FAKE_ID} 러너 종료로 중단되어 자동 재큐잉")
        elif mark:
            (h / "mark_line").write_text(mark)
        fields = dict(base=self.base, sha=self.sha, bytes=len(self.patch), host="hostA",
                      nfiles=len(self.files), age_ok="t", hash_ok="t")
        fields.update(row if isinstance(row, dict) else {})
        if row != "none":
            (h / "row").write_text(RS.join(str(fields[k]) for k in
                                           ("base", "sha", "bytes", "host", "nfiles", "age_ok", "hash_ok")) + "\n")
        (h / "stored_patch").write_bytes(self.stored(patch) if raw_stored is None else raw_stored)
        (h / "files_out").write_text("\n".join(self.files if files is None else files) + "\n")
        body = (f'resume_brief=""\n'
                f'prepare_clean_job_worktree {self.jid} AADS sid "{self.main_b}" "{self.wt}" resume_brief && echo RC=0 || echo RC=$?\n'
                f'printf "BRIEF_BEGIN\\n%s\\nBRIEF_END\\n" "$resume_brief"\n{body_extra}')
        r = _bash(h, body, env=env)
        return r, h


@pytest.fixture
def world(tmp_path):
    w = World(tmp_path)
    yield w
    shutil.rmtree(f"/tmp/aads-wt-{w.jid}", ignore_errors=True)


def _brief(r) -> str:
    return re.search(r"BRIEF_BEGIN\n(.*)\nBRIEF_END", r.stdout, re.S).group(1)


def test_resume_from_other_host_applies_patch(world):
    r, h = world.run()
    assert r.returncode == 0 and "RC=0" in r.stdout, r.stdout + r.stderr
    wt = world.wt
    assert not _read(h / "fail.log")
    # 파일 내용 일치(추적 수정·미추적 신규·바이너리)
    assert (wt / "a.txt").read_text() == "one\ntwo\n"
    assert (wt / "new.txt").read_text() == "brand new\n"
    assert (wt / "new.bin").read_bytes() == world.new_bin
    assert (wt / "bin.dat").read_bytes() == bytes(range(255, -1, -1))
    # base 는 base_sha(origin/main 은 1커밋 앞섬)이고 index 에 변경이 있다
    assert _git(wt, "rev-parse", "HEAD").stdout.strip() == world.base
    assert _git(world.main_b, "rev-parse", "origin/main").stdout.strip() == world.head_new
    assert not (wt / "other.txt").exists()
    staged = sorted(_git(wt, "diff", "--cached", "--name-only").stdout.split())
    assert staged == world.files
    assert _git(wt, "diff", "--quiet", check=False).returncode == 0  # 작업 트리에만 남은 변경 없음
    log = _read(h / "log.txt")
    assert f"WORKTREE_RESUMED: {wt} base={world.base} checkpoint={FAKE_ID}" in log
    assert "WORKTREE_CLEAN" not in log
    ev = _read(h / "events.log")
    assert "job_resumed_from_checkpoint" in ev and f'"checkpoint_id":"{FAKE_ID}"' in ev
    assert f'"files":{len(world.files)}' in ev and f'"bytes":{len(world.patch)}' in ev and '"from_host":"hostA"' in ev
    assert "checkpoint_resume_skipped" not in ev
    assert "c.host" not in _read(h / "sql.log").replace("c.host, jsonb_array_length", "")  # 호스트로 거르지 않는다


def test_resume_brief_lists_files_without_diff(world):
    r, _ = world.run()
    b = _brief(r)
    assert "이전 시도에서 아래 파일을 이미 수정했다. 이어서 완료하라." in b
    for f in world.files:
        assert f"- {f}" in b
    assert "diff --git" not in b and "brand new" not in b


def test_resume_brief_caps_files_at_50(world):
    many = [f"dir/f{i:03d}.txt" for i in range(50)]
    r, _ = world.run(row={"nfiles": 60}, files=many)
    b = _brief(r)
    assert b.count("\n- dir/") == 50 and "- (외 10개)" in b


@pytest.mark.parametrize("mark", ["", f"{MARK} 러너 종료로 중단되어 자동 재큐잉"])
def test_no_checkpoint_marker_is_silent(world, mark):
    r, h = world.run(mark=mark or False)
    _assert_fallback(world, r, h, reason=None)
    assert "to_regclass" not in _read(h / "sql.log")  # 표시가 없으면 체크포인트 테이블 조회도 하지 않는다


def _assert_fallback(world, r, h, reason):
    assert r.returncode == 0, r.stdout + r.stderr
    assert "RC=0" in r.stdout and not _read(h / "fail.log"), r.stdout
    wt = world.wt
    assert _git(wt, "rev-parse", "HEAD").stdout.strip() == world.head_new
    assert _git(wt, "status", "--porcelain").stdout == ""
    assert not (wt / "new.txt").exists()
    assert _brief(r) == ""
    log = _read(h / "log.txt")
    assert f"WORKTREE_CLEAN: {wt} base=origin/main" in log and "WORKTREE_RESUMED" not in log
    ev = _read(h / "events.log")
    assert "job_resumed_from_checkpoint" not in ev
    if reason is None:
        assert ev == ""
    else:
        assert "checkpoint_resume_skipped" in ev and f'"reason":"{reason}"' in ev, ev
    # 실패한 이어받기가 남긴 worktree 등록이 없다(메인 + 새 폴더 둘뿐)
    assert len(_git(world.main_b, "worktree", "list").stdout.strip().splitlines()) == 2


def test_fallback_none_row_missing(world):
    r, h = world.run(row="none")
    _assert_fallback(world, r, h, "none")


def test_fallback_expired(world):
    r, h = world.run(row={"age_ok": "f"})
    _assert_fallback(world, r, h, "expired")


def test_fallback_hash_mismatch(world):
    r, h = world.run(row={"hash_ok": "f"})
    _assert_fallback(world, r, h, "hash_mismatch")


def test_fallback_sha_mismatch(world):
    r, h = world.run(patch=world.patch + b"\n")
    _assert_fallback(world, r, h, "sha_mismatch")


def test_fallback_apply_failed(world):
    bad = b"this is not a patch\n"
    r, h = world.run(patch=bad, row={"sha": hashlib.sha256(bad).hexdigest(), "bytes": len(bad)})
    _assert_fallback(world, r, h, "apply_failed")


def test_fallback_apply_failed_on_conflicting_context(world, tmp_path):
    """base 에 없는 내용을 기대하는 정상 형식 patch 는 git apply 가 거부한다."""
    bad = (b"diff --git a/a.txt b/a.txt\n--- a/a.txt\n+++ b/a.txt\n@@ -1 +1,2 @@\n-zzz\n+one\n+two\n")
    r, h = world.run(patch=bad, row={"sha": hashlib.sha256(bad).hexdigest(), "bytes": len(bad)})
    _assert_fallback(world, r, h, "apply_failed")


def test_fallback_base_missing(world):
    r, h = world.run(row={"base": "deadbeef" * 5})
    _assert_fallback(world, r, h, "base_missing")


def test_fallback_table_missing(world):
    r, h = world.run(env={"MOCK_TABLE": "no"})
    _assert_fallback(world, r, h, "table_missing")


@pytest.mark.parametrize("where", ["row", "mark"])
def test_fallback_db_error(world, where):
    r, h = world.run(env={"MOCK_DB_FAIL": where})
    _assert_fallback(world, r, h, "db_error")


def test_fallback_timeout(world):
    t0 = time.monotonic()
    r, h = world.run(env={"MOCK_ENC_SLEEP": "6", "CHECKPOINT_RESUME_TIMEOUT": "1"})
    assert time.monotonic() - t0 < 5, "시간 상한 안에 폴백해야 한다"
    _assert_fallback(world, r, h, "timeout")


def test_resume_timeout_does_not_consume_save_budget(world):
    r, _ = world.run(env={"MOCK_ENC_SLEEP": "6", "CHECKPOINT_RESUME_TIMEOUT": "1"}, body_extra='echo "SPENT=$_CKPT_SPENT_MS"')
    assert "SPENT=0" in r.stdout


def test_requeue_count_logic_untouched():
    """재대기 한도 계산(마커 개수)은 그대로다."""
    text = SH.read_text(encoding="utf-8")
    assert "replace(COALESCE(review_feedback,''), $(sql_escape \"$SHUTDOWN_REQUEUE_MARK\"), '')" in text
    assert "${#SHUTDOWN_REQUEUE_MARK}" in text


def test_instruction_gets_resume_brief_before_original():
    text = SH.read_text(encoding="utf-8")
    assert '"$worktree_dir" resume_brief || {' in text
    assert "    local resume_brief=\"\"\n" in text
    assert '${resume_brief}${safe_instruction}"' in text
    # 이어받지 않으면 resume_brief 가 비어 지시에 아무것도 붙지 않는다
    assert "--resume" not in text.split("CKPT-RESUME-BEGIN")[1].split("CKPT-RESUME-END")[0]


def test_sh_and_local_identical_and_syntax():
    assert SH.read_bytes() == LOCAL.read_bytes()
    for p in (SH, LOCAL):
        assert subprocess.run(["bash", "-n", str(p)]).returncode == 0


def test_real_pg_queries_in_isolated_schema(world):
    """실 PG 가 있으면 격리 스키마에서 이어받기 SQL 3종(표시 줄·행 조회·patch 조회)을 검증한다."""
    dsn = os.environ.get("PG_TEST_DSN")
    if not dsn or not shutil.which("psql"):
        pytest.skip("PG_TEST_DSN/psql 없음 — 실 PG SQL 검증 생략(대역은 psql -t -A 형식을 따른다)")
    schema = "ckpt_resume_" + uuid.uuid4().hex[:8]

    def psql(sql):
        return subprocess.run(["psql", dsn, "-v", "ON_ERROR_STOP=1", "-q", "-t", "-A", "-c", sql],
                              capture_output=True, text=True, check=True).stdout

    b64 = base64.b64encode(world.stored()).decode()
    try:
        psql(f"CREATE SCHEMA {schema}")
        psql(f"""CREATE TABLE {schema}.pipeline_jobs (job_id text, review_feedback text, instruction_hash text);
            CREATE TABLE {schema}.pipeline_job_checkpoints (id uuid PRIMARY KEY, job_id text NOT NULL, base_sha text NOT NULL,
              patch bytea NOT NULL, patch_sha256 text NOT NULL, files jsonb NOT NULL, bytes int NOT NULL, model text, attempt int,
              host text NOT NULL, instruction_hash text NOT NULL, created_at timestamptz NOT NULL DEFAULT now());
            INSERT INTO {schema}.pipeline_jobs VALUES ('{world.jid}', E'x\\n{MARK} 이전\\n{MARK} checkpoint={FAKE_ID} 러너', 'h1');
            INSERT INTO {schema}.pipeline_job_checkpoints (id, job_id, base_sha, patch, patch_sha256, files, bytes, host, instruction_hash)
              VALUES ('{FAKE_ID}', '{world.jid}', '{world.base}', decode('{b64}','base64'), '{world.sha}',
                      '{str(world.files).replace(chr(39), chr(34))}'::jsonb, {len(world.patch)}, 'hostA', 'h1');""")
        # to_regclass('public.…') 는 격리 스키마를 보지 않으므로 public 을 격리 스키마로 바꿔 돌린다.
        pre = ("db_exec() { local s=\"${1//public./" + schema + ".}\"; printf '%s' \"$s\" | PGOPTIONS='-c search_path=" + schema +
               "' psql \"$PG_TEST_DSN\" -q -t -A -P footer=off -F $'\\x1e'; }")
        h = world.tmp / "hpg"
        h.mkdir()
        (h / "files_out").write_text("")
        body = (f'resume_brief=""\nprepare_clean_job_worktree {world.jid} AADS sid "{world.main_b}" "{world.wt}" resume_brief && echo RC=0\n'
                'printf "BRIEF_BEGIN\\n%s\\nBRIEF_END\\n" "$resume_brief"')
        r = _bash(h, body, env={"PG_TEST_DSN": dsn}, preamble=pre)
        assert "RC=0" in r.stdout, r.stdout + r.stderr
        assert f"WORKTREE_RESUMED: {world.wt} base={world.base} checkpoint={FAKE_ID}" in _read(h / "log.txt")
        assert (world.wt / "new.txt").read_text() == "brand new\n"
        for f in world.files:
            assert f"- {f}" in _brief(r)
        # 지시서가 바뀌면(instruction_hash 불일치) 폴백
        psql(f"UPDATE {schema}.pipeline_jobs SET instruction_hash='h2'")
        shutil.rmtree(world.wt, ignore_errors=True)
        _git(world.main_b, "worktree", "prune")
        h2 = world.tmp / "hpg2"
        h2.mkdir()
        r = _bash(h2, body, env={"PG_TEST_DSN": dsn}, preamble=pre)
        assert '"reason":"hash_mismatch"' in _read(h2 / "events.log"), r.stdout + r.stderr
        assert _git(world.wt, "rev-parse", "HEAD").stdout.strip() == world.head_new
    finally:
        psql(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
