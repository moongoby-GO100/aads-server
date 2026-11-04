"""sync_pipeline_runner_remote.sh: 대상 루프 안의 ssh 가 남은 대상 행을 먹지 않는다.

2026-11-04 실측: warn_stale_runner_processes 의 ssh 에 -n 이 없어 첫 대상(contabo14) 처리 중
here-string 의 나머지(cafe24_114)를 읽어 버렸다. 로그는 매번 targets=1 이었다.
"""
import os
import stat
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SYNC = ROOT / "scripts/sync_pipeline_runner_remote.sh"


def _function(text: str, name: str) -> str:
    start = text.index(name + "() {")
    return text[start:text.index("\n}\n", start) + 3]


def test_warn_stale_ssh_does_not_consume_target_list(tmp_path):
    fake = tmp_path / "ssh"
    # 실제 ssh 처럼 -n 이 없으면 stdin 을 끝까지 읽는다.
    fake.write_text('#!/usr/bin/env bash\nfor a in "$@"; do [ "$a" = -n ] && exit 0; done\ncat >/dev/null\n')
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    fn = _function(SYNC.read_text(), "warn_stale_runner_processes")
    script = (
        "set -u\nSSH_OPTS=(-o BatchMode=yes)\nlog(){ :; }\n" + fn +
        'targets=$(printf "contabo14|contabo14|x\\ncafe24_114|server-114|x\\n")\n'
        'while IFS= read -r t || [[ -n "$t" ]]; do\n'
        '  [[ -n "$t" ]] || continue\n'
        '  echo "seen ${t%%|*}"\n'
        '  IFS="|" read -r n h _ <<< "$t"\n'
        '  warn_stale_runner_processes "$n" "$h"\n'
        'done <<< "$targets"\n'
    )
    env = dict(os.environ, PATH=f"{tmp_path}:{os.environ['PATH']}")
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env, timeout=30)
    assert r.returncode == 0, r.stderr
    assert r.stdout.split("\n")[:2] == ["seen contabo14", "seen cafe24_114"]
