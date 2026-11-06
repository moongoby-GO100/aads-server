"""체크포인트 patch 열 앱 측 인증 암호화(v1) — 채팅 SQL 경로에는 암호문만 보여야 한다.

저장(_shutdown_save_checkpoint)·복원(_ckpt_resume_try) 하네스는 기존 save/resume 시험의 것을 그대로 쓴다.
암호문 해독 쪽은 러너와 별개로 형식 명세만으로 만든 tests/unit/ckpt_crypto_helper.py 가 맡는다.
"""
import base64
import os
import re
import shutil
import stat
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tests.unit import ckpt_crypto_helper as cc  # noqa: E402
from test_pipeline_runner_checkpoint_resume import World, _assert_fallback  # noqa: E402
from test_pipeline_runner_checkpoint_save import (  # noqa: E402
    FAKE_ID, HARNESS_HEAD, LOCAL, SH, _extract, _read, _run,
)

SECRETISH = "ZETA-UNIQUE-PLAINTEXT-MARKER-9f3a1c"


@pytest.fixture
def world(tmp_path):
    w = World(tmp_path)
    yield w
    shutil.rmtree(f"/tmp/aads-wt-{w.jid}", ignore_errors=True)


@pytest.fixture
def job():
    """/tmp/aads-wt-<jid> 에 base 커밋이 있는 작업 폴더."""
    jid = "runner-ckptenc" + uuid.uuid4().hex[:8]
    wt = Path(f"/tmp/aads-wt-{jid}")

    def g(*a):
        return subprocess.run(["git", "-C", str(wt), *a], check=True, capture_output=True, text=True)

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


def _stored_blob(h: Path) -> bytes:
    sql = _read(h / "insert.sql")
    m = re.search(r"decode\('([A-Za-z0-9+/=]+)','base64'\)", sql)
    return base64.b64decode(m.group(1))


def _save(tmp_path, jid, env=None):
    return _run(tmp_path, f'_shutdown_save_checkpoint {jid} 1; echo "ID=[$_CKPT_LAST_ID]"', env=env)


def _dirty_marked(wt):
    (wt / "a.txt").write_text(f"one\n{SECRETISH}\n")
    (wt / "new.txt").write_text(f"brand {SECRETISH} new\n")


# ── 저장 ────────────────────────────────────────────────────────────

def test_save_roundtrip_and_ciphertext_hides_plaintext(tmp_path, job):
    jid, wt, base, g = job
    _dirty_marked(wt)
    r, h = _save(tmp_path, jid)
    assert f"ID=[{FAKE_ID}]" in r.stdout, r.stdout + r.stderr
    sql = _read(h / "insert.sql")
    blob = _stored_blob(h)
    assert blob.startswith(b"v1:")
    plain = cc.decrypt(cc.make_key_file(tmp_path), jid, base, blob)
    assert SECRETISH.encode() in plain and b"diff --git" in plain
    # 암호문·INSERT 문 어디에도 원문 조각이 없다(부분 문자열 포함)
    for needle in (SECRETISH.encode(), b"diff --git", b"brand", b"+one", plain[40:60]):
        assert needle not in blob and needle not in sql.encode()
    assert b"new.txt" not in blob  # 경로 이름은 files 열(평문 jsonb)에 남는다 — 내용이 아니다


def test_same_input_gets_fresh_nonce(tmp_path, job):
    jid, wt, *_ = job
    _dirty_marked(wt)
    body = (f'_shutdown_save_checkpoint {jid} 1; cp "$H/insert.sql" "$H/insert1.sql"\n'
            f'_shutdown_save_checkpoint {jid} 1')
    r, h = _run(tmp_path, body)
    first = re.search(r"decode\('([A-Za-z0-9+/=]+)','base64'\)", _read(h / "insert1.sql")).group(1)
    assert first and base64.b64decode(first) != _stored_blob(h)


def test_runner_own_enc_dec_roundtrip_and_binary_safe(tmp_path):
    key = cc.make_key_file(tmp_path)
    data = bytes(range(256)) * 50
    (tmp_path / "in").write_bytes(data)
    body = ('_ckpt_crypt enc j1 abc "$H/../in" "$H/enc" && echo ENC=0\n'
            '_ckpt_crypt dec j1 abc "$H/enc" "$H/out" && echo DEC=0\n'
            '_ckpt_crypt dec j2 abc "$H/enc" "$H/out2" || echo WRONG_JOB=$?\n'
            '_ckpt_crypt dec j1 def "$H/enc" "$H/out3" || echo WRONG_BASE=$?')
    r, h = _run(tmp_path, body)
    assert "ENC=0" in r.stdout and "DEC=0" in r.stdout, r.stdout + r.stderr
    assert (h / "out").read_bytes() == data
    assert "WRONG_JOB=4" in r.stdout and "WRONG_BASE=4" in r.stdout
    assert not (h / "out2").exists() and not (h / "out3").exists()
    assert cc.decrypt(key, "j1", "abc", (h / "enc").read_bytes()) == data  # 독립 구현으로도 풀린다
    assert stat.S_IMODE((h / "enc").stat().st_mode) == 0o600 and stat.S_IMODE((h / "out").stat().st_mode) == 0o600


def _no_key_cases(tmp_path):
    d = tmp_path / "keys"
    d.mkdir()
    good = cc.make_key_file(d, "good.key")
    yield "missing", {"RUNNER_CHECKPOINT_KEY_FILE": str(d / "nope.key")}
    yield "mode0644", cc.key_env(d, name="m644.key", mode=0o644)
    yield "mode0640", cc.key_env(d, name="m640.key", mode=0o640)
    yield "mode0400", cc.key_env(d, name="m400.key", mode=0o400)
    yield "short", cc.key_env(d, name="short.key", content="ab" * 16)
    yield "long", cc.key_env(d, name="long.key", content="ab" * 40)
    yield "nonhex", cc.key_env(d, name="nonhex.key", content="zz" * 32)
    yield "empty", cc.key_env(d, name="empty.key", content="")
    link = d / "link.key"
    link.symlink_to(good)
    yield "symlink", {"RUNNER_CHECKPOINT_KEY_FILE": str(link)}
    yield "directory", {"RUNNER_CHECKPOINT_KEY_FILE": str(d)}


@pytest.mark.parametrize("case", ["missing", "mode0644", "mode0640", "mode0400", "short", "long", "nonhex", "empty",
                                  "symlink", "directory"])
def test_save_without_usable_key_stores_nothing(tmp_path, job, case):
    jid, wt, *_ = job
    _dirty_marked(wt)
    env = dict(_no_key_cases(tmp_path))[case]
    r, h = _save(tmp_path, jid, env=env)
    assert r.returncode == 0, r.stderr
    assert "ID=[]" in r.stdout
    assert not (h / "insert.sql").exists(), "키가 없는데 INSERT 가 호출됨"
    ev = _read(h / "events.log")
    assert "checkpoint_skipped" in ev and "key_unavailable" in ev


def test_save_with_default_path_missing_stores_nothing(tmp_path, job):
    """환경변수가 없고 기본 경로(/root/.config/aads-runner/checkpoint.key)에도 키가 없으면 저장하지 않는다."""
    if Path("/root/.config/aads-runner/checkpoint.key").exists():
        pytest.skip("이 호스트에는 실제 키가 있다 — 건드리지 않는다")
    jid, wt, *_ = job
    _dirty_marked(wt)
    env = {k: v for k, v in os.environ.items() if k != "RUNNER_CHECKPOINT_KEY_FILE"}
    text = SH.read_text(encoding="utf-8")
    h = tmp_path / "h2"
    h.mkdir()
    # _run 은 키 환경변수를 주입하므로 직접 돌린다
    script = f"H={h}\nFAKE_ID={FAKE_ID}\n{HARNESS_HEAD}\n{_extract(text)}\n_shutdown_save_checkpoint {jid} 1; echo \"ID=[$_CKPT_LAST_ID]\"\n"
    (h / "run.sh").write_text(script, encoding="utf-8")
    r = subprocess.run(["bash", str(h / "run.sh")], capture_output=True, text=True, env=env, timeout=120)
    assert "ID=[]" in r.stdout and not (h / "insert.sql").exists()
    assert "key_unavailable" in _read(h / "events.log")


def test_missing_key_still_requeues_job(tmp_path, job):
    jid, wt, *_ = job
    _dirty_marked(wt)
    r, h = _run(tmp_path, f'_shutdown_finalize_job {jid} sid quiet',
                env={"RUNNER_CHECKPOINT_KEY_FILE": str(tmp_path / "nope")})
    assert r.returncode == 0, r.stderr
    up = _read(h / "update.log")
    assert "status='queued'" in up and "checkpoint=" not in up
    assert not (h / "insert.sql").exists()


def test_secret_check_still_runs_before_encryption(tmp_path, job):
    """시크릿 검사는 완화되지 않았다: 시크릿이 있으면 키가 있어도, 없어도 secret_detected 로 막힌다."""
    jid, wt, *_ = job
    (wt / "a.txt").write_text("one\nAPI_KEY=" + "A1b2C3d4" * 4 + "\n")
    for name, env in (("with_key", None), ("no_key", {"RUNNER_CHECKPOINT_KEY_FILE": str(tmp_path / "nope")})):
        sub = tmp_path / name
        sub.mkdir()
        r, h = _save(sub, jid, env=env)
        assert "ID=[]" in r.stdout and not (h / "insert.sql").exists()
        assert "secret_detected" in _read(h / "events.log")


# ── 복원 ────────────────────────────────────────────────────────────

def test_resume_decrypts_and_applies(world):
    r, h = world.run()
    assert "RC=0" in r.stdout, r.stdout + r.stderr
    assert (world.wt / "new.txt").read_text() == "brand new\n"
    assert not _read(h / "fail.log")


def _flip(blob: bytes, pos: int) -> bytes:
    b = bytearray(blob)
    b[pos] ^= 0x01
    return bytes(b)


@pytest.mark.parametrize("where", ["version", "nonce_first", "nonce_last", "ct_first", "ct_mid", "ct_last", "tag_first", "tag_last"])
def test_one_byte_tamper_falls_back(world, where):
    blob = world.stored()
    n = len(blob)
    pos = {"version": 1, "nonce_first": 3, "nonce_last": 18, "ct_first": 19, "ct_mid": 19 + (n - 19 - 32) // 2,
           "ct_last": n - 33, "tag_first": n - 32, "tag_last": n - 1}[where]
    r, h = world.run(raw_stored=_flip(blob, pos))
    _assert_fallback(world, r, h, "decrypt_failed")


def test_truncated_and_extended_blob_fall_back(world):
    blob = world.stored()
    for bad in (blob[:-1], blob + b"\x00", blob[:20], b"v1:"):
        shutil.rmtree(world.wt, ignore_errors=True)
        subprocess.run(["git", "-C", str(world.main_b), "worktree", "prune"], check=True)
        r, h = world.run(raw_stored=bad)
        _assert_fallback(world, r, h, "decrypt_failed")


def test_plaintext_row_is_not_trusted(world):
    """접두 없는 평문 행(옛 형식)은 sha 가 맞아도 이어받지 않는다."""
    r, h = world.run(raw_stored=world.patch)
    _assert_fallback(world, r, h, "decrypt_failed")


def test_unknown_version_falls_back(world):
    r, h = world.run(raw_stored=b"v2:" + world.stored()[3:])
    _assert_fallback(world, r, h, "decrypt_failed")


def test_ciphertext_of_other_job_or_base_falls_back(world):
    other = cc.encrypt(cc.make_key_file(world.tmp), "runner-someone-else", world.base, world.patch)
    r, h = world.run(raw_stored=other)
    _assert_fallback(world, r, h, "decrypt_failed")


def test_wrong_key_falls_back(world):
    other_dir = world.tmp / "otherkey"
    other_dir.mkdir()
    blob = cc.encrypt(cc.make_key_file(other_dir), world.jid, world.base, world.patch)
    r, h = world.run(raw_stored=blob)
    _assert_fallback(world, r, h, "decrypt_failed")


@pytest.mark.parametrize("case", ["missing", "mode0644", "short"])
def test_resume_without_usable_key_falls_back(world, case):
    env = dict(_no_key_cases(world.tmp))[case]
    r, h = world.run(env=env)
    _assert_fallback(world, r, h, "key_unavailable")


# ── 새지 않음 ───────────────────────────────────────────────────────

def test_key_and_plaintext_do_not_leak(tmp_path, job):
    """argv(ps)·환경변수·로그·이벤트·SQL·임시 디렉터리 어디에도 키(원문·파생값 모두)와 평문이 없다."""
    jid, wt, base, g = job
    _dirty_marked(wt)
    key_path = cc.make_key_file(tmp_path)
    key_hex = key_path.read_text().strip()
    shim_dir = tmp_path / "shim"
    shim_dir.mkdir()
    real = shutil.which("openssl")
    probe = tmp_path / "probe.log"
    shim = shim_dir / "openssl"
    shim.write_text(f'#!/bin/bash\n{{ printf "ARGV:%s\\n" "$*"; tr "\\0" "\\n" </proc/$$/environ; }} >> {probe}\nexec {real} "$@"\n')
    shim.chmod(0o755)
    tdir = tmp_path / "tmp"
    tdir.mkdir()
    body = f'_shutdown_save_checkpoint {jid} 1; echo "ID=[$_CKPT_LAST_ID]"'
    pre = 'log() { echo "$*" >> "$H/log.txt"; }'
    r, h = _run(tmp_path, body, env={"PATH": f"{shim_dir}:{os.environ['PATH']}", "TMPDIR": str(tdir)}, preamble=pre)
    assert f"ID=[{FAKE_ID}]" in r.stdout, r.stdout + r.stderr
    blob = _stored_blob(h)
    plain = cc.decrypt(key_path, jid, base, blob)
    import hashlib
    import hmac
    master = bytes.fromhex(key_hex)
    nonce = blob[3:19]
    derived = [key_hex, hmac.new(master, b"aads-ckpt-v1|enc|" + nonce, hashlib.sha256).hexdigest(),
               hmac.new(master, b"aads-ckpt-v1|mac", hashlib.sha256).hexdigest(), master.hex()]
    probe_txt = _read(probe)
    assert "ARGV:" in probe_txt, "openssl 이 호출되지 않았다"
    argv_lines = [ln for ln in probe_txt.split("\n") if ln.startswith("ARGV:")]
    assert argv_lines and all(" -K " not in f" {ln} " and " -iv " not in f" {ln} " and "-pass fd:" in ln for ln in argv_lines)
    outputs = {"openssl argv/env": probe_txt, "stdout": r.stdout, "stderr": r.stderr}
    for f in h.iterdir():
        if f.name not in ("run.sh",) and f.is_file():
            outputs[f.name] = f.read_text(errors="replace")
    # run.sh 는 하네스 코드이므로 키 값이 들어 있을 수 없고, 그래도 함께 확인한다.
    outputs["run.sh"] = _read(h / "run.sh")
    for name, txt in outputs.items():
        for k in derived:
            assert k not in txt, f"{name} 에 키/파생값이 있다"
        assert SECRETISH not in txt, f"{name} 에 평문이 있다"
    # 임시 디렉터리에 평문·키가 남지 않는다(mktemp -d 는 호출자가 지운다)
    leftovers = [p for p in tdir.rglob("*") if p.is_file()]
    assert leftovers == [], leftovers
    assert plain  # 복호화 결과는 시험 안에서만 쓴다


def test_resume_leaves_no_temp_files_and_no_plaintext_in_logs(world, tmp_path):
    tdir = tmp_path / "tmpr"
    tdir.mkdir()
    r, h = world.run(env={"TMPDIR": str(tdir)})
    assert "RC=0" in r.stdout
    assert [p for p in tdir.rglob("*") if p.is_file()] == []
    for name in ("log.txt", "events.log", "sql.log"):
        assert "brand new" not in _read(h / name)
    assert "brand new" not in r.stdout + r.stderr


def test_crypto_code_has_no_key_in_argv_and_no_key_generation():
    text = SH.read_text(encoding="utf-8")
    m = re.search(r"^_ckpt_crypt_py\(\) \{.*?^\}\n", text, re.S | re.M)
    code = m.group(0)
    assert '"-K"' not in code and "'-K'" not in code and '"-iv"' not in code
    assert "fd:%d" in code and "pass_fds" in code
    assert "token_bytes" in code  # nonce 생성만. 키 생성 코드는 없다
    assert "openssl rand" not in text and "genrsa" not in text
    assert "checkpoint.key" in code  # 기본 경로
    assert "ANTHROPIC_API_KEY" not in code


def test_sh_and_local_identical():
    assert SH.read_bytes() == LOCAL.read_bytes()
