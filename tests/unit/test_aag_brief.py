"""AAG 착수 브리프 생성기 단위테스트.

브리프는 워커 프롬프트에 **조용히 끼어드는** 부품이다. 그래서 여기서 지켜야 할
성질은 "좋은 브리프를 만드는가" 보다 **"어떤 경우에도 러너를 막지 않는가"** 가 먼저다.
그래프가 없든, 깨졌든, 매칭이 0건이든 전부 exit 0 이어야 한다 — 이 성질이 깨지면
`timeout 20 python3 tools/aag/brief.py` 가 비정상 종료하면서 러너 쪽 로그만
더럽히거나, 최악의 경우 프롬프트에 쓰레기가 섞인다.

픽스처는 tmp_path 에 만드는 소형 그래프다. 실제 328KB 그래프를 읽으면 테스트가
그날그날의 결함 수에 따라 흔들린다.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BRIEF = ROOT / "tools" / "aag" / "brief.py"
RUNNER = ROOT / "scripts" / "pipeline-runner.sh"


def _graph() -> dict:
    """실제 스키마를 그대로 축소한 그래프 — chat.py 중복 사고를 합성으로 고정한다."""
    return {
        "generated_at": "2026-09-18T08:19:47.358171+09:00",
        "root": "/root/aads/aads-server",
        "nodes": [
            {"id": "app/main.py", "kind": "entrypoint"},
            {"id": "app/api/chat.py", "kind": "router_module", "routes": 4},
            {"id": "app/routers/chat.py", "kind": "router_module", "routes": 81},
            {"id": "ns:/api/v1/chat", "kind": "namespace", "path": "/api/v1/chat"},
            {"id": "table:chat_messages", "kind": "table", "defined": False, "users": 1},
            {"id": "/root/aads/aads-dashboard/src/app/chat/page.tsx", "kind": "frontend"},
        ],
        "edges": [
            {"from": "app/main.py", "to": "app/api/chat.py", "kind": "mounts",
             "prefix": "/api/v1", "lineno": 3766},
            {"from": "app/api/chat.py", "to": "ns:/api/v1/chat", "kind": "owns"},
            {"from": "app/routers/chat.py", "to": "ns:/api/v1/chat", "kind": "owns"},
            {"from": "app/routers/chat.py", "to": "table:chat_messages", "kind": "queries"},
            {"from": "/root/aads/aads-dashboard/src/app/chat/page.tsx", "to": "ns:/api/v1/chat",
             "kind": "calls", "method": "GET", "path": "/api/v1/chat/sessions", "lineno": 42},
        ],
        "findings": [
            {"rule": "DUP_MODULE", "key": "chat.py",
             "modules": ["app/api/chat.py", "app/routers/chat.py"],
             "detail": "모듈명 `chat.py` 이 2개 디렉터리에 중복 존재", "severity": "P1"},
            {"rule": "DOUBLE_MOUNT", "key": "/api/v1/chat", "namespace": "/api/v1/chat",
             "entrypoint": "app/main.py",
             "owners": ["app/api/chat.py", "app/routers/chat.py"],
             "detail": "네임스페이스 `/api/v1/chat` 를 2개 모듈이 소유", "severity": "P1"},
            {"rule": "TABLE_NO_MODEL", "key": "unrelated_table", "table": "unrelated_table",
             "users": ["app/api/billing.py"], "detail": "상관없는 결함", "severity": "P1"},
        ],
        "unresolved": [
            {"kind": "SQL_TABLE", "module": "app/routers/chat.py", "lineno": 77,
             "detail": "테이블 이름이 런타임 보간이라 확정 불가"},
            {"kind": "SQL_TABLE", "module": "app/api/billing.py", "lineno": 12,
             "detail": "상관없는 항목"},
        ],
    }


def _run(tmp_path, instruction: str, graph=..., extra=()):
    ins = tmp_path / "ins.txt"
    ins.write_text(instruction, encoding="utf-8")
    if graph is ...:
        graph_path = tmp_path / "graph.json"
        graph_path.write_text(json.dumps(_graph(), ensure_ascii=False), encoding="utf-8")
    else:
        graph_path = graph
    cmd = [sys.executable, str(BRIEF), "--instruction-file", str(ins),
           "--graph", str(graph_path), *extra]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=60)


# ── ① 그래프가 없을 때 ────────────────────────────────────────────────


def test_missing_graph_is_silent_success(tmp_path):
    """그래프가 없다고 본 작업을 막지 않는다 — exit 0 에 빈 출력."""
    res = _run(tmp_path, "app/api/chat.py 를 고쳐라", graph=tmp_path / "nope.json")

    assert res.returncode == 0
    assert res.stdout == ""
    assert "그래프" in res.stderr  # 조용히 통과하지는 않는다


# ── ② 매칭 0건 ────────────────────────────────────────────────────────


def test_no_keyword_match_emits_nothing(tmp_path):
    """그래프에 없는 것은 채택하지 않는다. 추측한 브리프는 없느니만 못하다."""
    res = _run(tmp_path, "회의록을 정리하고 보고서를 써라")

    assert res.returncode == 0
    assert res.stdout == ""


def test_empty_instruction_emits_nothing(tmp_path):
    res = _run(tmp_path, "   \n  ")

    assert res.returncode == 0
    assert res.stdout == ""


# ── ③ 파일경로 매칭 → 실제 마운트 경로 ────────────────────────────────


def test_filepath_match_reports_mount_prefix_and_lineno(tmp_path):
    """워커가 grep 으로 다시 찾지 않도록 prefix 와 include_router 줄번호를 준다."""
    res = _run(tmp_path, "app/api/chat.py 의 스트리밍 응답을 고쳐라")

    assert res.returncode == 0
    out = res.stdout
    assert "## 건드릴 파일과 실제 마운트 경로" in out
    assert "app/api/chat.py" in out
    assert "/api/v1" in out
    assert "3766" in out  # include_router 가 있는 줄
    assert "위 목록은 정적분석 실측이다" in out


def test_frontend_caller_is_listed(tmp_path):
    res = _run(tmp_path, "app/api/chat.py 를 고쳐라")

    assert "## 이 모듈을 부르는 곳" in res.stdout
    assert "src/app/chat/page.tsx" in res.stdout
    assert ":42" in res.stdout


def test_queried_tables_are_listed(tmp_path):
    res = _run(tmp_path, "app/routers/chat.py 의 조회 쿼리를 고쳐라")

    assert "## 이 모듈이 쓰는 테이블" in res.stdout
    assert "chat_messages" in res.stdout


# ── ④ 관련 결함만 ⚠섹션에 ────────────────────────────────────────────


def test_related_findings_appear_and_unrelated_do_not(tmp_path):
    """DUP_MODULE 이 보이지 않으면 워커는 또 죽은 쪽 파일을 고친다."""
    res = _run(tmp_path, "app/api/chat.py 의 스트리밍 응답을 고쳐라")

    out = res.stdout
    assert "## ⚠ 이 범위에 이미 걸린 결함" in out
    assert "DUP_MODULE" in out
    assert "DOUBLE_MOUNT" in out
    # app/main.py 는 모든 라우터를 mount 하는 허브다. 이것을 타고 관련을 판정하면
    # 상관없는 결함이 전부 딸려 들어온다.
    assert "unrelated_table" not in out


def test_unresolved_section_is_scoped(tmp_path):
    res = _run(tmp_path, "app/routers/chat.py 를 고쳐라")

    out = res.stdout
    assert "## 판정 불가(UNRESOLVED)" in out
    assert ":77" in out
    assert "app/api/billing.py" not in out


# ── ⑤ 예산 초과 시 무엇을 남기는가 ────────────────────────────────────


def test_max_bytes_keeps_findings_and_marks_truncation(tmp_path):
    """잘릴 때 남아야 하는 것은 ⚠결함이다 — 유일하게 행동을 바꾸는 정보다."""
    res = _run(tmp_path, "app/routers/chat.py 와 app/api/chat.py 를 고쳐라",
               extra=["--max-bytes", "900"])

    out = res.stdout
    assert res.returncode == 0
    assert "## ⚠ 이 범위에 이미 걸린 결함" in out
    assert "(브리프 일부 생략)" in out
    assert "## 판정 불가(UNRESOLVED)" not in out  # 낮은 우선순위부터 버린다
    assert len(out.encode("utf-8")) <= 900


def test_full_brief_is_not_marked_truncated(tmp_path):
    res = _run(tmp_path, "app/api/chat.py 를 고쳐라", extra=["--max-bytes", "6000"])

    assert "(브리프 일부 생략)" not in res.stdout


# ── ⑥ 깨진 그래프 ────────────────────────────────────────────────────


def test_corrupt_graph_json_does_not_crash(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text('{"nodes": [', encoding="utf-8")

    res = _run(tmp_path, "app/api/chat.py 를 고쳐라", graph=bad)

    assert res.returncode == 0
    assert res.stdout == ""
    assert "Traceback" not in res.stderr


def test_graph_with_wrong_shape_does_not_crash(tmp_path):
    odd = tmp_path / "odd.json"
    odd.write_text('{"generated_at": "x", "nodes": "not-a-list"}', encoding="utf-8")

    res = _run(tmp_path, "app/api/chat.py 를 고쳐라", graph=odd)

    assert res.returncode == 0
    assert res.stdout == ""


# ── 프로젝트 일반화 (AADS-AAG-BRIEF-002) ────────────────────────────────


def test_project_header_is_stamped(tmp_path):
    """머리글에 프로젝트 키가 찍혀야 다른 프로젝트 브리프와 구분된다."""
    res = _run(tmp_path, "app/api/chat.py 를 고쳐라", extra=["--project", "AADS"])

    assert "[AAG 착수 브리프 — AADS]" in res.stdout


def test_project_acct_resolves_named_graph_by_cwd(tmp_path):
    """--graph 없이 --project ACCT 만으로 CWD 기준 acct-graph.json 을 찾는다."""
    graph_dir = tmp_path / "reports" / "aag"
    graph_dir.mkdir(parents=True)
    (graph_dir / "acct-graph.json").write_text(json.dumps(_graph(), ensure_ascii=False), encoding="utf-8")
    ins = tmp_path / "ins.txt"
    ins.write_text("app/api/chat.py 를 고쳐라", encoding="utf-8")

    res = subprocess.run(
        [sys.executable, str(BRIEF), "--instruction-file", str(ins), "--project", "ACCT"],
        capture_output=True, text=True, timeout=60, cwd=tmp_path,
    )

    assert res.returncode == 0
    assert "[AAG 착수 브리프 — ACCT]" in res.stdout
    assert "app/api/chat.py" in res.stdout


def test_unknown_project_without_graph_is_silent_success(tmp_path):
    """그래프가 없는 프로젝트(등록 안 된 키)도 본 작업을 막지 않는다 — exit 0, 빈 출력."""
    ins = tmp_path / "ins.txt"
    ins.write_text("app/api/chat.py 를 고쳐라", encoding="utf-8")

    res = subprocess.run(
        [sys.executable, str(BRIEF), "--instruction-file", str(ins), "--project", "NO_SUCH_PROJECT"],
        capture_output=True, text=True, timeout=60, cwd=tmp_path,
    )

    assert res.returncode == 0
    assert res.stdout == ""


def test_explicit_graph_overrides_project_default(tmp_path):
    """`--graph` 가 주어지면 프로젝트 기본 경로에 있는 그래프보다 우선한다."""
    graph_dir = tmp_path / "reports" / "aag"
    graph_dir.mkdir(parents=True)
    decoy = _graph()
    decoy["generated_at"] = "DECOY"
    (graph_dir / "aads-graph.json").write_text(json.dumps(decoy, ensure_ascii=False), encoding="utf-8")

    explicit_graph = tmp_path / "explicit.json"
    real = _graph()
    real["generated_at"] = "EXPLICIT"
    explicit_graph.write_text(json.dumps(real, ensure_ascii=False), encoding="utf-8")

    ins = tmp_path / "ins.txt"
    ins.write_text("app/api/chat.py 를 고쳐라", encoding="utf-8")

    res = subprocess.run(
        [sys.executable, str(BRIEF), "--instruction-file", str(ins),
         "--graph", str(explicit_graph), "--project", "AADS"],
        capture_output=True, text=True, timeout=60, cwd=tmp_path,
    )

    assert "EXPLICIT" in res.stdout
    assert "DECOY" not in res.stdout


# ── 러너 배선 ─────────────────────────────────────────────────────────


def test_runner_injects_brief_before_step0():
    """브리프를 만들어도 러너가 붙이지 않으면 아무 일도 일어나지 않는다.

    배선이 끊긴 것은 도구가 조용히 잘 도는 것과 구분되지 않으므로 여기서 고정한다.
    """
    src = RUNNER.read_text(encoding="utf-8")

    assert "tools/aag/brief.py" in src
    assert "timeout 20 python3" in src
    assert "AAG_BRIEF_SKIP" in src and "AAG_BRIEF_OK" in src
    assert "aag_brief_attached" in src

    # 필수 규칙 → 브리프 → STEP 0 순서여야 한다.
    i_rules = src.index("위 규칙을 위반하면 작업이 거부됩니다.")
    i_brief = src.index("${aag_brief}")
    i_step0 = src.index("[STEP 0 기존 구현 조사")
    assert i_rules < i_brief < i_step0

    # STEP 0 첫 줄 힌트는 브리프가 있을 때만 붙는다(빈 변수 = 빈 줄 아님).
    assert "${aag_step0_hint}- 대상 파일의 기존" in src


def test_runner_brief_condition_is_project_generic():
    """AADS 하드코딩을 걷어내고 파일 존재 여부로 판정해야 ACCT/NTV2 도 브리프를 받는다."""
    src = RUNNER.read_text(encoding="utf-8")
    block_start = src.index("AAG 착수 브리프")
    block_end = src.index("H7:", block_start)
    block = src[block_start:block_end]

    assert '"$project" == "AADS"' not in block
    assert "-f \"$main_workdir/tools/aag/brief.py\"" in block
    assert 'cd "$main_workdir"' in block
    assert '--project "$project"' in block


# ── 리더 호스트 공용화 (AADS-AAG-BRIEF-003) ──────────────────────────────
#
# 아래 테스트들은 pipeline-runner.sh 의 브리프 블록을 텍스트로 잘라 실제 bash 로
# 실행한다("경로 결정 로직" 검증). brief.py 대역은 인자를 마커 파일에 기록하는
# 가짜 python3 로 갈음해 3957행짜리 러너 전체를 띄우지 않는다.


def _extract_brief_block() -> str:
    """실행 가능한 형태로 자른다 — 주석 줄 중간이 아니라 줄 시작부터."""
    src = RUNNER.read_text(encoding="utf-8")
    marker = src.index("AAG 착수 브리프")
    start = src.rfind("\n", 0, marker) + 1
    end = src.index("H7:", marker)
    return src[start:end]


def _write_fake_python3(bin_dir: Path, marker: Path) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    stub = bin_dir / "python3"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        f'printf %s "$1" > "{marker}"\n'
        'echo "- stub brief line"\n',
        encoding="utf-8",
    )
    stub.chmod(0o755)


def _run_wiring(tmp_path, main_workdir, *, aag_brief_bin=None, fake_bin_dir=None,
                 instruction="app/api/chat.py 를 고쳐라", project="ACCT"):
    block = _extract_brief_block()
    script = f"""
set -uo pipefail
log() {{ :; }}
record_runner_event() {{ :; }}
run_block() {{
{block}
printf 'BIN=[%s]\\nSRC=[%s]\\nOUT=[%s]\\n' "$aag_brief_bin" "$aag_brief_source" "${{aag_out:-}}"
}}
ARTIFACT_DIR="{tmp_path}"
job_id="testjob"
project="{project}"
safe_instruction="{instruction}"
current_model="model"
job_size="S"
main_workdir="{main_workdir}"
run_block
"""
    env = dict(os.environ)
    if fake_bin_dir is not None:
        env["PATH"] = f"{fake_bin_dir}:{env['PATH']}"
    if aag_brief_bin is not None:
        env["AAG_BRIEF_BIN"] = str(aag_brief_bin)
    else:
        env.pop("AAG_BRIEF_BIN", None)
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                           timeout=30, env=env)


def test_runner_prefers_repo_copy_when_present(tmp_path):
    """저장소본이 있으면 호스트 공용 사본(AAG_BRIEF_BIN)보다 우선한다."""
    main_workdir = tmp_path / "repo"
    (main_workdir / "tools" / "aag").mkdir(parents=True)
    repo_brief = main_workdir / "tools" / "aag" / "brief.py"
    repo_brief.write_text("# repo stub", encoding="utf-8")
    host_brief = tmp_path / "host-aag-brief.py"
    host_brief.write_text("# host stub", encoding="utf-8")

    bin_dir = tmp_path / "bin"
    marker = tmp_path / "invoked.marker"
    _write_fake_python3(bin_dir, marker)

    res = _run_wiring(tmp_path, main_workdir, aag_brief_bin=host_brief, fake_bin_dir=bin_dir)

    assert res.returncode == 0, res.stderr
    assert f"BIN=[{repo_brief}]" in res.stdout
    assert "SRC=[repo]" in res.stdout
    assert marker.read_text(encoding="utf-8") == str(repo_brief)


def test_runner_falls_back_to_host_copy_when_repo_copy_missing(tmp_path):
    """저장소본이 없으면 AAG_BRIEF_BIN(호스트 공용 사본)을 쓴다."""
    main_workdir = tmp_path / "repo"
    main_workdir.mkdir()
    host_brief = tmp_path / "host-aag-brief.py"
    host_brief.write_text("# host stub", encoding="utf-8")

    bin_dir = tmp_path / "bin"
    marker = tmp_path / "invoked.marker"
    _write_fake_python3(bin_dir, marker)

    res = _run_wiring(tmp_path, main_workdir, aag_brief_bin=host_brief, fake_bin_dir=bin_dir)

    assert res.returncode == 0, res.stderr
    assert f"BIN=[{host_brief}]" in res.stdout
    assert "SRC=[host]" in res.stdout
    assert marker.read_text(encoding="utf-8") == str(host_brief)


def test_runner_brief_noop_when_neither_copy_exists(tmp_path):
    """둘 다 없으면 브리프 블록은 아무것도 하지 않는다 — python3 도 호출되지 않는다."""
    main_workdir = tmp_path / "repo"
    main_workdir.mkdir()
    missing_host_bin = tmp_path / "does-not-exist.py"

    bin_dir = tmp_path / "bin"
    marker = tmp_path / "invoked.marker"
    _write_fake_python3(bin_dir, marker)

    res = _run_wiring(tmp_path, main_workdir, aag_brief_bin=missing_host_bin, fake_bin_dir=bin_dir)

    assert res.returncode == 0, res.stderr
    assert "BIN=[]" in res.stdout
    assert "SRC=[]" in res.stdout
    assert "OUT=[]" in res.stdout
    assert not marker.exists()


def test_runner_renders_acct_brief_from_host_copy_only(tmp_path):
    """저장소에 brief.py 가 없어도 호스트 공용 사본만으로 ACCT 브리프가 실제 렌더된다."""
    main_workdir = tmp_path / "repo"
    graph_dir = main_workdir / "reports" / "aag"
    graph_dir.mkdir(parents=True)
    (graph_dir / "acct-graph.json").write_text(
        json.dumps(_graph(), ensure_ascii=False), encoding="utf-8")

    host_brief = tmp_path / "host" / "aag-brief.py"
    host_brief.parent.mkdir(parents=True)
    host_brief.write_text(BRIEF.read_text(encoding="utf-8"), encoding="utf-8")

    res = _run_wiring(tmp_path, main_workdir, aag_brief_bin=host_brief)

    assert res.returncode == 0, res.stderr
    assert "SRC=[host]" in res.stdout
    assert "[AAG 착수 브리프 — ACCT]" in res.stdout
    assert "app/api/chat.py" in res.stdout
