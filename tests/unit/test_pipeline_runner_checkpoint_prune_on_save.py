"""체크포인트 저장 성공 직후 정리 함수 호출 — cron 없이도 표 크기가 제한되는지 확인.

기존 저장 테스트의 bash 하네스(_run)와 작업 폴더 fixture(job)를 재사용하고,
정리 SQL 호출 여부는 db_exec 대역을 감싸 별도 로그에 남겨 확인한다.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_pipeline_runner_checkpoint_save import (  # noqa: E402,F401
    LOCAL, SH, _dirty, _read, _run, job,
)

PRUNE = "aads_pipeline_job_checkpoints_purge_terminal(), aads_pipeline_job_checkpoints_prune(interval '7 days')"

# 원래 db_exec 대역을 _orig_db_exec 로 옮기고, 정리 SQL 은 따로 기록한다.
WRAP = r"""
eval "$(declare -f db_exec | sed '1s/^db_exec/_orig_db_exec/')"
db_exec() {
    case "$1" in
      *aads_pipeline_job_checkpoints_purge_terminal*)
          echo x >> "$H/prune.log"
          [[ "${MOCK_PRUNE:-ok}" == "ok" ]] || return 1
          echo "0|0"; return 0 ;;
    esac
    _orig_db_exec "$@"
}
"""


def _prune_calls(h):
    return len(_read(h / "prune.log").split())


def test_prune_called_once_after_save(tmp_path, job):
    jid, wt, *_ = job
    _dirty(wt)
    r, h = _run(tmp_path, f'_shutdown_save_checkpoint {jid} 1; echo "ID=$_CKPT_LAST_ID"', preamble=WRAP)
    assert r.returncode == 0, r.stderr
    assert _prune_calls(h) == 1
    assert "ID=11111111-2222-3333-4444-555555555555" in r.stdout


def test_prune_not_called_when_table_missing(tmp_path, job):
    jid, wt, *_ = job
    _dirty(wt)
    r, h = _run(tmp_path, f'_shutdown_save_checkpoint {jid} 1; echo "ID=[$_CKPT_LAST_ID]"',
                env={"MOCK_TABLE": "no"}, preamble=WRAP)
    assert "ID=[]" in r.stdout and _prune_calls(h) == 0


def test_prune_not_called_when_no_changes(tmp_path, job):
    jid, *_ = job
    r, h = _run(tmp_path, f'_shutdown_save_checkpoint {jid} 1', preamble=WRAP)
    assert _prune_calls(h) == 0


def test_prune_not_called_when_insert_fails(tmp_path, job):
    jid, wt, *_ = job
    _dirty(wt)
    r, h = _run(tmp_path, f'_shutdown_save_checkpoint {jid} 1; echo "ID=[$_CKPT_LAST_ID]"',
                env={"MOCK_INSERT": "fail"}, preamble=WRAP)
    assert "ID=[]" in r.stdout and _prune_calls(h) == 0


def test_prune_failure_keeps_saved(tmp_path, job):
    jid, wt, *_ = job
    _dirty(wt)
    r, h = _run(tmp_path, f'_shutdown_save_checkpoint {jid} 1; echo "rc=$? ID=$_CKPT_LAST_ID"',
                env={"MOCK_PRUNE": "fail"}, preamble=WRAP)
    assert "rc=0" in r.stdout and "ID=11111111-2222-3333-4444-555555555555" in r.stdout
    assert _prune_calls(h) == 1
    assert "checkpoint_saved" in _read(h / "events.log")


def test_prune_timeout_keeps_saved(tmp_path, job):
    jid, wt, *_ = job
    _dirty(wt)
    slow = WRAP.replace('echo x >> "$H/prune.log"', 'echo x >> "$H/prune.log"; sleep 30')
    r, h = _run(tmp_path, f'_shutdown_save_checkpoint {jid} 1; echo "ID=$_CKPT_LAST_ID"',
                env={"CHECKPOINT_SAVE_TIMEOUT": "2"}, preamble=slow)
    # 시간 초과로 예산을 다 썼어도 저장 결과(id)는 유지된다(이벤트 기록은 남은 예산이 없어 생략될 수 있다).
    assert "ID=11111111-2222-3333-4444-555555555555" in r.stdout


def test_both_scripts_identical_and_call_prune():
    a, b = SH.read_text(encoding="utf-8"), LOCAL.read_text(encoding="utf-8")
    assert a == b
    assert PRUNE in a
