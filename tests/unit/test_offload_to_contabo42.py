"""offload_to_contabo42.sh: 카페24 검증 기록이 있고 42 대조가 맞는 덤프만 116 에서 지운다."""
import hashlib
import os
import stat
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "offload_to_contabo42.sh"


def _exe(path: Path, body: str) -> None:
    path.write_text("#!/usr/bin/env bash\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def _setup(tmp_path: Path, remote_sum_ok: bool):
    remote = tmp_path / "remote"
    remote.mkdir()
    bindir = tmp_path / "bin"
    bindir.mkdir()
    # ssh: 원격 명령을 로컬에서 실행하되, 경로를 가짜 원격 디렉터리로 바꾼다. 대조 실패 모드 지원.
    _exe(bindir / "ssh", f'''cmd="${{@: -1}}"
cmd="${{cmd//\\/srv\\/r/{remote}}}"
if [[ "$cmd" == sha256sum* && "{int(remote_sum_ok)}" == 0 ]]; then echo "deadbeef  x"; exit 0; fi
bash -c "$cmd"
''')
    # rsync: 마지막 인자 host:path → 로컬 경로로 바꿔 진짜 rsync 대신 cp 로 흉내
    _exe(bindir / "rsync", f'''args=("$@"); dest="${{args[-1]}}"; dest="{remote}${{dest#*:/srv/r}}"
remove=0; files_from=""; srcs=()
for a in "${{args[@]:0:${{#args[@]}}-1}}"; do
  case "$a" in --remove-source-files) remove=1;; --files-from=*) files_from="${{a#*=}}";; -*) ;; *) srcs+=("$a");; esac
done
mkdir -p "$dest"
if [[ -n "$files_from" ]]; then
  while read -r f; do mkdir -p "$dest/$(dirname "$f")"; cp "${{srcs[0]}}/$f" "$dest/$f"; if ((remove)); then rm -f "${{srcs[0]}}/$f"; fi; done < "$files_from"
else
  for s in "${{srcs[@]}}"; do cp -r "$s" "$dest/"; if ((remove)); then rm -rf "$s"; fi; done
fi
exit 0
''')
    dumps = tmp_path / "dumps"
    dumps.mkdir()
    dump = dumps / "aads_20991231.dump"
    dump.write_bytes(b"dump-bytes")
    unverified = dumps / "aads_20991230.dump"
    unverified.write_bytes(b"other")
    log = tmp_path / "cafe24.log"
    log.write_text(f"x OFFLOADED dump {dump} checksum={hashlib.sha256(b'dump-bytes').hexdigest()} files=1\n")
    env = dict(os.environ, PATH=f"{bindir}:{os.environ['PATH']}",
               AADS_OFFLOAD42_ROOT="/srv/r", AADS_OFFLOAD42_DUMPS=str(dumps),
               AADS_OFFLOAD42_CAFE24_LOG=str(log), AADS_OFFLOAD42_CODEX_ROOT=str(tmp_path / "codex"),
               AADS_OFFLOAD42_RELEASES=str(tmp_path / "rel"))
    return env, dump, unverified, remote


def test_verified_dump_moves_and_unverified_stays(tmp_path):
    env, dump, unverified, remote = _setup(tmp_path, remote_sum_ok=True)
    r = subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
    assert not dump.exists() and (remote / "db-dumps" / dump.name).exists()
    assert unverified.exists() and "카페24 검증 기록 없음" in r.stdout


def test_remote_checksum_mismatch_keeps_local(tmp_path):
    env, dump, _, _ = _setup(tmp_path, remote_sum_ok=False)
    r = subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=60)
    assert dump.exists() and "불일치" in r.stdout


def test_old_releases_beyond_three_move(tmp_path):
    env, *_ , remote = _setup(tmp_path, remote_sum_ok=True)
    rel = tmp_path / "rel"
    for i in range(5):
        d = rel / f"acct-app-{i}"
        d.mkdir(parents=True)
        (d / "f").write_text("x")
        os.utime(d, (1_000_000 + i, 1_000_000 + i))
    subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=60)
    assert sorted(p.name for p in rel.iterdir() if any(p.iterdir())) == ["acct-app-2", "acct-app-3", "acct-app-4"]
    assert (remote / "component-releases" / "acct-app-0" / "f").exists()
