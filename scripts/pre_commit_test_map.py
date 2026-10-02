#!/usr/bin/env python3
"""pre-commit Step 3 — 스테이징된 변경 → 실행할 단위 테스트 묶음.

왜 있는가
  2026-09-29 까지 게이트는 tests/unit/test_tools_and_pipeline.py 한 파일만,
  그것도 도구 관련 .py 가 바뀔 때만 돌렸다. 배포·러너 코드만 고친 커밋은
  테스트를 한 건도 돌리지 않고 초록으로 통과했다. 그 틈에서
  test_pipeline_runner_deploy_lock_requeue.py 가 계속 실패했는데 아무도 몰랐고,
  runner-104b2fda 가 CEO 승인까지 끝난 뒤 deploy_lock_fail 로 산출물을 잃었다.

대응을 추가할 때는 아래 GROUPS 표 한 곳만 고친다 — hook 본문은 건드리지 않는다.
여기에 넣는 테스트는 main 에서 통과하고 있어야 한다. 실패 중인 테스트를 연결하면
그 묶음에 걸리는 모든 커밋이 막힌다.

사용
  git diff --cached --name-only | python3 scripts/pre_commit_test_map.py          # 선택만 출력
  git diff --cached --name-only | python3 scripts/pre_commit_test_map.py --run    # 선택 + 실행

종료코드(--run): 0 통과 또는 대응 없음 / 1 테스트 실패 / 2 실행 불가.
여러 묶음 중 하나라도 2 면 2, 그렇지 않고 하나라도 실패면 1 — 둘 다 hook 이 차단한다.
"""
from __future__ import annotations

import fnmatch
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Sequence

REPO = Path(__file__).resolve().parents[1]
RUNNER = ("bash", "scripts/run_unit_tests.sh")
# 한 묶음이 이보다 오래 걸리면 게이트가 멈춘 것으로 보고 실행 불가(2)로 처리한다(R-BG).
GROUP_TIMEOUT_SEC = 900


@dataclass(frozen=True)
class Group:
    name: str
    tests: tuple[str, ...]
    globs: tuple[str, ...] = ()
    # 예전 hook 의 TOOL_FILES_PATTERN 호환 — .py 경로에 부분 문자열로 건다.
    py_regex: str = ""

    def matches(self, path: str) -> bool:
        if path in self.tests:
            return True
        if any(fnmatch.fnmatchcase(path, g) for g in self.globs):
            return True
        return bool(self.py_regex and path.endswith(".py") and re.search(self.py_regex, path))


# ── 대응표 ────────────────────────────────────────────────────────────
GROUPS: tuple[Group, ...] = (
    Group(
        name="tools",
        tests=("tests/unit/test_tools_and_pipeline.py",),
        py_regex=r"tool_executor|tool_registry|ceo_chat_tools|chat_tools|model_selector|system_prompt|test_tools",
    ),
    Group(
        name="model_selector_codex_route",
        tests=("tests/unit/test_model_selector_codex_db_route.py",),
        py_regex=r"app/services/model_selector\.py",
    ),
    Group(
        name="chat_stall_codex_auth_fallback",
        tests=(
            "tests/unit/test_chat_stall_codex_auth_fallback.py",
            "tests/unit/test_chat_retry_model_switch.py",
        ),
        py_regex=r"app/services/(chat_service|model_selector)\.py",
    ),
    Group(
        name="cost_catalog",
        tests=(
            "tests/unit/test_oauth_usage_catalog_model_normalize.py",
            "tests/unit/test_llm_cost_basis.py",
        ),
        globs=(
            "app/services/oauth_usage_tracker.py",
            "app/services/llm_cost_basis.py",
            "scripts/runner_cli_usage.py",
            "migrations/20260930_oauth_usage_cost_basis.sql",
            "migrations/20260930_oauth_usage_catalog_model_normalize_backfill.sql",
            "migrations/rollback/20260930_oauth_usage_*.down.sql",
        ),
    ),
    Group(
        name="cli_model_autoreg",
        tests=(
            "tests/unit/test_cli_model_autoreg.py",
            "tests/unit/test_cli_model_autoreg_chat_llm.py",
        ),
        globs=(
            "app/services/cli_model_autoreg.py",
            "app/api/llm_models.py",
        ),
    ),
    Group(
        name="llm_quality",
        tests=("tests/unit/test_llm_model_quality.py",),
        globs=(
            "app/services/llm_model_quality.py",
            "app/api/ops.py",
        ),
    ),
    Group(
        name="ops_slot_projects",
        tests=("tests/unit/test_ops_oauth_slot_projects_accounts.py",),
        globs=("app/api/ops.py",),
    ),
    Group(
        name="runner",
        tests=(
            "tests/unit/test_pipeline_runner_deploy_lock_requeue.py",
            "tests/unit/test_pipeline_runner_shell_deploy_lock_requeue.py",
            "tests/unit/test_pipeline_runner_script_guards.py",
            "tests/unit/test_pipeline_runner_model_cycle_executable_guard.py",
            "tests/unit/test_pipeline_runner_worktree_policy.py",
            "tests/unit/test_pipeline_runner_autodep_release.py",
            "tests/unit/test_pipeline_runner_deploy_gate_stale_chain.py",
            "tests/unit/test_pipeline_runner_push_stale_base.py",
            "tests/unit/test_pipeline_runner_deploy_only_approval_commit.py",
            "tests/unit/test_pipeline_runner_deploy_only_job_path.py",
            "tests/unit/test_pipeline_runner_approval_sha_stdout_contract.py",
            "tests/unit/test_pipeline_runner_rejected_artifact_preserve.py",
            "tests/unit/test_pipeline_runner_approval_patchid_inherit.py",
            "tests/unit/test_pipeline_runner_rebase_requeue_guard.py",
            "tests/unit/test_pipeline_runner_autoheal_successor_follow.py",
            "tests/unit/test_codex_token_revoked_detect.py",
            "tests/unit/test_deploy_lock_renew.py",
            "tests/unit/test_stale_approval_trigger_guard.py",
            "tests/unit/test_pipeline_runner_deploy_preflight_ffonly_scoped.py",
        ),
        globs=(
            "app/services/pipeline_runner_service.py",
            "app/services/deploy_lock.py",
            "app/api/pipeline_runner.py",
            "scripts/pipeline-runner.sh",
            "scripts/pipeline-runner.sh.local",
        ),
    ),
    Group(
        name="codex_auth_revoked",
        tests=("tests/unit/test_codex_token_revoked_detect.py",),
        globs=(
            "scripts/codex_usage.py",
            "scripts/materialize_codex_accounts.py",
            "migrations/20261001_codex_usage_snapshots_auth_revoked.sql",
            "migrations/rollback/20261001_codex_usage_snapshots_auth_revoked.down.sql",
        ),
    ),
    Group(
        name="deploy",
        tests=(
            "tests/unit/test_deploy_build_guards.py",
            "tests/unit/test_deploy_dependency_image_contract.py",
            "tests/unit/test_deploy_autoheal.py",
            "tests/unit/test_deploy_stream_reconcile.py",
            "tests/unit/test_deploy_terminal_state_contract.py",
            "tests/unit/test_deploy_ancestor_release_guard.py",
        ),
        globs=(
            "deploy.sh",
            "scripts/deploy.sh",
            "scripts/deploy*",
            "scripts/classify_deploy_streams.py",
            "scripts/verify-bluegreen-release-contract.sh",
            "scripts/compile_requirements.sh",
            "scripts/prune_aads_images.sh",
            "scripts/start_aads_deploy_queue_worker.sh",
        ),
    ),
    Group(
        name="hooks",
        tests=(
            "tests/unit/test_dup_guard.py",
            "tests/unit/test_pre_commit_test_map.py",
        ),
        globs=(
            "scripts/hooks/*",
            "scripts/dup_guard.py",
            "scripts/pre_commit_test_map.py",
            "scripts/run_unit_tests.sh",
        ),
    ),
)


def select(paths: Iterable[str], groups: Sequence[Group] | None = None) -> list[tuple[Group, list[str], list[str]]]:
    """스테이징 경로 → [(묶음, 있는 테스트, 없는 테스트)]. 대응 없는 묶음은 빠진다."""
    groups = GROUPS if groups is None else groups
    staged = [p.strip() for p in paths if p.strip()]
    out = []
    for g in groups:
        if not any(g.matches(p) for p in staged):
            continue
        present = [t for t in g.tests if (REPO / t).is_file()]
        missing = [t for t in g.tests if t not in present]
        out.append((g, present, missing))
    return out


def _default_runner(tests: Sequence[str]) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            [*RUNNER, *tests], cwd=REPO, capture_output=True, text=True, timeout=GROUP_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired:
        return 2, "[test-gate] %d초 안에 끝나지 않았습니다 — 실행 불가로 봅니다" % GROUP_TIMEOUT_SEC
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def combine(rcs: Iterable[int]) -> int:
    """하나라도 2 → 2(실행 불가), 아니면 하나라도 0 이 아니면 1, 전부 0 이면 0."""
    rcs = list(rcs)
    if any(rc == 2 for rc in rcs):
        return 2
    if any(rc != 0 for rc in rcs):
        return 1
    return 0


def run(
    paths: Iterable[str],
    runner: Callable[[Sequence[str]], tuple[int, str]] = _default_runner,
    log: Callable[[str], None] = print,
) -> int:
    chosen = select(paths)
    if not chosen:
        log("[pre-commit] 테스트 대응 파일 미변경 — 단위 테스트 생략")
        return 0
    rcs = []
    for g, present, missing in chosen:
        for t in missing:
            log("  ⚠️ [test-gate] %s 묶음의 테스트가 리포에 없습니다: %s" % (g.name, t))
        if not present:
            continue
        log("[pre-commit] 단위 테스트 [%s] 실행: %s" % (g.name, " ".join(present)))
        rc, out = runner(present)
        if rc == 2:
            log("  ❌ [%s] 단위 테스트를 실행하지 못했습니다 — 게이트가 아무것도 검증하지 못하므로 차단합니다" % g.name)
            for line in out.strip().splitlines()[-5:]:
                log("     " + line)
        elif rc != 0:
            m = re.search(r"\d+ failed", out)
            log("  ❌ [%s] 테스트 실패: %s" % (g.name, m.group(0) if m else "rc=%d" % rc))
            for line in [ln for ln in out.splitlines() if "FAILED" in ln][:5]:
                log("     " + line)
        else:
            m = re.search(r"\d+ passed", out)
            log("  ✅ [%s] 테스트 통과: %s" % (g.name, m.group(0) if m else ""))
        rcs.append(rc)
    return combine(rcs)


def main(argv: Sequence[str]) -> int:
    paths = sys.stdin.read().splitlines()
    if "--run" in argv:
        return run(paths, log=lambda s: print(s, flush=True))
    for g, present, missing in select(paths):
        for t in missing:
            print("[test-gate] %s 묶음의 테스트가 리포에 없습니다: %s" % (g.name, t), file=sys.stderr)
        for t in present:
            print("%s\t%s" % (g.name, t))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
