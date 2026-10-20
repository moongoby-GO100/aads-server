"""record-release-provenance.sh 의 DB 후보 SQL: 사전순 LIMIT 500 대신 최신순 + 릴리스 조상 우선.

--candidates-file 경로는 후보 선택 SQL 을 거치지 않는다. 여기서는 PATH 앞에 가짜 `docker` 를 두어
스크립트가 실제로 내보낸 SELECT 를 붙잡고, 그 SQL 의 정렬·LIMIT·접두사 필터 의미를 그대로 흉내 낸
결과를 되돌려 준다(이 환경에는 Postgres 가 없다). 그래서 이 테스트가 증명하는 것은
"스크립트가 내보내는 SQL 의 모양과, 그 의미대로 DB 가 답할 때 최신 7~f 커밋이 기록 SQL 까지 도달한다"이다.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
HOOK = ROOT / "scripts" / "record-release-provenance.sh"

STUB_DOCKER = textwrap.dedent(
    r'''
    #!/usr/bin/env python3
    import json, os, re, sys

    sql = sys.argv[-1]
    with open(os.environ["STUB_SQL_LOG"], "a", encoding="utf-8") as fh:
        fh.write(sql + "\n-----\n")
    if "DISTINCT ON" not in sql:
        sys.exit(0)
    rows = json.load(open(os.environ["STUB_ROWS"], encoding="utf-8"))
    rows = [r for r in rows if re.fullmatch(r"[0-9a-fA-F]{7,40}", r["ref"])]
    m = re.search(r"ARRAY\[([^\]]*)\]::text\[\]", sql)
    if m:
        allowed = set(re.findall(r"[0-9a-f]{7}", m.group(1)))
        rows = [r for r in rows if r["ref"].lower()[:7] in allowed]
    newest = {}
    for r in rows:
        if r["ref"] not in newest or r["seen_at"] > newest[r["ref"]]:
            newest[r["ref"]] = r["seen_at"]
    ordered = sorted(newest.items(), key=lambda kv: (-kv[1], kv[0]))
    limit = int(re.search(r"LIMIT (\d+)", sql).group(1))
    for ref, _ in ordered[:limit]:
        print(ref)
    '''
).lstrip()


def _git(repo, *args, env=None):
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True,
        env={**os.environ, **(env or {})},
    ).stdout.strip()


def _commit_with_first_char(repo, chars: str) -> str:
    """해시가 chars 중 하나로 시작할 때까지 빈 커밋을 만든다 (cda45fdc·fe850f38 같은 7~f 시작 해시)."""
    for i in range(400):
        _git(repo, "commit", "-q", "--allow-empty", "-m", f"c-{chars}-{i}")
        sha = _git(repo, "rev-parse", "HEAD")
        if sha[0] in chars:
            return sha
    raise AssertionError("could not craft a commit with the requested prefix")


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "release"
    path.mkdir()
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "t@example.com")
    _git(path, "config", "user.name", "t")
    _git(path, "commit", "-q", "--allow-empty", "-m", "base")
    return path


@pytest.fixture
def stub(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(STUB_DOCKER, encoding="utf-8")
    docker.chmod(0o755)
    return {
        "bin": bin_dir,
        "sql_log": tmp_path / "sql.log",
        "rows": tmp_path / "rows.json",
    }


def _run(repo, stub, rows, **env):
    stub["rows"].write_text(json.dumps(rows), encoding="utf-8")
    stub["sql_log"].write_text("", encoding="utf-8")
    proc = subprocess.run(
        [str(HOOK), "--repo", str(repo), "--deploy-run-id", "42", "--project", "AADS",
         "--emit-sql-only"],
        capture_output=True, text=True,
        env={**os.environ, "PATH": f"{stub['bin']}{os.pathsep}{os.environ['PATH']}",
             "STUB_SQL_LOG": str(stub["sql_log"]), "STUB_ROWS": str(stub["rows"]), **env},
    )
    return proc, stub["sql_log"].read_text(encoding="utf-8")


def _select_sql(log: str) -> str:
    return next(part for part in log.split("-----\n") if "DISTINCT ON" in part)


def test_candidate_sql_is_recency_ordered_not_lexicographic(repo, stub):
    _, log = _run(repo, stub, [])
    sql = _select_sql(log)

    assert not re.search(r"ORDER BY\s+1\b", sql)
    assert "DISTINCT ON (candidates.candidate_ref)" in sql
    assert "ORDER BY latest.seen_at DESC" in sql
    assert "LIMIT 500" in sql
    assert "COALESCE(j.updated_at, j.created_at)" in sql
    assert "COALESCE(l.updated_at, l.created_at)" in sql
    # 기존 안전 장치: 이미 이 배포에 기록된 ref 는 다시 보지 않는다. 7~40자 hex 만 후보가 된다.
    assert "NOT EXISTS" in sql and "p.source_ref = candidates.candidate_ref" in sql
    assert "p.deploy_run_id = 42" in sql
    assert "{7,40}" in sql


def test_old_lexicographic_head_no_longer_starves_newer_7_to_f_commits(repo, stub):
    c_sha = _commit_with_first_char(repo, "c")
    f_sha = _commit_with_first_char(repo, "f")
    old = [{"ref": f"{i % 7}{i:039x}"[:40], "seen_at": 1000 + i} for i in range(600)]
    recent = [{"ref": c_sha[:8], "seen_at": 90000}, {"ref": f_sha[:8], "seen_at": 90001}]

    # 예전 쿼리(ORDER BY 1 LIMIT 500)였다면: 사전순 앞 500칸은 전부 0~6 시작이라 c·f 는 탈락한다.
    lexicographic = sorted({r["ref"] for r in old + recent})[:500]
    assert c_sha[:8] not in lexicographic and f_sha[:8] not in lexicographic

    proc, log = _run(repo, stub, old + recent, AADS_PROVENANCE_ANCESTOR_DAYS="0")
    assert proc.returncode == 0, proc.stderr
    assert "ARRAY[" not in _select_sql(log)
    assert f"'{c_sha[:8]}', '{c_sha}'" in proc.stdout
    assert f"'{f_sha[:8]}', '{f_sha}'" in proc.stdout
    assert "rejected_unknown=" in proc.stderr


def test_ancestor_prefilter_keeps_limit_for_release_ancestors(repo, stub):
    ancestors = [_commit_with_first_char(repo, "89abcdef"), _commit_with_first_char(repo, "89abcdef")]
    _git(repo, "commit", "-q", "--allow-empty", "-m", "release head")
    head = _git(repo, "rev-parse", "HEAD")
    # 릴리스에 없는 분기에서 더 최근에 관측된 후보 3개가 LIMIT 2 를 가로채려 한다.
    foreign = [{"ref": f"d{i:039x}"[:40], "seen_at": 90000 + i} for i in range(3)]
    rows = foreign + [{"ref": sha[:8], "seen_at": 1000 + n} for n, sha in enumerate(ancestors)]

    proc, log = _run(repo, stub, rows, AADS_PROVENANCE_MAX_CANDIDATES="2")
    assert proc.returncode == 0, proc.stderr
    sql = _select_sql(log)
    assert "LIMIT 2" in sql and "ARRAY[" in sql
    assert f"'{head[:7]}'" in sql
    for sha in ancestors:
        assert f"'{sha[:8]}', '{sha}', '{head}', 'ancestor'" in proc.stdout

    # 필터를 끄면 같은 입력에서 조상이 밀려난다 — 필터가 실제로 일하고 있다는 대조군.
    proc_off, _ = _run(repo, stub, rows, AADS_PROVENANCE_MAX_CANDIDATES="2",
                       AADS_PROVENANCE_ANCESTOR_DAYS="0")
    assert "INSERT INTO" not in proc_off.stdout


def test_prefilter_is_not_a_safety_boundary(repo, stub):
    """접두사가 겹치는 비조상 후보는 SQL 을 통과해도 호스트 Git 판정에서 탈락한다."""
    ancestor = _commit_with_first_char(repo, "89abcdef")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "release head")
    head = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "-b", "side", "HEAD~1")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "side")
    side = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "main")

    rows = [{"ref": ancestor[:8], "seen_at": 2}, {"ref": side[:8], "seen_at": 3}]
    proc, _ = _run(repo, stub, rows, AADS_PROVENANCE_ANCESTOR_DAYS="0")
    assert f"'{ancestor[:8]}', '{ancestor}', '{head}', 'ancestor'" in proc.stdout
    assert side not in proc.stdout
    assert "rejected_not_contained=1" in proc.stderr


def test_record_sql_keeps_certified_gate_and_idempotent_conflict(repo, stub):
    sha = _commit_with_first_char(repo, "89abcdef")
    proc, _ = _run(repo, stub, [{"ref": sha[:8], "seen_at": 1}])
    sql = proc.stdout
    assert "INSERT INTO deploy_release_provenance" in sql
    assert "d.image_digest = d.standby_digest" in sql
    assert "ON CONFLICT DO NOTHING" in sql
    assert sql.index("WHERE EXISTS") < sql.index("ON CONFLICT")


def test_candidate_query_failure_still_fails_closed(repo, stub, tmp_path):
    failing = Path(stub["bin"]) / "docker"
    failing.write_text("#!/usr/bin/env bash\nexit 1\n", encoding="utf-8")
    proc, _ = _run(repo, stub, [])
    assert proc.returncode == 5
    assert proc.stdout.strip() == ""
    assert "could not load release provenance candidates" in proc.stderr
