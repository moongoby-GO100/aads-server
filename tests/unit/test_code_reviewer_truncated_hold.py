"""AADS-RUNNER-REVIEW-TRUNCATED-DIFF-HOLD — diff 절단만이 사유인 반려는 review_hold 로 보낸다.

판별은 단어 목록이 아니라 리뷰어가 이슈마다 적는 구조화 필드(issues[].basis)와
러너가 남기는 절단 표지(`# [TRUNCATED] 상한 N바이트`)로만 한다.
"""

import asyncio
import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


cr = _load("code_reviewer_truncated_hold_under_test", "app/services/code_reviewer.py")

MARKER = "# [TRUNCATED] 상한 45000바이트 — 생략된 파일: app/omitted_a.py, app/omitted_b.py"
OMITTED = {"basis": "omitted_region", "text": "app/omitted_a.py 가 diff 에 없어 확인할 수 없다"}


def _section(path: str = "app/shown.py", added: int = 3) -> str:
    body = "\n".join(f"+line_{i} = {i}" for i in range(added))
    return (
        f"diff --git a/{path} b/{path}\nindex 1111111..2222222 100644\n"
        f"--- a/{path}\n+++ b/{path}\n@@ -1,1 +1,{added + 1} @@\n context\n{body}\n"
    )


def _marked_diff() -> str:
    return _section() + MARKER


def _response(issues, *, needs_retry=True, score=0.55) -> str:
    return json.dumps({
        "verdict": "REQUEST_CHANGES", "correctness": score, "security": score,
        "scope_compliance": score, "preservation": score, "quality": score,
        "needs_retry": needs_retry, "issues": issues, "summary": "요약",
    }, ensure_ascii=False)


def _run(diff: str, response: str, **kwargs):
    async def go():
        call_model = AsyncMock(return_value=response)
        with patch.object(cr, "_get_review_models", new=AsyncMock(return_value=["codex:gpt-5.6-luna"])), \
                patch.object(cr, "_call_review_model", new=call_model), \
                patch.object(cr, "_save_review_result", new=AsyncMock()):
            verdict = await cr.review_code_diff(
                project="GO100", job_id="runner-test-trunc-hold", diff=diff,
                instruction="지시", **kwargs,
            )
        return verdict, call_model

    return asyncio.run(go())


def _assert_held(verdict):
    assert verdict.verdict == "FLAG"
    assert verdict.flag_category == "REVIEW_DIFF_TRUNCATED"
    assert verdict.failure_stage == "review_diff_truncated"
    assert verdict.needs_retry is False
    assert verdict.feedback["flag_category"] == "REVIEW_DIFF_TRUNCATED"


def _assert_rejected(verdict):
    assert verdict.verdict == "REQUEST_CHANGES"
    assert verdict.flag_category != "REVIEW_DIFF_TRUNCATED"


# (a) 표지 + 전 이슈 omitted_region → 보류
def test_marker_and_all_omitted_region_becomes_truncated_hold():
    verdict, _ = _run(_marked_diff(), _response([OMITTED, dict(OMITTED, text="b 도 안 보임")]))
    _assert_held(verdict)
    assert verdict.issues == [OMITTED["text"], "b 도 안 보임"]  # 하위 소비자용 평문
    assert verdict.feedback["issues_structured"][0]["basis"] == "omitted_region"


# (b) 하나라도 visible_code 면 반려
def test_one_visible_code_issue_keeps_request_changes():
    issues = [OMITTED, {"basis": "visible_code", "text": "app/shown.py line_1 이 정의되지 않았다"}]
    verdict, _ = _run(_marked_diff(), _response(issues))
    _assert_rejected(verdict)


# (c) basis 없음 / 구형 문자열 → 반려
@pytest.mark.parametrize("issues", [
    [{"text": "basis 필드 없음"}],
    ["구형 문자열 이슈 — 절단되어 확인 불가"],
    [OMITTED, "구형 문자열이 섞임"],
    [{"basis": 3, "text": "문자열이 아닌 basis"}],
    [{"basis": "unknown_value", "text": "알 수 없는 basis"}],
    [],
])
def test_missing_or_legacy_basis_is_not_normalized(issues):
    verdict, _ = _run(_marked_diff(), _response(issues))
    _assert_rejected(verdict)


# (d) 리뷰 반례: 절단 문구 + 구체 결함이 basis=visible_code 로 오면 반려
def test_review_counterexample_sentence_with_visible_code_basis_is_rejected():
    issues = [{
        "basis": "visible_code",
        "text": "diff가 절단되어 확인 불가: a.py foo()는 입력 1에 기대값 1 대신 2를 반환한다",
    }]
    verdict, _ = _run(_marked_diff(), _response(issues))
    _assert_rejected(verdict)


def test_decision_does_not_depend_on_the_words_in_the_sentence():
    """절단 문구가 없는 문장이어도 basis=omitted_region 이면 보류 — 단어 목록으로 판별하지 않는다."""
    verdict, _ = _run(_marked_diff(), _response([{"basis": "omitted_region", "text": "foo() 반환값을 알 수 없음"}]))
    _assert_held(verdict)


# (e) 표지 없음 → 정규화 안 함
def test_without_marker_nothing_is_normalized():
    verdict, _ = _run(_section(), _response([OMITTED]))
    _assert_rejected(verdict)


def test_needs_retry_false_is_not_normalized():
    verdict, _ = _run(_marked_diff(), _response([OMITTED], needs_retry=False))
    _assert_rejected(verdict)


def test_marker_must_start_the_line_so_diff_content_cannot_forge_it():
    forged = _section() + "+# [TRUNCATED] 상한 45000바이트 — 생략된 파일: x\n"
    assert cr._has_diff_truncation_marker(forged) is False
    assert cr._has_diff_truncation_marker(_marked_diff()) is True
    verdict, _ = _run(forged, _response([OMITTED]))
    _assert_rejected(verdict)


# (f) 스위퍼 재전송: DB git_diff(≈45KB, 앞머리 고지 없음)도 표지로 was_truncated=True
def test_sweeper_resend_of_stored_diff_is_truncated_by_marker():
    big = _section(added=2500)
    assert 40_000 < len(big) <= 50_000 and "[DIFF TRUNCATED]" not in big
    stored = big + MARKER
    verdict, call_model = _run(stored, _response([OMITTED]))
    _assert_held(verdict)
    measurement = verdict.feedback["measurement"]
    assert measurement["diff_truncated"] is True
    assert measurement["diff_truncation_source"] == "diff_marker"
    assert "[절단 고지]" in call_model.await_args.kwargs["prompt"]


def test_untruncated_stored_diff_has_no_truncation_measurement():
    verdict, _ = _run(_section(), _response([OMITTED]))
    assert verdict.feedback["measurement"]["diff_truncated"] is False


# (g) 삭제 줄만 보이고 재정의가 잘린 절단 diff — 보존 게이트가 조기 반환하지 않고 정규화까지 간다
def _deletion_only_marked_diff() -> str:
    return (
        "diff --git a/app/core.py b/app/core.py\nindex 1111111..2222222 100644\n"
        "--- a/app/core.py\n+++ b/app/core.py\n@@ -10,4 +10,0 @@\n"
        "-def compute_total(items):\n-    return sum(items)\n-\n-# end\n"
        + MARKER
    )


def test_preservation_gate_does_not_return_early_on_deletion_only_truncated_diff():
    diff = _deletion_only_marked_diff()
    # 대조: 표지가 없으면 종전대로 "추가 없이 삭제만" 하드 게이트가 즉시 반환한다(LLM 미호출).
    verdict, call_model = _run(diff.replace(MARKER, ""), _response([OMITTED]))
    assert verdict.flag_category == "PRESERVATION_HARD_GATE"
    call_model.assert_not_awaited()
    # 표지가 있으면 게이트가 보류하고 LLM·정규화까지 도달한다.
    verdict, call_model = _run(diff, _response([OMITTED]))
    call_model.assert_awaited_once()
    _assert_held(verdict)


def test_gate_line_check_is_unverified_when_truncated_and_exposes_visible_counts():
    notes: dict = {}
    result = cr._precheck_preservation_gate(
        _deletion_only_marked_diff(), "지시", None, notes, "GO100", diff_truncated=True)
    assert result is None
    assert notes["preservation_line_gate_unverified"] == "diff_truncated"
    assert notes["preservation_visible_deletions"] == 4


def test_untruncated_deletion_only_diff_is_still_hard_gated():
    result = cr._precheck_preservation_gate(
        _deletion_only_marked_diff().replace(MARKER, ""), "지시", None, {}, "GO100")
    assert result is not None and result.flag_category == "PRESERVATION_HARD_GATE"


# ── (h) adjudicate: REVIEW_DIFF_TRUNCATED 는 retry_count=0 허용, 타 카테고리는 기존 임계값 ──

class _Tx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Conn:
    def __init__(self, row):
        self.row, self.executed = row, []

    def transaction(self):
        return _Tx()

    async def fetchrow(self, *_a):
        return self.row

    async def execute(self, sql, *args):
        self.executed.append((" ".join(sql.split()), args))
        return "UPDATE 1"


class _Acquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *exc):
        return False


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return _Acquire(self.conn)


SESSION = "11111111-1111-4111-8111-111111111111"
TENANT = "22222222-2222-4222-8222-222222222222"
COMMIT = "0123456789abcdef0123456789abcdef01234567"
STORED_DIFF = _marked_diff()
DIFF_SHA = hashlib.sha256(STORED_DIFF.encode("utf-8")).hexdigest()


def _adjudicate(category: str, retry_count: int, *, diff_sha: str = DIFF_SHA, verdict: str = "REJECT"):
    import importlib
    pr = importlib.import_module("app.api.pipeline_runner")
    row = {
        "job_id": "runner-test", "project": "AADS", "status": "review_hold",
        "chat_session_id": SESSION, "tenant_id": TENANT, "commit_hash": COMMIT,
        "git_diff": STORED_DIFF, "review_flag_category": category, "error_detail": "",
        "review_retry_count": retry_count, "session_tenant_id": TENANT,
    }
    conn = _Conn(row)
    req = pr.PipelineReviewAdjudicateRequest(
        caller_session_id=SESSION, expected_commit_sha=COMMIT,
        expected_diff_sha256=diff_sha, verdict=verdict, findings="판정")
    context = {"tenant": {"id": TENANT}}
    with patch("app.core.db_pool.get_pool", return_value=_Pool(conn)):
        result = asyncio.run(pr.adjudicate_review_from_origin_session("runner-test", req, context))
    return result, conn


def _status_of_error(category, retry_count, **kw):
    from fastapi import HTTPException
    try:
        _adjudicate(category, retry_count, **kw)
    except HTTPException as exc:
        return exc.status_code, exc.detail
    return None, None


def test_adjudicate_accepts_truncated_hold_with_zero_retries():
    result, conn = _adjudicate("REVIEW_DIFF_TRUNCATED", 0)
    assert result["status"] == "error" and result["verdict"] == "REJECT"
    assert any("UPDATE pipeline_jobs" in sql for sql, _ in conn.executed)


def test_adjudicate_truncated_hold_unknown_preserves_state_and_needs_retry_and_retry_count():
    """REVIEW_DIFF_TRUNCATED + UNKNOWN -> 상태(review_hold)·needs_retry·retry_count 불변, 재검수 루프로 안 보냄."""
    result, conn = _adjudicate("REVIEW_DIFF_TRUNCATED", 0, verdict="UNKNOWN")
    assert result["status"] == "review_hold"
    assert result["verdict"] == "UNKNOWN"
    updates = [sql for sql, _ in conn.executed if "UPDATE pipeline_jobs" in sql]
    assert len(updates) == 1
    sql = updates[0]
    assert "review_needs_retry=TRUE" not in sql
    assert "review_retry_count" not in sql
    assert "error_detail='review_adjudication_unknown'" not in sql
    assert "review_verdict='UNKNOWN'" in sql
    assert "updated_at=NOW()" in sql


def test_adjudicate_truncated_hold_approve_transitions_to_awaiting_approval():
    """REVIEW_DIFF_TRUNCATED + APPROVE -> awaiting_approval 전이."""
    result, conn = _adjudicate("REVIEW_DIFF_TRUNCATED", 0, verdict="APPROVE")
    assert result["status"] == "awaiting_approval"
    assert result["verdict"] == "APPROVE"
    updates = [sql for sql, _ in conn.executed if "UPDATE pipeline_jobs" in sql]
    assert len(updates) == 1
    sql = updates[0]
    assert "status='awaiting_approval'" in sql
    assert "review_needs_retry=FALSE" in sql


def test_adjudicate_other_category_unknown_modifies_needs_retry_and_retry_count():
    """다른 카테고리(REVIEW_TIMEOUT) + UNKNOWN -> review_needs_retry=TRUE, review_retry_count 갱신 (기존 동작)."""
    result, conn = _adjudicate("REVIEW_TIMEOUT", 3, verdict="UNKNOWN")
    assert result["status"] == "review_hold"
    assert result["verdict"] == "UNKNOWN"
    updates = [sql for sql, _ in conn.executed if "UPDATE pipeline_jobs" in sql]
    assert len(updates) == 1
    sql = updates[0]
    assert "review_needs_retry=TRUE" in sql
    assert "review_retry_count=GREATEST($2 - 1, 0)" in sql
    assert "error_detail='review_adjudication_unknown'" in sql



def test_adjudicate_keeps_threshold_for_other_categories():
    code, detail = _status_of_error("REVIEW_TIMEOUT", 0)
    assert code == 409 and "상한" in detail
    result, _ = _adjudicate("REVIEW_TIMEOUT", 3)
    assert result["status"] == "error"


def test_adjudicate_still_rejects_non_hold_categories():
    code, detail = _status_of_error("CODE_QUALITY", 9)
    assert code == 409 and "인프라" in detail


def test_adjudicate_keeps_diff_hash_check_for_truncated_hold():
    code, detail = _status_of_error("REVIEW_DIFF_TRUNCATED", 0, diff_sha="0" * 64)
    assert code == 409 and "SHA" in detail


# ── (j) restore 스크립트: --job 없이는 0건 ──

restore = _load("restore_truncated_under_test", "scripts/restore_truncated_review_failed.py")


def test_restore_selects_nothing_without_job_and_never_touches_db(capsys):
    assert restore.select_job_ids([]) == []
    assert restore.select_job_ids(None) == []
    with patch.object(restore, "_psql", side_effect=AssertionError("DB 접근 금지")):
        assert restore.main([]) == 0
        assert restore.main(["--apply"]) == 0
    assert "대상 0건" in capsys.readouterr().out


def test_restore_rejects_malformed_job_id():
    with pytest.raises(ValueError):
        restore.select_job_ids(["runner-1'; DROP TABLE x;--"])


def test_restore_is_dry_run_by_default_and_writes_only_with_apply(capsys):
    row = "runner-aa|error|review_failed|REQUEST_CHANGES|CODE_QUALITY|" + COMMIT + "|t|45100"
    calls = []

    def fake(sql):
        calls.append(sql)
        return row + "\n" if sql.startswith("SELECT") else "runner-aa\n"

    with patch.object(restore, "_psql", side_effect=fake):
        assert restore.main(["--job", "runner-aa"]) == 0
        assert all(c.startswith("SELECT") for c in calls)
        out = capsys.readouterr().out
        assert "구조화 근거 없음, 사람 지정" in out and "dry-run" in out
        assert restore.main(["--job", "runner-aa", "--apply"]) == 0
    update = [c for c in calls if c.startswith("UPDATE")]
    assert len(update) == 1
    assert "review_flag_category='REVIEW_DIFF_TRUNCATED'" in update[0]
    assert "review_needs_retry=FALSE" in update[0]
    assert "status='error' AND phase='review_failed'" in update[0]


def test_restore_excludes_job_without_truncation_marker():
    ok, reason = restore.classify(["runner-aa", "error", "review_failed", "REQUEST_CHANGES", "", COMMIT, "f", "900"])
    assert ok is False and "절단 표지" in reason
