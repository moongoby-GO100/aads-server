"""러너 체크포인트 저장 전 시크릿 검사 — 걸리거나 검사할 수 없으면 저장하지 않는다(INSERT 호출 없음).

scripts/pipeline-runner.sh 의 CKPT 블록을 추출해 DB 함수만 대역으로 바꾼 bash 하네스에서 실제 임시 git
저장소로 돌린다(test_pipeline_runner_checkpoint_save.py 와 같은 방식). 운영 러너처럼 set -eo pipefail 로 돌린다.
가짜 키는 pre-commit 에 걸리지 않게 런타임에 문자열을 조립한다.
"""
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from tests.unit import ckpt_crypto_helper as cc

ROOT = Path(__file__).resolve().parents[2]
SH = ROOT / "scripts" / "pipeline-runner.sh"
LOCAL = ROOT / "scripts" / "pipeline-runner.sh.local"
FAKE_ID = "11111111-2222-3333-4444-555555555555"

AWS = "AKIA" + "ABCDEFGHIJKLMNOP"
OAT = "sk-ant-" + "oat01-" + "abcdEFGH1234abcdEFGH1234wxyz"
PEM_HEAD = "-----BEGIN " + "RSA PRIVATE" + " KEY-----"
CKPT_BLOCK = r"^# ── M2 체크포인트 저장 \(CKPT-BEGIN\).*?\(CKPT-END\)[^\n]*\n"
NO_GITLEAKS = {"CHECKPOINT_GITLEAKS_BIN": "gitleaks-not-installed-for-test"}


def _extract(text: str) -> str:
    parts = []
    for pat in (
        r"^sql_escape\(\) \{.*?^\}\n",
        r"^resolve_job_base_sha\(\) \{.*?^\}\n",
        r"^JOB_DIFF_EXCLUDE_SPECS=.*$",
        CKPT_BLOCK,
        r"^SHUTDOWN_REQUEUE_MARK=.*$",
        r"^SHUTDOWN_REQUEUE_MAX=.*$",
        r"^_shutdown_finalize_job\(\) \{.*?^\}\n",
    ):
        flags = re.M | (re.S if ".*?" in pat else 0)
        parts.append(re.search(pat, text, flags).group(0))
    return "\n".join(parts)


HARNESS_HEAD = r"""
set -u
log() { :; }
post_to_chat() { :; }
_notify_ai() { :; }
_reap_job_test_containers() { :; }
RUNNER_HOSTNAME=testhost
DEPLOY_LOCK_WAIT_PHASE=deploy_lock_wait
db_exec() {
    local sql="$1"
    printf '%s' "$sql" >> "$H/sql.log"; printf '\n--\n' >> "$H/sql.log"
    case "$sql" in
      *"to_regclass('public.pipeline_job_checkpoints')"*) echo t ;;
      *"FROM pipeline_jobs WHERE job_id"*"instruction_hash"*|*"instruction_hash,''"*)
          printf 'ihash123\x1emodel-x\n' ;;
      *"INSERT INTO pipeline_job_checkpoints"*)
          printf '%s' "$sql" > "$H/insert.sql"; echo "$FAKE_ID" ;;
      *"review_feedback"*) echo 0 ;;
      *) echo "" ;;
    esac
}
db_update() { printf '%s\n--\n' "$1" >> "$H/update.log"; }
record_runner_event() { echo "$2|${9-}" >> "$H/events.log"; }
set -eo pipefail
"""


def _run(tmp_path, body, env=None, text=None):
    text = text if text is not None else SH.read_text(encoding="utf-8")
    h = tmp_path / "h"
    h.mkdir(exist_ok=True)
    script = f"H={h}\nFAKE_ID={FAKE_ID}\n{HARNESS_HEAD}\n{_extract(text)}\n{body}\n"
    (h / "run.sh").write_text(script, encoding="utf-8")
    tdir = tmp_path / "tmp"
    tdir.mkdir(exist_ok=True)
    e = {**os.environ, "TMPDIR": str(tdir), **cc.key_env(tmp_path), **(env or {})}
    r = subprocess.run(["bash", str(h / "run.sh")], capture_output=True, text=True, env=e, timeout=120)
    return r, h


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8") if p.exists() else ""


def _shim(tmp_path, name, body) -> Path:
    d = tmp_path / "shim"
    d.mkdir(exist_ok=True)
    f = d / name
    f.write_text("#!/bin/bash\n" + body)
    f.chmod(f.stat().st_mode | stat.S_IEXEC)
    return d


@pytest.fixture
def job(tmp_path):
    jid = "runner-guard" + uuid.uuid4().hex[:8]
    wt = Path(f"/tmp/aads-wt-{jid}")
    g = lambda *a: subprocess.run(["git", "-C", str(wt), *a], check=True, capture_output=True, text=True)  # noqa: E731
    wt.mkdir()
    g("init", "-q")
    g("config", "user.email", "t@t")
    g("config", "user.name", "t")

    def commit_base():
        g("add", "-A")
        g("commit", "-q", "-m", "base")
        base = g("rev-parse", "HEAD").stdout.strip()
        g("update-ref", "refs/remotes/origin/main", base)
        return base

    yield jid, wt, commit_base, g
    shutil.rmtree(wt, ignore_errors=True)


def _save(tmp_path, jid, env=None):
    return _run(tmp_path, f'_shutdown_save_checkpoint {jid} 1; echo "ID=[$_CKPT_LAST_ID]"', env={**NO_GITLEAKS, **(env or {})})


def _assert_blocked(tmp_path, jid, reason, secrets=(), env=None):
    r, h = _save(tmp_path, jid, env)
    assert r.returncode == 0, r.stderr
    assert "ID=[]" in r.stdout
    assert not (h / "insert.sql").exists(), "INSERT 가 호출됨"
    ev = _read(h / "events.log")
    assert "checkpoint_skipped" in ev and reason in ev, ev
    blob = "".join(_read(f) for f in h.iterdir() if f.is_file()) + r.stdout + r.stderr
    for s in secrets:
        assert s not in blob, "값이 로그·이벤트·SQL 에 노출됨"
    assert not list((tmp_path / "tmp").iterdir()), "임시 파일이 남음"
    return ev


def _bin(n=65536):
    """압축·반복이 안 되는 결정적 바이트열(NUL 포함) — git 이 delta 를 고르게 한다."""
    out = bytearray()
    i = 0
    while len(out) < n:
        out += hashlib.sha256(str(i).encode()).digest()
        i += 1
    return bytes(out[:n])


# ── 시크릿 행: 추가 / 삭제 / 문맥 / +++ 로 시작하는 추가 행 ───────────────────────

def test_added_key_line_blocked(tmp_path, job):
    jid, wt, commit_base, _ = job
    (wt / "a.txt").write_text("one\n")
    commit_base()
    (wt / "a.txt").write_text(f"one\nKEY={AWS}\n")
    ev = _assert_blocked(tmp_path, jid, "secret_detected", [AWS])
    assert "aws_access_key" in ev and "a.txt" in ev


def test_deleted_key_line_blocked(tmp_path, job):
    jid, wt, commit_base, _ = job
    (wt / "a.txt").write_text(f"one\nKEY={AWS}\ntwo\n")
    commit_base()
    (wt / "a.txt").write_text("one\ntwo\n")
    _assert_blocked(tmp_path, jid, "secret_detected", [AWS])


def test_key_only_in_context_line_blocked(tmp_path, job):
    jid, wt, commit_base, _ = job
    (wt / "a.txt").write_text(f"alpha\nKEY={AWS}\nbeta\n")
    commit_base()
    (wt / "a.txt").write_text(f"alpha\nKEY={AWS}\nbeta2\n")
    _assert_blocked(tmp_path, jid, "secret_detected", [AWS])


def test_added_line_starting_with_plus_plus_blocked(tmp_path, job):
    jid, wt, commit_base, _ = job
    (wt / "a.txt").write_text("one\n")
    commit_base()
    (wt / "a.txt").write_text(f"one\n++TOKEN={OAT}\n")
    ev = _assert_blocked(tmp_path, jid, "secret_detected", [OAT])
    assert "anthropic_oauth_token" in ev


# ── 바이너리: 해독하지 않고 거부 ────────────────────────────────────────────

def test_new_binary_with_nul_rejected(tmp_path, job):
    jid, wt, commit_base, g = job
    (wt / "a.txt").write_text("one\n")
    commit_base()
    (wt / "new.bin").write_bytes(b"\x00\x01\x02" + b"payload" * 50)
    ev = _assert_blocked(tmp_path, jid, "secret_unscannable")
    assert "binary_patch" in ev and "new.bin" in ev


def test_one_byte_change_in_64k_binary_rejected_as_delta(tmp_path, job):
    jid, wt, commit_base, g = job
    data = bytearray(_bin())
    (wt / "big.bin").write_bytes(bytes(data))
    commit_base()
    data[30000] ^= 0xFF
    (wt / "big.bin").write_bytes(bytes(data))
    raw = subprocess.run(["git", "-C", str(wt), "diff", "--binary", "HEAD"], check=True, capture_output=True).stdout
    assert re.search(rb"(?m)^delta \d+$", raw) is not None  # 전제: delta 블록이 생긴다
    _assert_blocked(tmp_path, jid, "secret_unscannable")


def test_deleted_binary_rejected(tmp_path, job):
    jid, wt, commit_base, g = job
    (wt / "gone.bin").write_bytes(b"\x00" + _bin(2000))
    commit_base()
    (wt / "gone.bin").unlink()
    _assert_blocked(tmp_path, jid, "secret_unscannable")


def test_gitattributes_minus_diff_text_rejected(tmp_path, job):
    jid, wt, commit_base, _ = job
    (wt / ".gitattributes").write_text("*.txt -diff\n")
    (wt / "a.txt").write_text("one\n")
    commit_base()
    (wt / "a.txt").write_text("one\ntwo\n")
    _assert_blocked(tmp_path, jid, "secret_unscannable")


def test_binary_files_differ_marker_rejected(tmp_path):
    patch = tmp_path / "p.patch"
    patch.write_bytes(b"diff --git a/x b/x\nindex 1..2 100644\nBinary files a/x and b/x differ\n")
    (tmp_path / "f.json").write_text('["x"]')
    body = f'rc=0; _ckpt_secret_scan {patch} {tmp_path}/f.json {tmp_path}/out.json {tmp_path} || rc=$?; echo "RC=$rc"'
    r, _ = _run(tmp_path, body, env=NO_GITLEAKS)
    assert "RC=11" in r.stdout, r.stdout + r.stderr


def test_text_patch_with_binary_looking_text_is_not_rejected(tmp_path, job):
    """텍스트 행에 'literal 12' 같은 문구가 있어도 접두(+/-/공백) 때문에 바이너리로 오인하지 않는다."""
    jid, wt, commit_base, _ = job
    (wt / "a.txt").write_text("one\n")
    commit_base()
    (wt / "a.txt").write_text("one\nliteral 12\nGIT binary patch\ndelta 3\n")
    r, h = _save(tmp_path, jid)
    assert f"ID=[{FAKE_ID}]" in r.stdout and (h / "insert.sql").exists()


# ── 비밀 파일 이름 ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("name", ["credentials.json", "id_rsa", ".env", ".env.production", "server.pem", "tls.key",
                                  "id_ed25519", "sub/dir/gcp-credentials-prod.json", "store.p12"])
def test_untracked_secret_file_names_blocked(tmp_path, job, name):
    jid, wt, commit_base, _ = job
    (wt / "a.txt").write_text("one\n")
    commit_base()
    (wt / name).parent.mkdir(parents=True, exist_ok=True)
    (wt / name).write_text("nothing-secret-looking\n")
    ev = _assert_blocked(tmp_path, jid, "secret_detected")
    assert "secret_filename" in ev


def test_untracked_private_key_block_blocked(tmp_path, job):
    jid, wt, commit_base, _ = job
    (wt / "a.txt").write_text("one\n")
    commit_base()
    (wt / "notes.txt").write_text(f"{PEM_HEAD}\nMIIB\n")
    ev = _assert_blocked(tmp_path, jid, "secret_detected")
    assert "private_key_block" in ev


def test_key_used_as_filename_is_redacted(tmp_path, job):
    jid, wt, commit_base, _ = job
    (wt / "a.txt").write_text("one\n")
    commit_base()
    (wt / f"{AWS}.key").write_text("x\n")
    ev = _assert_blocked(tmp_path, jid, "secret_detected", [AWS])
    assert "<redacted:aws_access_key>" in ev


# ── fail-closed ────────────────────────────────────────────────────────────

def _dirty_clean_text(wt, commit_base):
    (wt / "a.txt").write_text("one\n")
    commit_base()
    (wt / "a.txt").write_text("one\ntwo\n")


def test_scan_command_failure_not_saved(tmp_path, job):
    jid, wt, commit_base, _ = job
    _dirty_clean_text(wt, commit_base)
    shim = _shim(tmp_path, "python3", f'case "$*" in *fnmatch*) exit 7;; esac\nexec {sys.executable} "$@"\n')
    _assert_blocked(tmp_path, jid, "secret_scan_failed", env={"PATH": f"{shim}:{os.environ['PATH']}"})


def test_scan_empty_output_not_saved(tmp_path, job):
    jid, wt, commit_base, _ = job
    _dirty_clean_text(wt, commit_base)
    shim = _shim(tmp_path, "python3", f'case "$*" in *fnmatch*) exit 0;; esac\nexec {sys.executable} "$@"\n')
    _assert_blocked(tmp_path, jid, "secret_scan_failed", env={"PATH": f"{shim}:{os.environ['PATH']}"})


def test_scan_timeout_not_saved(tmp_path, job):
    """검사가 멈추면 시간 상한에서 끊기고 저장하지 않는다(이벤트는 남은 예산이 없어 생략될 수 있다)."""
    jid, wt, commit_base, _ = job
    _dirty_clean_text(wt, commit_base)
    shim = _shim(tmp_path, "python3", f'case "$*" in *fnmatch*) sleep 30;; esac\nexec {sys.executable} "$@"\n')
    r, h = _save(tmp_path, jid, {"PATH": f"{shim}:{os.environ['PATH']}", "CHECKPOINT_SAVE_TIMEOUT": "2"})
    assert r.returncode == 0 and "ID=[]" in r.stdout and not (h / "insert.sql").exists()


def test_diff_command_failure_not_saved(tmp_path, job):
    jid, wt, commit_base, _ = job
    _dirty_clean_text(wt, commit_base)
    real_git = shutil.which("git")
    shim = _shim(tmp_path, "git", f'case "$*" in *"diff --cached --binary"*) exit 9;; esac\nexec {real_git} "$@"\n')
    _assert_blocked(tmp_path, jid, "secret_scan_failed", env={"PATH": f"{shim}:{os.environ['PATH']}"})


def test_file_list_command_failure_not_saved(tmp_path, job):
    jid, wt, commit_base, _ = job
    _dirty_clean_text(wt, commit_base)
    real_git = shutil.which("git")
    shim = _shim(tmp_path, "git", f'case "$*" in *"--name-only"*) exit 9;; esac\nexec {real_git} "$@"\n')
    _assert_blocked(tmp_path, jid, "secret_scan_failed", env={"PATH": f"{shim}:{os.environ['PATH']}"})


@pytest.mark.parametrize("script,reason,extra", [
    ("exit 1", "secret_scan_failed", {}),
    ("exit 2", "secret_detected", {}),
    ("sleep 30", "secret_scan_failed", {"CHECKPOINT_GITLEAKS_TIMEOUT": "1"}),
])
def test_gitleaks_failure_not_saved(tmp_path, job, script, reason, extra):
    jid, wt, commit_base, _ = job
    _dirty_clean_text(wt, commit_base)
    fake = _shim(tmp_path, "fake-gitleaks", script + "\n") / "fake-gitleaks"
    r, h = _run(tmp_path, f'_shutdown_save_checkpoint {jid} 1; echo "ID=[$_CKPT_LAST_ID]"',
                env={"CHECKPOINT_GITLEAKS_BIN": str(fake), **extra})
    assert "ID=[]" in r.stdout and not (h / "insert.sql").exists()
    assert reason in _read(h / "events.log")


def test_gitleaks_clean_saves(tmp_path, job):
    jid, wt, commit_base, _ = job
    _dirty_clean_text(wt, commit_base)
    fake = _shim(tmp_path, "fake-gitleaks", "cat >/dev/null\nexit 0\n") / "fake-gitleaks"
    r, h = _run(tmp_path, f'_shutdown_save_checkpoint {jid} 1; echo "ID=[$_CKPT_LAST_ID]"',
                env={"CHECKPOINT_GITLEAKS_BIN": str(fake)})
    assert f"ID=[{FAKE_ID}]" in r.stdout and (h / "insert.sql").exists()


@pytest.mark.skipif(shutil.which("gitleaks") is None, reason="gitleaks 미설치")
def test_real_gitleaks_clean_text_saved(tmp_path, job):
    jid, wt, commit_base, _ = job
    _dirty_clean_text(wt, commit_base)
    r, h = _run(tmp_path, f'_shutdown_save_checkpoint {jid} 1; echo "ID=[$_CKPT_LAST_ID]"')
    assert f"ID=[{FAKE_ID}]" in r.stdout and (h / "insert.sql").exists()


# ── 회귀: 정상 patch 는 저장된다 ──────────────────────────────────────────────

def test_clean_text_patch_saved(tmp_path, job):
    jid, wt, commit_base, _ = job
    _dirty_clean_text(wt, commit_base)
    (wt / "new.txt").write_text("brand new\n")
    r, h = _save(tmp_path, jid)
    assert f"ID=[{FAKE_ID}]" in r.stdout and (h / "insert.sql").exists()
    assert '"files":2' in _read(h / "events.log")
    assert not list((tmp_path / "tmp").iterdir())


def test_env_example_saved(tmp_path, job):
    jid, wt, commit_base, _ = job
    _dirty_clean_text(wt, commit_base)
    (wt / ".env.example").write_text("SOME_SETTING=\n")
    r, h = _save(tmp_path, jid)
    assert f"ID=[{FAKE_ID}]" in r.stdout and (h / "insert.sql").exists()


# ── findings JSON 은 길어도 잘리지 않는다(리뷰 지적 2) ────────────────────────────

def _skipped_meta(h):
    for line in _read(h / "events.log").splitlines():
        if line.startswith("checkpoint_skipped|"):
            return json.loads(line.split("|", 1)[1])
    raise AssertionError("checkpoint_skipped 이벤트 없음")


def test_findings_json_not_truncated_with_many_long_paths(tmp_path, job):
    jid, wt, commit_base, _ = job
    (wt / "a.txt").write_text("one\n")
    commit_base()
    for i in range(40):
        (wt / (f"file{i:02d}_" + "d" * 140 + ".txt")).write_text(f"KEY={AWS}\n")
    r, h = _save(tmp_path, jid)
    assert not (h / "insert.sql").exists()
    raw = [ln for ln in _read(h / "events.log").splitlines() if ln.startswith("checkpoint_skipped|")][0]
    assert len(raw.encode()) > 6000, "재현 조건(6000바이트 초과)이 아님"
    meta = _skipped_meta(h)
    scan = meta["scan"]
    assert meta["reason"] == "secret_detected"
    assert scan["count"] == 40 and len(scan["findings"]) == 40 and scan["truncated"] is False
    assert all(f["pattern"] == "aws_access_key" for f in scan["findings"])
    assert AWS not in raw


def test_findings_capped_by_scanner_but_still_valid_json(tmp_path, job):
    jid, wt, commit_base, _ = job
    (wt / "a.txt").write_text("one\n")
    commit_base()
    for i in range(70):
        (wt / f"f{i:02d}.txt").write_text(f"KEY={AWS}\n")
    r, h = _save(tmp_path, jid)
    scan = _skipped_meta(h)["scan"]
    assert scan["count"] == 70 and len(scan["findings"]) == 50 and scan["truncated"] is True


# ── 두 러너 파일의 해당 블록은 동일하다 ───────────────────────────────────────

def test_runner_and_local_blocks_identical():
    a, b = SH.read_text(encoding="utf-8"), LOCAL.read_text(encoding="utf-8")
    assert _extract(a) == _extract(b)
