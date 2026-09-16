"""대시보드 dirty 게이트 — 남의 미커밋 변경이 내 배포를 죽이지 않는지 고정한다.

배경: 2026-09-16 runner-168bac82(AADS-AAG-DEBT-002) 은 대시보드 파일을 하나도
건드리지 않았는데, 다른 세션이 남긴 `src/components/chat/UsageBar.tsx` 미커밋
1건 때문에 `aads-dashboard:isolated_worktree_required` 로 build_fail 이 됐다.
백엔드 push 는 이미 성공한 뒤였다.

그대로 두면 그 파일이 커밋될 때까지 **모든 AADS 백엔드 배포가 인질이 된다.**

게이트의 원래 의도(공유 워크트리에 직접 쓴 잡을 잡는다)는 지켜야 하므로,
"이 잡이 시작한 뒤에 바뀐 dirty 파일이 있는가" 로 책임을 가른다.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ("pipeline-runner.sh", "pipeline-runner.sh.local")


def _read(name: str) -> str:
    return (ROOT / "scripts" / name).read_text(encoding="utf-8")


def test_gate_attributes_dirt_by_job_start_time():
    """시작 시각 이후 변경 건수로 책임을 가르는 판정이 있어야 한다."""
    for name in SCRIPTS:
        script = _read(name)
        assert "EXTRACT(EPOCH FROM started_at)" in script, f"{name}: 잡 시작 시각 조회 없음"
        assert "_dash_recent" in script, f"{name}: 시작 이후 변경 카운트 없음"
        assert "stat -c %Y" in script, f"{name}: mtime 비교 없음"


def test_unrelated_dirt_warns_instead_of_failing():
    """이 잡과 무관한 dirty 는 WARN 으로 끝나야 하고 build_fail 을 붙이면 안 된다."""
    script = _read("pipeline-runner.sh")

    start = script.index("elif [ -n \"$(git -C /root/aads/aads-dashboard status --porcelain")
    end = script.index("if [ \"$DASHBOARD_CHANGED\" = true ]", start)
    block = script[start:end]

    assert "WARN aads-dashboard dirty" in block
    warn_idx = block.index("WARN aads-dashboard dirty")
    fail_idx = block.index("aads-dashboard:isolated_worktree_required")
    # 실패 표식은 BLOCK 분기에만, WARN 분기에는 없어야 한다
    assert fail_idx < warn_idx, "WARN 분기 뒤에 build_fail 이 붙어 있다"
    assert block.count("aads-dashboard:isolated_worktree_required") == 1


def test_gate_still_blocks_when_this_job_touched_shared_worktree():
    """원래 의도는 유지 — 이 잡이 공유 워크트리를 건드렸으면 여전히 막는다."""
    script = _read("pipeline-runner.sh")
    assert "BLOCK aads-dashboard shared worktree changes" in script
    assert '_build_fail="${_build_fail:+${_build_fail};}aads-dashboard:isolated_worktree_required"' in script


def test_unknown_start_time_falls_back_to_blocking():
    """시작 시각을 못 읽으면 예전처럼 막는다 — 모르면 안전한 쪽."""
    script = _read("pipeline-runner.sh")
    assert '"${_dash_started:-0}" -le 0 || "${_dash_recent:-0}" -gt 0' in script


def test_dashboard_build_is_never_triggered_by_unrelated_dirt():
    """WARN 이든 BLOCK 이든 대시보드 빌드는 돌지 않아야 한다(더러운 트리로 빌드 금지)."""
    script = _read("pipeline-runner.sh")
    start = script.index("elif [ -n \"$(git -C /root/aads/aads-dashboard status --porcelain")
    end = script.index("if [ \"$DASHBOARD_CHANGED\" = true ]", start)
    block = script[start:end]
    assert "DASHBOARD_CHANGED=false" in block
    assert "DASHBOARD_CHANGED=true" not in block


def test_runner_scripts_stay_byte_identical():
    assert _read("pipeline-runner.sh") == _read("pipeline-runner.sh.local")
