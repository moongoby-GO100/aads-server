"""중복 재적용 패치 탐지기(scripts/dup_guard.py) 단위 테스트.

무엇을 지키려는 테스트인가
  1. 2026-09-15 에 실제로 운영을 깨뜨린 모양을 잡는다 —
     INSERT 컬럼 2회 나열(PostgreSQL 'specified more than once'),
     동일 함수/상수 재정의, 블록 통째 재적용.
  2. 정상 코드를 막지 않는다. @property/@setter 쌍, 셸 변수 재대입,
     짧은 보일러플레이트 반복은 중복이 아니다 — 막히는 게이트는 곧 우회된다.
  3. hook 이 실제로 이 검사를 부르고, 우회 경로가 명시돼 있다.

이 파일은 깨진 SQL 과 중복 정의를 일부러 담고 있어서, 검사 대상이 되면
자기 자신의 커밋을 막는다. 그래서 검사에서 제외한다 — dup-guard: ignore-file
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_MODULE_PATH = _REPO / "scripts" / "dup_guard.py"
_spec = importlib.util.spec_from_file_location("dup_guard", _MODULE_PATH)
assert _spec and _spec.loader
dup = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dup)


def _kinds(path: str, text: str):
    return {f.kind for f in dup.check_text(path, text)}


# ── 실제 사고 모양 ────────────────────────────────────────────────────


def test_duplicate_insert_columns_are_detected():
    """a9603307 이 되돌린 그 모양. 컬럼이 두 번 나열되면 INSERT 자체가 거절된다."""
    sql = '''
        await conn.execute("""
            INSERT INTO deploy_runs
              (project, sha, status, current_slot, candidate_slot, current_slot, candidate_slot)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
        """)
    '''
    found = dup.check_sql("scripts/sync_external_deploy_ledger.py", sql)
    idents = {f.ident for f in found}
    assert "deploy_runs.current_slot" in idents
    assert "deploy_runs.candidate_slot" in idents


def test_duplicate_update_set_target_is_detected():
    sql = "UPDATE deploy_runs SET status=$1, current_slot=$2, current_slot=$3 WHERE id=$4"
    found = dup.check_sql("x.py", sql)
    assert {f.ident for f in found} == {"SET.current_slot"}


def test_clean_insert_is_not_flagged():
    sql = "INSERT INTO deploy_runs (project, sha, status, current_slot) VALUES ($1,$2,$3,$4)"
    assert dup.check_sql("x.py", sql) == []


def test_duplicate_python_function_is_detected():
    src = "def resolve_slots(a):\n    return a\n\n\ndef resolve_slots(a):\n    return a\n"
    found = dup.check_python("scripts/x.py", src)
    assert [f.kind for f in found] == ["py-dup-def"]
    assert found[0].ident == "resolve_slots"


def test_identical_module_constant_assigned_twice_is_detected():
    """_REVIEW_ASYNC_DEADLINE_SEC 가 같은 값으로 두 번 정의됐던 그 모양(b5a03b9e)."""
    src = (
        '_DEADLINE = int(os.environ.get("X", "240"))\n'
        '_DEADLINE = int(os.environ.get("X", "240"))\n'
    )
    found = dup.check_python("app/services/code_reviewer.py", src)
    assert [f.kind for f in found] == ["py-dup-assign"]


def test_reassignment_with_different_value_is_not_flagged():
    """값이 다르면 재대입이다 — 중복 적용이 아니다."""
    src = 'MODE = "a"\nMODE = os.environ.get("MODE", "b")\n'
    assert dup.check_python("x.py", src) == []


def test_property_setter_pair_is_not_flagged():
    src = (
        "class A:\n"
        "    @property\n"
        "    def value(self):\n"
        "        return self._v\n"
        "\n"
        "    @value.setter\n"
        "    def value(self, v):\n"
        "        self._v = v\n"
    )
    assert dup.check_python("x.py", src) == []


def test_duplicate_shell_function_and_config_are_detected():
    src = (
        'SWEEP_INFRA_CIRCUIT="${SWEEP_INFRA_CIRCUIT:-3}"\n'
        'SWEEP_INFRA_CIRCUIT="${SWEEP_INFRA_CIRCUIT:-3}"\n'
        "infra_retry() {\n    echo a\n}\n"
        "infra_retry() {\n    echo a\n}\n"
    )
    assert _kinds("scripts/review-hold-sweeper.sh", src) == {"sh-dup-func", "sh-dup-assign"}


def test_shell_counter_reassignment_is_not_flagged():
    src = "total=0\nconsec_infra=0\ntotal=$((total + 1))\nconsec_infra=0\n"
    assert dup.check_shell("x.sh", src) == []


def test_ignore_file_marker_disables_checks():
    """검사 자체의 픽스처처럼 일부러 깨진 코드를 담은 파일을 위한 탈출구."""
    src = ("# dup-guard: ignore-file (일부러 깨뜨린 픽스처)\n"
           "def foo():\n    pass\n\n\ndef foo():\n    pass\n")
    assert dup.check_text("x.py", src) == []
    assert dup.check_python("x.py", src) != [], "표시가 없는 경로에서는 그대로 잡혀야 한다"


# ── 블록 재적용 그물 ──────────────────────────────────────────────────


def _block(prefix: str, count: int) -> str:
    return "".join("%s_step_%d(value_%d)\n" % (prefix, i, i) for i in range(count))


def test_long_repeated_block_is_detected():
    text = _block("run", dup.BLOCK_MIN_LINES) + _block("run", dup.BLOCK_MIN_LINES)
    found = dup.check_dup_block("x.py", text)
    assert len(found) == 1
    assert "%d줄" % dup.BLOCK_MIN_LINES in found[0].detail


def test_repeated_block_is_reported_once_not_per_window():
    """창을 한 칸씩 밀며 같은 구간을 다시 잡으면 보고가 수십 건이 된다."""
    text = _block("run", dup.BLOCK_MIN_LINES + 20) + _block("run", dup.BLOCK_MIN_LINES + 20)
    assert len(dup.check_dup_block("x.py", text)) == 1


def test_short_boilerplate_repeat_is_not_flagged():
    """임계값 아래 반복은 막지 않는다 — 오탐 0 으로 맞춘 캘리브레이션 결과."""
    short = dup.BLOCK_MIN_LINES - 4
    text = _block("run", short) + _block("other", 14) + _block("run", short)
    assert dup.check_dup_block("x.py", text) == []


# ── hook 배선 ─────────────────────────────────────────────────────────


def test_pre_commit_hook_runs_dup_guard_and_blocks():
    hook = (_REPO / "scripts" / "hooks" / "pre-commit").read_text(encoding="utf-8")
    assert "scripts/dup_guard.py" in hook
    assert "ALLOW_DUP_COMMIT" in hook
    assert "ruff 미설치" in hook, "ruff 없이 조용히 건너뛰던 단계는 보이게 남아야 한다"


def test_commit_msg_hook_blocks_repeated_subject_but_allows_amend():
    hook = (_REPO / "scripts" / "hooks" / "commit-msg").read_text(encoding="utf-8")
    assert "ALLOW_DUP_COMMIT" in hook
    assert "--amend" in hook, "amend 는 제목이 같은 것이 정상이므로 제외해야 한다"
    assert "--since='3 days ago'" in hook


def test_installed_hooks_stay_synced_with_repo_copies():
    """저장소본만 고치고 설치본을 두면 게이트는 여전히 옛 코드로 돈다."""
    installed_dir = _REPO / ".git" / "hooks"
    if not installed_dir.is_dir():
        return  # 컨테이너 마운트 등 .git 이 없는 환경
    for name in ("pre-commit", "commit-msg"):
        installed = installed_dir / name
        if not installed.exists():
            continue
        assert installed.read_text(encoding="utf-8") == \
            (_REPO / "scripts" / "hooks" / name).read_text(encoding="utf-8"), \
            "%s: 설치본과 저장소본이 다릅니다 — cp scripts/hooks/%s .git/hooks/" % (name, name)
