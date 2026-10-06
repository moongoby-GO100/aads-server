"""AADS-RUNNER-REVIEW-DIFF-TRUNCATION-PRESERVATION-FP — 상류 절단 diff 가 보존 게이트를 속이지 않는다.

배경(2026-10-06, GO100 runner-36797f75 / code_reviews 74d67e3d): 전체 diff 128,134B 중
`-def main() -> None:` 은 42,736B, `+def main() -> int:` 는 63,191B 에 있었다. 러너가 앞
45,000B 만 보내 `+def main` 을 못 본 게이트가 main 을 "삭제" 로 FLAG 했고 측정값은
diff_truncated=false 였다. 오류 사전 키: reviewer.upstream_diff_truncation_false_symbol_delete
"""

import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[2]
CUT = 50_000


def _load_reviewer():
    module_name = "code_reviewer_upstream_trunc_under_test"
    spec = importlib.util.spec_from_file_location(
        module_name, ROOT / "app" / "services" / "code_reviewer.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


cr = _load_reviewer()

APPROVE_JSON = json.dumps({
    "verdict": "APPROVE", "correctness": 0.9, "security": 0.9, "scope_compliance": 0.9,
    "preservation": 0.9, "quality": 0.9, "issues": [], "summary": "ok",
})


def _big_file_section(path: str = "scripts/go100/sync.py", *, return_added: bool = True) -> str:
    """-def main 이 앞, +def main 이 60,000자 뒤에 있는 한 파일 구간(반환형만 바뀜)."""
    added = [f"+    step_{i} = {i}  # 새 코드" for i in range(2500)]
    body = ["-def main() -> None:"] + added + (["+def main() -> int:"] if return_added else [])
    new_count = len(added) + (1 if return_added else 0)
    return (
        f"diff --git a/{path} b/{path}\n"
        "index 1111111..2222222 100644\n"
        f"--- a/{path}\n+++ b/{path}\n"
        f"@@ -10,1 +10,{new_count} @@\n" + "\n".join(body) + "\n"
    )


def _tail_file_section(path: str = "app/tail.py") -> str:
    return (
        f"diff --git a/{path} b/{path}\n"
        "index 3333333..4444444 100644\n"
        f"--- a/{path}\n+++ b/{path}\n"
        "@@ -1,1 +1,2 @@\n context\n+extra\n"
    )


def _full_diff() -> str:
    full = _big_file_section() + _tail_file_section()
    assert len(full) > CUT + 10_000
    assert full.index("-def main") < CUT < full.index("+def main")
    return full


def _runner_style_truncated(full: str, stat_files: list[str]) -> str:
    """scripts/pipeline-runner.sh build_review_diff_prefix + head -c 절단과 같은 모양."""
    stat = "\n".join(f" {path} | 10 +++++-----" for path in stat_files)
    prefix = (
        f"[DIFF TRUNCATED] 전체 {len(full.encode())}B 중 앞 {CUT}B 만 아래에 포함됨. 아래 diff 에 특정 파일이 "
        "보이지 않는 것은 \"그 파일이 없다\"는 뜻이 아니다.\n"
        f"DIFFSTAT (전체 변경 파일 — 절단 없음)\n{stat}\n=== DIFF (원문 시작) ===\n"
    )
    return prefix + full[:CUT]


def _symbols(verdict) -> list[str]:
    return list(verdict.feedback.get("deleted_symbols") or [])


# ── ① -def 가 앞, +def 가 50,000자 뒤 ──────────────────────────────────────────


def test_full_diff_with_late_plus_def_is_not_a_symbol_deletion():
    assert cr._precheck_preservation_gate(_full_diff(), "지시", None, None, "GO100") is None


def test_old_behavior_on_cut_diff_was_false_delete_and_is_still_visible_without_flag():
    """절단 사실을 모르면(diff_truncated 미지정) 종전 오판이 그대로 난다 — 수정 전 재현."""
    cut = _full_diff()[:CUT]
    verdict = cr._precheck_preservation_gate(cut, "지시", None, None, "GO100")
    assert verdict is not None and verdict.verdict == "FLAG"
    assert "def main" in _symbols(verdict)


def test_cut_diff_marked_truncated_defers_symbol_instead_of_hard_flag():
    cut = _full_diff()[:CUT]
    notes: dict = {}
    verdict = cr._precheck_preservation_gate(
        cut, "지시", None, notes, "GO100", diff_truncated=True
    )
    assert verdict is None
    assert notes["preservation_unverified_symbols"] == ["def main"]
    assert notes["preservation_unverified_reason"] == "diff_truncated"


# ── ② 진짜 삭제는 여전히 FLAG ──────────────────────────────────────────────────


def _true_delete_section(path: str = "app/gone.py") -> str:
    return (
        f"diff --git a/{path} b/{path}\n"
        "index 5555555..6666666 100644\n"
        f"--- a/{path}\n+++ b/{path}\n"
        "@@ -1,4 +1,2 @@\n keep = 1\n-def helper():\n-    return 1\n tail = 2\n"
    )


def test_true_deletion_in_full_diff_is_still_flagged():
    full = _true_delete_section() + _big_file_section()
    verdict = cr._precheck_preservation_gate(full, "지시", None, None, "GO100")
    assert verdict is not None and verdict.verdict == "FLAG"
    assert _symbols(verdict) == ["def helper"]


def test_true_deletion_in_completely_visible_section_stays_flag_when_diff_truncated():
    cut = (_true_delete_section() + _big_file_section())[:CUT]
    notes: dict = {}
    verdict = cr._precheck_preservation_gate(
        cut, "지시", None, notes, "GO100", diff_truncated=True
    )
    assert verdict is not None and verdict.verdict == "FLAG"
    assert _symbols(verdict) == ["def helper"]
    assert notes["preservation_unverified_symbols"] == ["def main"]


def test_true_deletion_without_plus_def_is_flagged_even_in_cut_through_section_of_full_diff():
    full = _big_file_section(return_added=False) + _tail_file_section()
    verdict = cr._precheck_preservation_gate(full, "지시", None, None, "GO100")
    assert verdict is not None and "def main" in _symbols(verdict)


# ── ③ 범위 게이트는 전체 파일 목록 기준 ────────────────────────────────────────


SCOPE = "허용 파일: scripts/go100/sync.py"


def test_scope_gate_sees_files_beyond_the_cut_via_extra_files():
    cut = _full_diff()[:CUT]
    assert "app/tail.py" not in cut
    verdict = cr._precheck_preservation_gate(
        cut, SCOPE, ["scripts/go100/sync.py"], None, "GO100",
        diff_truncated=True, extra_changed_files=["scripts/go100/sync.py", "app/tail.py"],
    )
    assert verdict is not None and verdict.verdict == "REQUEST_CHANGES"
    assert verdict.feedback["out_of_scope_files"] == ["app/tail.py"]


def test_scope_gate_on_full_diff_lists_all_files():
    verdict = cr._precheck_preservation_gate(
        _full_diff(), SCOPE, None, None, "GO100"
    )
    assert verdict is not None
    assert verdict.feedback["out_of_scope_files"] == ["app/tail.py"]


def test_upstream_diffstat_paths_are_parsed_and_shortened_paths_dropped():
    full = _full_diff()
    text = _runner_style_truncated(full, ["scripts/go100/sync.py", "app/tail.py"])
    text = text.replace(" app/tail.py |", " .../cut/long.py |").replace(
        "=== DIFF", " a => b | 1 +\n=== DIFF"
    )
    assert cr._extract_upstream_diffstat_files(text) == ["scripts/go100/sync.py"]
    assert cr._extract_upstream_diffstat_files(full) == []


# ── ④ 측정값 diff_truncated 가 실제 절단과 일치 ───────────────────────────────


def _run_review(diff: str, **kwargs):
    async def run():
        call_model = AsyncMock(return_value=APPROVE_JSON)
        with patch.object(cr, "_get_review_models", new=AsyncMock(return_value=["codex:gpt-5.6-luna"])), \
                patch.object(cr, "_call_review_model", new=call_model), \
                patch.object(cr, "_save_review_result", new=AsyncMock()):
            verdict = await cr.review_code_diff(
                project=kwargs.pop("project", "GO100"), job_id="runner-test-trunc",
                diff=diff, instruction=kwargs.pop("instruction", "지시"), **kwargs,
            )
        return verdict, call_model

    return asyncio.run(run())


def test_measurement_is_not_truncated_for_small_diff():
    small = _tail_file_section()
    verdict, _ = _run_review(small, files_changed=["app/tail.py"])
    measurement = verdict.feedback["measurement"]
    assert measurement["diff_truncated"] is False
    assert "diff_original_chars" not in measurement
    assert "diff_truncation_source" not in measurement


def test_measurement_records_upstream_truncation_and_original_size():
    full = _full_diff()
    text = _runner_style_truncated(full, ["scripts/go100/sync.py", "app/tail.py"])
    verdict, call_model = _run_review(
        text, files_changed=["scripts/go100/sync.py"], instruction="지시"
    )
    measurement = verdict.feedback["measurement"]
    assert measurement["diff_truncated"] is True
    assert measurement["diff_truncation_source"] == "upstream_prefix"
    assert measurement["diff_original_chars"] == len(full.encode())
    assert measurement["diff_chars"] == len(text)
    # 하드 FLAG 가 아니라 LLM 검수로 넘어가고, 판정 불가가 이슈·프롬프트에 남는다.
    assert verdict.verdict == "APPROVE"
    assert any("판정 불가(truncated)" in issue and "def main" in issue for issue in verdict.issues)
    assert verdict.feedback["preservation_unverified_symbols"] == ["def main"]
    prompt = call_model.await_args.kwargs["prompt"]
    assert "판정 불가" in prompt and "def main" in prompt


def test_upstream_truncated_diff_is_scope_checked_against_diffstat_files():
    full = _full_diff()
    text = _runner_style_truncated(full, ["scripts/go100/sync.py", "app/tail.py"])
    verdict, call_model = _run_review(
        text, files_changed=["scripts/go100/sync.py"], instruction=SCOPE
    )
    assert verdict.verdict == "REQUEST_CHANGES"
    assert verdict.feedback["out_of_scope_files"] == ["app/tail.py"]
    call_model.assert_not_awaited()


def test_full_diff_gate_uses_whole_diff_and_llm_prompt_is_cut_for_review_only():
    full = _full_diff()
    verdict, call_model = _run_review(
        full[:CUT], full_diff=full, files_changed=["scripts/go100/sync.py", "app/tail.py"]
    )
    assert verdict.verdict == "APPROVE"
    assert "preservation_unverified_symbols" not in verdict.feedback
    measurement = verdict.feedback["measurement"]
    assert measurement["diff_truncated"] is False
    assert measurement["diff_chars"] == len(full)
    prompt = call_model.await_args.kwargs["prompt"]
    assert "+def main() -> int:" in prompt  # 전체 diff 가 (상한 이내라) 프롬프트에 그대로 간다


def test_full_diff_over_review_limit_marks_review_input_truncation():
    full = _full_diff()
    with patch.object(cr, "REVIEW_DIFF_MAX_CHARS", 20_000):
        verdict, call_model = _run_review(full[:CUT], full_diff=full)
    measurement = verdict.feedback["measurement"]
    assert measurement["diff_truncated"] is True
    assert measurement["diff_truncation_source"] == "review_input"
    assert measurement["diff_original_chars"] == len(full)
    # 게이트는 전체 diff 로 돌았으므로 +def main 을 봤다 — 판정 불가 이슈가 없다.
    assert not any("판정 불가" in issue for issue in verdict.issues)
    assert "[절단 고지]" in call_model.await_args.kwargs["prompt"]


def test_full_diff_that_hit_the_service_cap_is_flagged_truncated_with_original_length():
    full = _full_diff()
    cut_full = full[:CUT]
    verdict, _ = _run_review(
        cut_full, full_diff=cut_full, diff_truncated=True, original_diff_chars=2_500_000,
        files_changed=["scripts/go100/sync.py"],
    )
    measurement = verdict.feedback["measurement"]
    assert measurement["diff_truncated"] is True
    assert measurement["diff_truncation_source"] == "full_diff_cap"
    assert measurement["diff_original_chars"] == 2_500_000
    assert verdict.verdict == "APPROVE"
    assert verdict.feedback["preservation_unverified_symbols"] == ["def main"]


# ── 구간 완전성 판정 ──────────────────────────────────────────────────────────


def test_section_completeness_follows_hunk_line_counts():
    section = _big_file_section()
    assert cr._section_is_complete(section) is True
    assert cr._section_is_complete(section[:CUT]) is False
    assert cr._section_is_complete(_tail_file_section()) is True


def test_untruncated_diff_has_no_incomplete_sections():
    full = _full_diff()
    complete, incomplete = cr._split_diff_by_completeness(full, truncated=False)
    assert complete == full and incomplete == ""
    complete, incomplete = cr._split_diff_by_completeness(full[:CUT], truncated=True)
    assert "-def main" in incomplete and complete == ""
