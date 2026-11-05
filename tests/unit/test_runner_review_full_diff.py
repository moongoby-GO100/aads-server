"""AADS-RUNNER-REVIEW-FULL-DIFF-20261106

러너 AI 리뷰 입력은 DB 저장용 45KB 절단 diff 가 아니라 리뷰 상한(200,000자)까지의 전체 diff 다.
상한을 넘으면 리뷰 LLM 을 부르지 않고 review_diff_too_large 로 종결하며 자동 재작업을 만들지 않는다.

셸 함수는 pipeline-runner.sh 에서 잘라 내 임시 git 저장소 위에서 돌린다. 리뷰 단계 전체(상한 판정 →
요청 본문 → curl → 보류 블록)는 러너 본문을 그 자리에서 잘라 가짜 curl/db 와 함께 실행해
"리뷰 호출 0회" 를 실제로 센다.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from app.services import pipeline_auto_rework as ar

ROOT = Path(__file__).resolve().parents[2]
RUNNER = ROOT / "scripts" / "pipeline-runner.sh"
RUNNER_LOCAL = ROOT / "scripts" / "pipeline-runner.sh.local"
TEXT = RUNNER.read_text(encoding="utf-8")

FUNCS = (
    "looks_like_git_diff",
    "_job_diff_is_code", "_job_diff_render", "_job_diff_full_bytes", "_job_diff_untracked_fallback",
    "capture_job_diff_text", "job_diff_changed_files",
    "review_diff_stat_section", "review_diff_truncation_notice", "build_review_diff_prefix",
    "review_diff_max_chars", "review_diff_cut_chars", "_review_diff_char_len", "review_diff_top_files",
    "capture_review_full_diff", "write_review_request_body",
)

# 단위테스트 컨테이너 이미지에는 jq 가 없다 — jq 가 필요한 테스트만 건너뛴다(러너 호스트에는 있다).
needs_jq = pytest.mark.skipif(shutil.which("jq") is None, reason="jq 필요")


def _extract(name: str) -> str:
    start = TEXT.index(f"\n{name}() {{\n") + 1
    return TEXT[start:TEXT.index("\n}\n", start) + 3]


def _git(cwd, *a):
    return subprocess.run(
        ["git", "-C", str(cwd), *a], check=True, capture_output=True, text=True,
    ).stdout.strip()


def _mkrepo(p: Path) -> Path:
    p.mkdir()
    _git(p, "init", "-q")
    _git(p, "config", "user.name", "T")
    _git(p, "config", "user.email", "t@e.com")
    (p / "seed.txt").write_text("seed\n")
    _git(p, "add", "seed.txt")
    _git(p, "commit", "-qm", "init")
    return p


def _commit_big_file(repo: Path, name: str, target_chars: int, line: str = None) -> str:
    """base SHA 를 돌려주고, 그 위에 target_chars 자 안팎의 코드 파일 하나를 커밋한다."""
    base = _git(repo, "rev-parse", "HEAD")
    line = line or ("x = '" + "a" * 90 + "'")
    n = max(1, target_chars // (len(line) + 2))
    (repo / name).write_text("\n".join(line for _ in range(n)) + "\n", encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "commit", "-qm", f"add {name}")
    return base


@pytest.fixture
def lib(tmp_path):
    specs = next(ln for ln in TEXT.splitlines() if ln.startswith("JOB_DIFF_EXCLUDE_SPECS="))
    globals_ = 'REVIEW_FULL_DIFF_TEXT=""; REVIEW_FULL_DIFF_CHARS=0; REVIEW_FULL_DIFF_TOP_FILES=""'
    p = tmp_path / "lib.sh"
    p.write_text("\n".join([specs, globals_, *(_extract(n) for n in FUNCS)]), encoding="utf-8")
    return p


def _bash(script: str, env=None, timeout=120):
    return subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True,
        env=os.environ | (env or {}), timeout=timeout,
    )


def _body_diff_path(tmp_path):
    return tmp_path / "body.json"


# ── ① 120,000자 diff: 리뷰 본문은 전량, DB 용은 45,000 이하, jq 는 인자 한계 없이 성공 ──
@needs_jq
def test_120k_diff_reaches_review_body_whole_while_db_diff_stays_capped(tmp_path, lib):
    repo = _mkrepo(tmp_path / "r")
    base = _commit_big_file(repo, "big.py", 120_000)
    expected = _git(repo, "diff", "--no-renames", f"{base}..HEAD", "--", ".")  # 명령치환과 같은 개행 처리
    body = tmp_path / "body.json"
    db_out = tmp_path / "db.txt"
    script = f"""
set -eo pipefail
source {lib}
db=$(capture_job_diff_text "$REPO" "$BASE")
printf '%s' "$db" > "$DBOUT"
rc=0
if capture_review_full_diff "$REPO" "$BASE"; then rc=0; else rc=$?; fi
echo "RC=$rc CHARS=$REVIEW_FULL_DIFF_CHARS"
write_review_request_body "$BODY" rid-1 runner-t AADS "지시서" "big.py" "$REVIEW_FULL_DIFF_TEXT" ""
echo "BODY_RC=$?"
"""
    r = _bash(script, env={"REPO": str(repo), "BASE": base, "BODY": str(body), "DBOUT": str(db_out)})
    assert r.returncode == 0, r.stderr
    assert "RC=0 " in r.stdout and "BODY_RC=0" in r.stdout
    review_diff = json.loads(body.read_text(encoding="utf-8"))["diff"]
    assert len(review_diff) >= 120_000
    assert review_diff == expected
    assert "[DIFF TRUNCATED]" not in review_diff and "# [TRUNCATED]" not in review_diff
    db_diff = db_out.read_text(encoding="utf-8")
    db_core = "\n".join(ln for ln in db_diff.split("\n") if not ln.startswith("# [TRUNCATED]"))
    assert len(db_core) <= 45_000
    assert len(review_diff) > 2 * len(db_core)
    # 임시 파일이 남지 않는다
    assert not list(tmp_path.glob("body.json.*"))


@needs_jq
def test_old_arg_path_would_have_failed_on_200kb(tmp_path):
    big = "a" * 200_000
    # MAX_ARG_STRLEN(128KiB) — 본문을 외부 명령 인자로 넘기면 안 되는 이유
    with pytest.raises(OSError):
        subprocess.run(["jq", "-n", "--arg", "diff", big, "{diff: $diff}"], capture_output=True, text=True)


# ── ② 250,000자 diff: 상한 초과 → 리뷰 호출 0회, error_detail 접두 review_diff_too_large ──
def test_over_limit_diff_returns_3_and_lists_top_files(tmp_path, lib):
    repo = _mkrepo(tmp_path / "r")
    base = _commit_big_file(repo, "huge.py", 250_000)
    out = tmp_path / "text.txt"
    script = f"""
source {lib}
rc=0
if capture_review_full_diff "$REPO" "$BASE"; then rc=0; else rc=$?; fi
printf '%s' "$REVIEW_FULL_DIFF_TEXT" > "$OUT"
echo "RC=$rc CHARS=$REVIEW_FULL_DIFF_CHARS"
echo "TOP:$REVIEW_FULL_DIFF_TOP_FILES"
"""
    r = _bash(script, env={"REPO": str(repo), "BASE": base, "OUT": str(out)})
    assert r.returncode == 0, r.stderr
    assert "RC=3 " in r.stdout
    chars = int(r.stdout.split("CHARS=")[1].split()[0])
    assert chars > 250_000
    assert "- huge.py (" in r.stdout and "자)" in r.stdout
    assert len(out.read_text(encoding="utf-8")) == 200_000


def test_env_limit_is_honoured_and_garbage_falls_back(lib):
    r = _bash(f"source {lib}; review_diff_max_chars; echo; RUNNER_REVIEW_DIFF_MAX_CHARS=1234 review_diff_max_chars; echo; "
              "RUNNER_REVIEW_DIFF_MAX_CHARS=abc review_diff_max_chars; echo; RUNNER_REVIEW_DIFF_MAX_CHARS=0 review_diff_max_chars")
    assert r.stdout.split() == ["200000", "1234", "200000", "200000"]


# ── 러너 본문의 리뷰 단계를 그 자리에서 실행 ──
def _slice(start: str, end: str) -> str:
    i = TEXT.index(start)
    return TEXT[i:TEXT.index(end, i)]


def _review_stage_script(lib, tmp_path):
    prefix_block = _slice('    local review_diff_prefix=""\n    local review_full_diff=""',
                          "    _persist_full_diff_if_truncated")
    decision_block = _slice('    local review_verdict="APPROVE"',
                            "\n    db_update \"UPDATE pipeline_jobs SET phase='awaiting_approval',\n"
                            "               status='awaiting_approval',\n               result_output=")
    cnt, cap, events = tmp_path / "curl_posts", tmp_path / "captured_body.json", tmp_path / "events"
    return f"""
source {lib}
ARTIFACT_DIR="{tmp_path}"; mkdir -p "$ARTIFACT_DIR"
AADS_API_URL=http://stub; AADS_REVIEW_ASYNC_WAIT_SEC=5; AADS_REVIEW_POLL_INTERVAL=0; AADS_REVIEW_MAX_ATTEMPTS=1
ARTIFACT_MAX_AGE_HOURS=24
log() {{ echo "LOG $*" >> "{events}"; }}
db_update() {{ echo "DB $*" >> "{events}"; }}
db_exec() {{ echo error; }}
sql_escape() {{ printf "'%s'" "$1"; }}
get_job_status() {{ echo running; }}
record_runner_event() {{ :; }}
post_to_chat() {{ echo "CHAT $2" >> "{events}"; }}
_release_work_lock() {{ :; }}
_cleanup_artifacts() {{ :; }}
_preserve_worktree_patch() {{ :; }}
_notify_ai() {{ :; }}
promote_next_queued() {{ :; }}
rm() {{ if [[ "$*" == *"/tmp/.pipeline_current_job"* ]]; then return 0; fi; command rm "$@"; }}
curl() {{
    if [[ "$*" == *"-X POST"* ]]; then
        echo x >> "{cnt}"
        for a in "$@"; do [[ "$a" == @* ]] && cp "${{a#@}}" "{cap}"; done
        printf '{{}}\\n202'
    else
        printf '{{"status":"completed","verdict":"APPROVE","score":0.95}}\\n200'
    fi
}}
stage() {{
    local workdir="$REPO" pre_exec_sha="$BASE" job_id=runner-t project=AADS instruction="지시서" session_id=s
    local worktree_dir=/tmp/wt-none parallel_group="" job_model=m job_size=M output=""
    local _current_job_id="" _current_session_id=""
    local git_diff
    git_diff=$(capture_job_diff_text "$workdir" "$pre_exec_sha")
    echo "$git_diff" > "{tmp_path}/db_git_diff.txt"
{prefix_block}
{decision_block}
    echo "FELL_THROUGH verdict=$review_verdict"
}}
rc=0
stage || rc=$?
echo "STAGE_RC=$rc"
echo "POSTS=$(wc -l < "{cnt}" 2>/dev/null || echo 0)"
"""


@needs_jq
def test_stage_120k_review_gets_whole_diff_in_one_post(tmp_path, lib):
    repo = _mkrepo(tmp_path / "r")
    base = _commit_big_file(repo, "big.py", 120_000)
    expected = _git(repo, "diff", "--no-renames", f"{base}..HEAD", "--", ".")
    r = _bash(_review_stage_script(lib, tmp_path), env={"REPO": str(repo), "BASE": base})
    assert r.returncode == 0, r.stderr
    assert "FELL_THROUGH verdict=APPROVE" in r.stdout and "POSTS=1" in r.stdout
    sent = json.loads((tmp_path / "captured_body.json").read_text(encoding="utf-8"))
    assert sent["diff"] == expected and len(sent["diff"]) >= 120_000
    assert sent["request_id"] and sent["job_id"] == "runner-t" and sent["files_changed"] == ["big.py"]
    assert "[DIFF TRUNCATED]" not in sent["diff"]
    assert len((tmp_path / "db_git_diff.txt").read_text(encoding="utf-8")) < 46_000
    assert not list(tmp_path.glob("runner-t.review_body.json*"))


def test_stage_250k_makes_zero_review_calls_and_ends_review_failed(tmp_path, lib):
    repo = _mkrepo(tmp_path / "r")
    base = _commit_big_file(repo, "huge.py", 250_000)
    r = _bash(_review_stage_script(lib, tmp_path), env={"REPO": str(repo), "BASE": base})
    assert r.returncode == 0, r.stderr
    assert "STAGE_RC=1" in r.stdout and "FELL_THROUGH" not in r.stdout
    assert "POSTS=0" in r.stdout
    assert not (tmp_path / "captured_body.json").exists()
    events = (tmp_path / "events").read_text(encoding="utf-8")
    assert "status='error', phase='review_failed'" in events
    assert "'review_diff_too_large: chars=" in events and " limit=200000'" in events
    assert "AI_REVIEW job=" not in events
    chat = "CHAT " + events.split("CHAT ", 1)[1]
    assert "review_diff_too_large: chars=" in chat
    assert "작업을 상한 이하로 나눠 다시 제출해야 합니다" in chat
    assert "- huge.py (" in chat
    assert not ar.is_request_changes_failure("error", "review_failed", "review_diff_too_large: chars=250000 limit=200000")


def test_stage_limit_is_env_tunable(tmp_path, lib):
    repo = _mkrepo(tmp_path / "r")
    base = _commit_big_file(repo, "mid.py", 30_000)
    r = _bash(_review_stage_script(lib, tmp_path),
              env={"REPO": str(repo), "BASE": base, "RUNNER_REVIEW_DIFF_MAX_CHARS": "10000"})
    assert "POSTS=0" in r.stdout and "STAGE_RC=1" in r.stdout


# ── ③ 멀티바이트 경계: 문자 단위로 자르고 UTF-8 은 유효 ──
@pytest.mark.parametrize("limit", [1001, 1002, 1003, 7777])
def test_cut_on_hangul_boundary_stays_valid_utf8(lib, limit, tmp_path):
    src = tmp_path / "src.txt"
    src.write_text("가나다라마바사" * 2000 + "é😀" * 100, encoding="utf-8")
    out = tmp_path / "out.txt"
    r = _bash(f'source {lib}; review_diff_cut_chars {limit} < "$SRC" > "$OUT"', env={"SRC": str(src), "OUT": str(out)})
    assert r.returncode == 0, r.stderr
    data = out.read_bytes()
    decoded = data.decode("utf-8")  # strict — 깨졌으면 예외
    assert len(decoded) == limit
    assert decoded == src.read_text(encoding="utf-8")[:limit]


def test_cut_short_text_is_untouched(lib, tmp_path):
    src = tmp_path / "s.txt"
    src.write_text("한글 diff\n+줄", encoding="utf-8")
    out = tmp_path / "o.txt"
    r = _bash(f'source {lib}; review_diff_cut_chars 200000 < "$SRC" > "$OUT"', env={"SRC": str(src), "OUT": str(out)})
    assert r.returncode == 0 and out.read_text(encoding="utf-8") == "한글 diff\n+줄"


def test_capture_counts_chars_not_bytes_for_hangul_diff(tmp_path, lib):
    repo = _mkrepo(tmp_path / "r")
    base = _commit_big_file(repo, "ko.py", 90_000, line="s = '" + "한글" * 40 + "'")
    out = tmp_path / "text.txt"
    # 바이트로 세면 200,000 을 넘고 문자로 세면 아래다 — 상한은 문자 기준이다.
    r = _bash(f"""source {lib}
rc=0
if capture_review_full_diff "$REPO" "$BASE"; then rc=0; else rc=$?; fi
printf '%s' "$REVIEW_FULL_DIFF_TEXT" > "$OUT"
echo "RC=$rc CHARS=$REVIEW_FULL_DIFF_CHARS"
""", env={"REPO": str(repo), "BASE": base, "OUT": str(out)})
    assert r.returncode == 0, r.stderr
    text = out.read_text(encoding="utf-8")
    assert len(text.encode("utf-8")) > 200_000 > len(text)
    assert "RC=0 " in r.stdout


def test_hangul_diff_cut_at_small_limit_is_valid_utf8(tmp_path, lib):
    repo = _mkrepo(tmp_path / "r")
    base = _commit_big_file(repo, "ko.py", 20_000, line="s = '" + "한글" * 40 + "'")
    out = tmp_path / "text.txt"
    r = _bash(f"""source {lib}
rc=0
if capture_review_full_diff "$REPO" "$BASE"; then rc=0; else rc=$?; fi
printf '%s' "$REVIEW_FULL_DIFF_TEXT" > "$OUT"
echo "RC=$rc"
""", env={"REPO": str(repo), "BASE": base, "OUT": str(out), "RUNNER_REVIEW_DIFF_MAX_CHARS": "5001"})
    assert "RC=3" in r.stdout
    assert len(out.read_bytes().decode("utf-8")) == 5001


# ── 절단 고지(앞머리)는 리뷰용 diff 가 실제로 잘렸을 때만 ──
def test_truncation_prefix_only_when_review_diff_really_truncated(tmp_path, lib):
    repo = _mkrepo(tmp_path / "r")
    base = _commit_big_file(repo, "big.py", 120_000)
    r = _bash(f"""source {lib}
capture_review_full_diff "$REPO" "$BASE"
p1=$(build_review_diff_prefix "$REPO" "$BASE" "$REVIEW_FULL_DIFF_TEXT")
echo "FULL_PREFIX_LEN=${{#p1}}"
db=$(capture_job_diff_text "$REPO" "$BASE")
p2=$(build_review_diff_prefix "$REPO" "$BASE" "$db")
echo "DB_PREFIX_HAS_NOTICE=$([[ "$p2" == *"[DIFF TRUNCATED]"* ]] && echo yes || echo no)"
""", env={"REPO": str(repo), "BASE": base})
    assert r.returncode == 0, r.stderr
    assert "FULL_PREFIX_LEN=0" in r.stdout
    assert "DB_PREFIX_HAS_NOTICE=yes" in r.stdout


# ── 배선 ──
def test_runner_and_local_copy_are_identical_and_wired():
    assert RUNNER.read_bytes() == RUNNER_LOCAL.read_bytes()
    assert 'jq -n \\\n                --arg rid "$review_request_id"' not in TEXT
    assert '--data-binary @"$review_body_file"' in TEXT
    assert '-d "$review_body"' not in TEXT
    assert "--arg diff" not in TEXT
    # DB 저장 상한은 그대로
    assert '_job_diff_render "$repo" 45000 "${base_sha}..${head_sha}"' in TEXT
    assert '_job_diff_render "$repo" 5000 HEAD' in TEXT
    assert '_job_diff_render "$repo" 50000 HEAD' in TEXT
    # 상한 초과 분기는 curl 이 있는 elif 보다 앞서고 그 사이에 curl 이 없다
    i = TEXT.index('        if [[ "$review_diff_too_large" == "1" ]]; then\n            review_verdict="FLAG"')
    j = TEXT.index("        elif looks_like_git_diff", i)
    assert "curl" not in TEXT[i:j]
    assert TEXT.index("code-diff/requests", j) > j


# ── ④ pipeline_auto_rework ──
class FakeConn:
    def __init__(self, row, gate_review=None, feedback=None):
        self.row, self.gate_review, self.feedback = row, gate_review, feedback
        self.executed: list[tuple] = []

    async def fetchrow(self, sql, *args):
        if "FROM code_reviews" in sql:
            return self.gate_review
        if "FROM goal_task_links" in sql:
            return None
        return self.row

    async def fetchval(self, sql, *args):
        if "instruction LIKE" in sql:
            return None
        if "code_reviews" in sql:
            return self.feedback
        return None

    async def execute(self, sql, *args):
        self.executed.append((sql, args))
        return "OK"

    @asynccontextmanager
    async def _tx(self):
        yield

    def transaction(self):
        return self._tx()


def _row(**kw):
    base = dict(
        job_id="runner-aaaaaaaa", project="AADS", instruction="TARGET: x\n본문\n",
        chat_session_id="1fa84036-b12d-4497-97f5-076a32645a20", status="error",
        phase="review_failed", error_detail="review_failed: verdict=REQUEST_CHANGES score=0.68",
        max_cycles=3, model="m", size="S", worker_model=None, model_override_reason=None,
        parallel_group=None, tenant_id="00000000-0000-0000-0000-000000000001",
        commit_hash="abc", review_score=0.68, goal_id=None, milestone_id=None,
    )
    base.update(kw)
    return base


def _inserts(conn):
    return [a for s, a in conn.executed if "INSERT INTO pipeline_jobs" in s]


def _review(issues, *, needs_retry=True, truncated=None, stage="review_analysis"):
    fb = {"issues": issues}
    if truncated is not None:
        fb["measurement"] = {"diff_truncated": truncated}
    return {"failure_stage": stage, "needs_retry": needs_retry, "feedback": json.dumps(fb)}


def test_review_diff_too_large_never_reworks():
    conn = FakeConn(_row(error_detail="review_diff_too_large: chars=250000 limit=200000"))
    res = asyncio.run(ar.maybe_submit_auto_rework(conn, "runner-aaaaaaaa"))
    assert res == {"skipped": "review_diff_too_large", "parent": "runner-aaaaaaaa"}
    assert _inserts(conn) == [] and conn.executed == []
    assert ar.is_review_diff_too_large("review_diff_too_large: chars=1 limit=2")
    assert not ar.is_review_diff_too_large("review_failed: verdict=REQUEST_CHANGES score=0.7")


def test_plain_request_changes_still_reworks():
    conn = FakeConn(_row(), feedback=json.dumps({"issues": ["결함A"]}))
    res = asyncio.run(ar.maybe_submit_auto_rework(conn, "runner-aaaaaaaa"))
    assert res and res["round"] == 1
    assert len(_inserts(conn)) == 1


def test_request_changes_with_code_issue_and_truncation_flag_still_reworks():
    issues = ["diff 가 절단돼 확인 불가", "validate() 가 None 을 반환한다"]
    conn = FakeConn(_row(), feedback=json.dumps({"issues": issues}),
                    gate_review=_review(issues, truncated=True))
    res = asyncio.run(ar.maybe_submit_auto_rework(conn, "runner-aaaaaaaa"))
    assert res and res["round"] == 1 and len(_inserts(conn)) == 1


def test_truncation_only_rejection_with_flag_does_not_rework():
    issues = ["diff 가 중간에서 절단되어 판정 불가", "pipeline-runner.sh 변경이 생략돼 로컬에 없는 것으로 보임"]
    conn = FakeConn(_row(), feedback=json.dumps({"issues": issues}),
                    gate_review=_review(issues, truncated=True))
    res = asyncio.run(ar.maybe_submit_auto_rework(conn, "runner-aaaaaaaa"))
    assert res == {"skipped": "diff_truncated", "parent": "runner-aaaaaaaa"}
    assert _inserts(conn) == []


@pytest.mark.parametrize("review", [
    _review(["diff 가 절단돼 판정 불가"], truncated=None),   # 필드 없음 → 추측하지 않고 기존대로
    _review(["diff 가 절단돼 판정 불가"], truncated=False),
    _review(["diff 가 절단돼 판정 불가"], truncated=True, needs_retry=False),
    _review([], truncated=True),
    {"failure_stage": None, "needs_retry": None, "feedback": None},
])
def test_truncation_guard_needs_explicit_evidence(review):
    assert not ar.is_truncation_only_rejection(review)
    conn = FakeConn(_row(), feedback=json.dumps({"issues": ["x"]}), gate_review=review)
    res = asyncio.run(ar.maybe_submit_auto_rework(conn, "runner-aaaaaaaa"))
    assert res and res.get("round") == 1
